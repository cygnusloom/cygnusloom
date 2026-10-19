from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

from cygnusforge.db.models import User
from cygnusforge.services.accounts import get_user
from cygnusforge.services.common import NotFound

CSRF_KEY = "csrf"
USER_KEY = "user_id"


class LoginRequired(Exception):
    """Raised by page dependencies when no session user exists. Handled as a redirect."""


def current_user(request: Request) -> User | None:
    user_id = request.session.get(USER_KEY)
    if not isinstance(user_id, int):
        return None
    try:
        return get_user(request.app.state.engine, user_id)
    except NotFound:
        return None


def page_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


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
