"""Identity claims from the single sign-on provider.

Cygnus Loom trusts only the issuer configured in its settings. Everything else about an
account, including its roles, comes from the validated ID token and userinfo response.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_TRUE = ("1", "true", "yes", "on")


class OIDCError(Exception):
    """A sign-in could not be trusted. The message is safe to show to a user."""


@dataclass(frozen=True)
class IdentityClaims:
    """The parts of a verified identity that Cygnus Loom stores."""

    issuer: str
    subject: str
    email: str
    email_verified: bool
    display_name: str
    roles: tuple[str, ...]
    auth_time: datetime
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    # The raw ID token is kept so sign-out can present it to the provider as id_token_hint.
    id_token: str | None = field(default=None, repr=False, compare=False)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def claim_path(claims: Mapping[str, Any], path: str) -> Any:
    """Read a dotted claim path, such as `groups` or `realm_access.roles`."""
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def claim_values(value: Any) -> tuple[str, ...]:
    """A claim may be one string or a list. Return its non-empty strings, deduplicated."""
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple, set)) else (value,)
    return tuple(sorted({str(item).strip() for item in items if str(item).strip()}))


def roles_from_claims(claims: Mapping[str, Any], claim: str) -> tuple[str, ...]:
    """Every role the provider puts in the configured claim."""
    return claim_values(claim_path(claims, claim))


def has_admin_role(roles: Iterable[str], admin_values: Iterable[str]) -> bool:
    wanted = {value.casefold() for value in admin_values}
    return any(role.casefold() in wanted for role in roles)


def _auth_time(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(int(value), UTC)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return datetime.now(UTC)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    return datetime.now(UTC)


def claims_from_mapping(
    data: Mapping[str, Any], *, issuer: str, admin_claim: str
) -> IdentityClaims:
    """Validate the issuer and subject and turn a token/userinfo mapping into claims."""
    actual_issuer = str(data.get("iss") or issuer).strip().rstrip("/")
    expected_issuer = issuer.strip().rstrip("/")
    if expected_issuer and actual_issuer != expected_issuer:
        raise OIDCError("the identity provider returned a different issuer")
    subject = str(data.get("sub") or "").strip()
    if not subject:
        raise OIDCError("the identity provider did not return a subject")
    email = str(data.get("email") or "").strip().lower()
    display_name = str(data.get("name") or data.get("preferred_username") or email or subject)
    return IdentityClaims(
        issuer=actual_issuer,
        subject=subject,
        email=email,
        email_verified=_truthy(data.get("email_verified")),
        display_name=display_name.strip()[:120],
        roles=roles_from_claims(data, admin_claim),
        auth_time=_auth_time(data.get("auth_time")),
        raw=dict(data),
    )
