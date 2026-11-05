from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request
from starlette.responses import RedirectResponse, Response

from cygnusloom.auth.claims import IdentityClaims, OIDCError


def claims(
    *,
    subject: str = "subject-1",
    email: str = "ada@example.com",
    roles: tuple[str, ...] = (),
    issuer: str = "https://idp.example.com",
    auth_time: datetime | None = None,
    id_token: str | None = None,
) -> IdentityClaims:
    return IdentityClaims(
        issuer=issuer,
        subject=subject,
        email=email,
        email_verified=True,
        display_name=email.split("@", 1)[0],
        roles=roles,
        auth_time=auth_time or datetime.now(UTC),
        id_token=id_token,
    )


@dataclass
class FakeOIDC:
    """A scripted identity provider for tests. It never makes a network request."""

    identity: IdentityClaims | None = None
    error: OIDCError | None = None
    name: str = "Test SSO"
    logout: str | None = None
    logout_hint: str | None = None

    async def authorize_redirect(
        self, request: Request, redirect_uri: str, *, max_age: int | None = None
    ) -> Response:
        return RedirectResponse("/auth/callback", status_code=303)

    async def fetch_claims(self, request: Request, redirect_uri: str) -> IdentityClaims:
        if self.error is not None:
            raise self.error
        if self.identity is None:
            raise OIDCError("no identity scripted")
        return self.identity

    async def logout_url(
        self,
        request: Request,
        post_logout_redirect_uri: str,
        *,
        id_token_hint: str | None = None,
    ) -> str | None:
        self.logout_hint = id_token_hint
        return self.logout
