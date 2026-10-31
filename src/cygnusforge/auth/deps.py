from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request

from cygnusforge.auth.claims import has_admin_role
from cygnusforge.db.models import User, UserSession
from cygnusforge.services.accounts import user_roles
from cygnusforge.services.sessions import (
    as_utc,
    create_session,
    get_session,
    revoke_session,
)

CSRF_KEY = "csrf"
SESSION_KEY = "session_id"


class LoginRequired(Exception):
    """Raised by page dependencies when no session user exists. Handled as a redirect."""


class AdminRequired(Exception):
    """Raised when a signed-in account does not carry the configured administrator role."""


class StepUpRequired(Exception):
    """The administrator must prove a recent sign-in before a destructive action."""

    def __init__(self, next_path: str) -> None:
        super().__init__(next_path)
        self.next_path = next_path


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def current_session(request: Request) -> tuple[User, UserSession] | None:
    session_id = request.session.get(SESSION_KEY)
    if not isinstance(session_id, str) or not session_id:
        return None
    found = get_session(request.app.state.engine, session_id)
    if found is None:
        request.session.pop(SESSION_KEY, None)
        return None
    return found


def current_user(request: Request) -> User | None:
    found = current_session(request)
    return found[0] if found is not None else None


def page_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def is_admin_user(request: Request, user: User | None) -> bool:
    if user is None:
        return False
    settings = request.app.state.settings
    return has_admin_role(user_roles(user), settings.oidc_admin_values)


def admin_user(request: Request) -> User:
    user = page_user(request)
    if not is_admin_user(request, user):
        raise AdminRequired()
    return user


def require_recent_auth(request: Request, next_path: str) -> None:
    """Destructive admin actions need a sign-in inside the step-up window."""
    minutes = request.app.state.settings.step_up_minutes
    if minutes <= 0:
        return
    found = current_session(request)
    if found is None:
        raise LoginRequired()
    _, session = found
    if as_utc(session.auth_time) < datetime.now(UTC) - timedelta(minutes=minutes):
        raise StepUpRequired(next_path)


def sign_in(
    request: Request,
    user: User,
    auth_time: datetime,
    *,
    id_token: str | None = None,
) -> None:
    """Start a new server-side session. Any old cookie state is discarded."""
    request.session.clear()
    if user.id is None:
        raise RuntimeError("cannot sign in an account without an id")
    row = create_session(
        request.app.state.engine,
        user.id,
        auth_time=auth_time,
        hours=request.app.state.settings.session_hours,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        id_token=id_token,
    )
    request.session[SESSION_KEY] = row.id


def sign_out(request: Request) -> None:
    session_id = request.session.get(SESSION_KEY)
    if isinstance(session_id, str) and session_id:
        revoke_session(request.app.state.engine, session_id)
    request.session.clear()


def get_csrf(request: Request) -> str:
    token = request.session.get(CSRF_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    """Check the csrf_token form field against the session token."""
    expected = request.session.get(CSRF_KEY)
    form = await request.form()
    value = form.get("csrf_token")
    supplied = value if isinstance(value, str) else None
    if (
        not isinstance(expected, str)
        or not supplied
        or not secrets.compare_digest(expected, supplied)
    ):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
