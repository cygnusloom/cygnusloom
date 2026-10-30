"""The OpenID Connect relying party.

All account details, credentials, MFA, and role membership live in the identity provider.
Cygnusforge only validates the authorization-code response and reads standard claims.
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from starlette.responses import Response

from cygnusforge.auth.claims import IdentityClaims, OIDCError, claims_from_mapping
from cygnusforge.config import Settings


class OIDCProvider(Protocol):
    """What the web layer needs from an identity provider. Tests supply a fake."""

    name: str

    async def authorize_redirect(
        self, request: Request, redirect_uri: str, *, max_age: int | None = None
    ) -> Response: ...

    async def fetch_claims(self, request: Request, redirect_uri: str) -> IdentityClaims: ...

    async def logout_url(self, request: Request, post_logout_redirect_uri: str) -> str | None: ...


class AuthlibOIDC:
    """A single OIDC provider, registered with Authlib's Starlette integration."""

    name: str

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.name = settings.oidc_name
        self.issuer = settings.oidc_issuer.rstrip("/")
        self.oauth = OAuth()
        self.oauth.register(
            name="cygnusforge",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=f"{self.issuer}/.well-known/openid-configuration",
            client_kwargs={
                "scope": settings.oidc_scopes,
                "code_challenge_method": "S256",
            },
        )

    def _client(self):
        return self.oauth.create_client("cygnusforge")

    async def authorize_redirect(
        self, request: Request, redirect_uri: str, *, max_age: int | None = None
    ) -> Response:
        extra: dict[str, object] = {}
        if max_age is not None:
            extra["max_age"] = max_age
            if max_age == 0:
                extra["prompt"] = "login"
        return await self._client().authorize_redirect(request, redirect_uri, **extra)

    async def fetch_claims(self, request: Request, redirect_uri: str) -> IdentityClaims:
        client = self._client()
        try:
            token = await client.authorize_access_token(request)
        except OIDCError:
            raise
        except Exception as exc:  # Authlib raises several transport and validation errors.
            raise OIDCError("the identity provider rejected the sign-in") from exc
        claims: dict[str, object] = dict(token.get("userinfo") or {})
        try:
            userinfo = await client.userinfo(token=token)
        except Exception:  # noqa: BLE001 (provider-specific transport errors)
            # Some providers put every claim in the ID token and do not run a userinfo
            # endpoint. The validated ID token claims are enough in that case.
            userinfo = None
        if userinfo:
            claims.update(dict(userinfo))
        try:
            return claims_from_mapping(
                claims,
                issuer=self.issuer,
                admin_claim=self.settings.oidc_admin_claim,
            )
        except OIDCError:
            raise
        except Exception as exc:
            raise OIDCError("the identity provider returned unusable claims") from exc

    async def logout_url(self, request: Request, post_logout_redirect_uri: str) -> str | None:
        try:
            metadata = await self._client().load_server_metadata()
        except Exception:  # noqa: BLE001 (logout is best effort)
            return None
        endpoint = metadata.get("end_session_endpoint")
        if not endpoint:
            return None
        query = urlencode(
            {
                "client_id": self.settings.oidc_client_id,
                "post_logout_redirect_uri": post_logout_redirect_uri,
            }
        )
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{query}"


def build_oidc_provider(settings: Settings) -> AuthlibOIDC | None:
    """The configured provider, or None when single sign-on is not set up (development)."""
    if not (settings.oidc_issuer and settings.oidc_client_id and settings.oidc_client_secret):
        return None
    return AuthlibOIDC(settings)
