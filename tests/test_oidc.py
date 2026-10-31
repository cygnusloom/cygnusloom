from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.responses import Response

from cygnusforge.auth.claims import OIDCError
from cygnusforge.auth.oidc import AuthlibOIDC, account_management_url, build_oidc_provider
from cygnusforge.config import Settings

ISSUER = "https://idp.example.com/application/o/cygnusforge"


def _settings(**overrides) -> Settings:
    values = {
        "secret_key": "test-secret",
        "oidc_issuer": ISSUER,
        "oidc_client_id": "cygnusforge",
        "oidc_client_secret": "secret",
        "oidc_admin_claim": "groups",
    }
    values.update(overrides)
    return Settings(**values)


class FakeClient:
    def __init__(self, token=None, userinfo=None, metadata=None, error=None):
        self.token = token or {}
        self.info = userinfo
        self.metadata = metadata or {}
        self.error = error
        self.redirect_kwargs: dict = {}

    async def authorize_redirect(self, request, redirect_uri, **kwargs):
        self.redirect_kwargs = kwargs
        return Response("redirect")

    async def authorize_access_token(self, request):
        if self.error is not None:
            raise self.error
        return self.token

    async def userinfo(self, token=None):
        if self.info is None:
            raise RuntimeError("no userinfo endpoint")
        return self.info

    async def load_server_metadata(self):
        return self.metadata


def _provider(fake: FakeClient) -> AuthlibOIDC:
    provider = AuthlibOIDC(_settings())
    provider._client = lambda: fake  # type: ignore[method-assign]
    return provider


def test_authorize_redirect_requests_fresh_authentication_for_step_up():
    fake = FakeClient()
    provider = _provider(fake)
    asyncio.run(
        provider.authorize_redirect(None, "https://app.example.com/auth/callback", max_age=0)
    )
    assert fake.redirect_kwargs == {"max_age": 0, "prompt": "login"}


def test_fetch_claims_merges_id_token_and_userinfo():
    fake = FakeClient(
        token={
            "id_token": "raw-id-token",
            "userinfo": {
                "iss": ISSUER,
                "sub": "abc",
                "email": "ada@example.com",
                "email_verified": True,
            },
        },
        userinfo={"groups": ["cygnusforge-admins"]},
    )
    claims = asyncio.run(
        _provider(fake).fetch_claims(None, "https://app.example.com/auth/callback")
    )
    assert claims.subject == "abc"
    assert claims.email == "ada@example.com"
    assert claims.roles == ("cygnusforge-admins",)
    assert claims.id_token == "raw-id-token"


def test_fetch_claims_reports_a_rejected_token():
    provider = _provider(FakeClient(error=RuntimeError("bad code")))
    with pytest.raises(OIDCError, match="rejected"):
        asyncio.run(provider.fetch_claims(None, "https://app.example.com/auth/callback"))


def test_fetch_claims_works_when_userinfo_is_unavailable():
    fake = FakeClient(
        token={
            "userinfo": {
                "iss": ISSUER,
                "sub": "abc",
                "email": "ada@example.com",
                "email_verified": True,
                "groups": ["cygnusforge-admins"],
            }
        }
    )
    claims = asyncio.run(
        _provider(fake).fetch_claims(None, "https://app.example.com/auth/callback")
    )
    assert claims.roles == ("cygnusforge-admins",)


def test_logout_url_uses_discovery_metadata():
    fake = FakeClient(metadata={"end_session_endpoint": "https://idp.example.com/logout"})
    url = asyncio.run(
        _provider(fake).logout_url(
            None,
            "https://app.example.com/login",
            id_token_hint="raw-id-token",
        )
    )
    assert url is not None
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://idp.example.com/logout"
    assert parse_qs(parsed.query) == {
        "client_id": ["cygnusforge"],
        "post_logout_redirect_uri": ["https://app.example.com/login"],
        "id_token_hint": ["raw-id-token"],
    }


def test_provider_is_absent_until_oidc_is_configured():
    assert build_oidc_provider(Settings(secret_key="test")) is None
    assert build_oidc_provider(_settings()) is not None


def test_account_management_url_finds_the_authentik_user_interface():
    assert (
        account_management_url("https://idp.example.com/application/o/cygnusforge/")
        == "https://idp.example.com/if/user/"
    )


def test_account_management_url_finds_the_keycloak_account_console():
    assert (
        account_management_url("https://idp.example.com/realms/cygnusforge")
        == "https://idp.example.com/realms/cygnusforge/account/"
    )


@pytest.mark.parametrize("issuer", ["", "not a url", "https://accounts.example.com"])
def test_account_management_url_ignores_unknown_issuers(issuer: str):
    assert account_management_url(issuer) is None
