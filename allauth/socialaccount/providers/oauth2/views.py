from __future__ import absolute_import

from datetime import timedelta
from requests import RequestException

from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone

from allauth.exceptions import ImmediateHttpResponse
from allauth.socialaccount import providers
from allauth.socialaccount.helpers import (
    complete_social_login,
    render_authentication_error,
)
from allauth.socialaccount.models import SocialLogin, SocialToken
from allauth.socialaccount.providers.base import ProviderException
from allauth.socialaccount.providers.oauth2.client import OAuth2Client, OAuth2Error
from allauth.utils import build_absolute_uri, get_request_param

from ..base import AuthAction, AuthError


class OAuth2Adapter(object):
    client_cls = OAuth2Client
    expires_in_key = "expires_in"
    supports_state = True
    redirect_uri_protocol = None
    access_token_method = "POST"
    login_cancelled_error = "access_denied"
    scope_delimiter = " "
    basic_auth = False
    headers = None

    def __init__(self, request):
        self.request = request

    def get_provider(self):
        return providers.registry.by_id(self.provider_id, self.request)

    def complete_login(self, request, app, access_token, **kwargs):
        """
        Returns a SocialLogin instance
        """
        raise NotImplementedError

    def get_callback_url(self, request, app):
        callback_url = reverse(self.provider_id + "_callback")
        protocol = self.redirect_uri_protocol
        return build_absolute_uri(request, callback_url, protocol)

    def parse_token(self, data):
        token = SocialToken(token=data["access_token"])
        token.token_secret = data.get("refresh_token", "")
        expires_in = data.get(self.expires_in_key, None)
        if expires_in:
            token.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        return token

    def get_access_token_data(self, request, app, client):
        code = get_request_param(self.request, "code")
        return client.get_access_token(code)



class OAuth2View(object):

    @classmethod
    def adapter_view(cls, adapter):
        def view(request, *args, **kwargs):
            self = cls()
            self.request = request
            self.adapter = adapter(request)
            try:
                return self.dispatch(request, *args, **kwargs)
            except ImmediateHttpResponse as e:
                return e.response

        return view

    def get_client(self, request, app):
        callback_url = self.adapter.get_callback_url(request, app)
        provider = self.adapter.get_provider()
        scope = provider.get_scope(request)
        client = self.adapter.client_cls(
            self.request,
            app.client_id,
            app.secret,
            self.adapter.access_token_method,
            self.adapter.access_token_url,
            callback_url,
            scope,
            key=app.key,
            cert=app.cert,
            scope_delimiter=self.adapter.scope_delimiter,
            headers=self.adapter.headers,
            basic_auth=self.adapter.basic_auth,
        )
        return client


class OAuth2LoginView(OAuth2View):
    def dispatch(self, request, *args, **kwargs):
        provider = self.adapter.get_provider()
        app = provider.get_app(self.request)
        client = self.get_client(request, app)
        action = request.GET.get('action', AuthAction.AUTHENTICATE)
        auth_url = self.adapter.authorize_url
        auth_params = provider.get_auth_params(request, action)
        client.state = SocialLogin.stash_state(request)
        try:
            return HttpResponseRedirect(client.get_redirect_url(
                auth_url, auth_params))
        except OAuth2Error as e:
            return render_authentication_error(
                request,
                provider.id,
                exception=e)


class OAuth2CallbackView(OAuth2View):

    def dispatch(self, request, *args, **kwargs):
        auth_error = get_request_param(request, "error")
        code = get_request_param(request, "code")
        if auth_error or not code:
            # Distinguish cancel from error
            if auth_error == self.adapter.login_cancelled_error:
                error = AuthError.CANCELLED
            else:
                error = AuthError.UNKNOWN
            return render_authentication_error(
                request,
                self.adapter.provider_id,
                error=error)

        app = self.adapter.get_provider().get_app(self.request)
        client = self.get_client(self.request, app)

        try:
            token_data = self.adapter.get_access_token_data(
                self.request, app=app, client=client
            )
            token = self.adapter.parse_token(data=token_data)
            token.app = app

            login = self.adapter.complete_login(
                request, app, token, response=token_data
            )
            login.token = token

            state = get_request_param(request, "state")

            if self.adapter.supports_state:
                login.state = SocialLogin.verify_and_unstash_state(request, state)
            else:
                login.state = SocialLogin.unstash_state(request)

            return complete_social_login(request, login)

        except (
            PermissionDenied,
            OAuth2Error,
            RequestException,
            ProviderException,
        ) as e:
            return render_authentication_error(
                request, self.adapter.provider_id, exception=e
            )
