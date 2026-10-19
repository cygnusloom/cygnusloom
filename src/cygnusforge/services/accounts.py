from __future__ import annotations

import secrets

from sqlalchemy import Engine
from sqlmodel import select

from cygnusforge.auth.passwords import hash_password, verify_password
from cygnusforge.db.models import User
from cygnusforge.services.common import Conflict, InvalidInput, NotFound, session_scope

MIN_PASSWORD = 10
MAX_PASSWORD = 256
TEST_USER_EMAIL = "test-user@example.com"


def normalize_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if not cleaned or "@" not in cleaned or len(cleaned) > 255:
        raise InvalidInput("email must be a valid email address")
    return cleaned


def _check_password(password: str) -> None:
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise InvalidInput(f"password must be {MIN_PASSWORD} to {MAX_PASSWORD} characters")


def register_user(engine: Engine, email: str, password: str) -> User:
    email = normalize_email(email)
    _check_password(password)
    with session_scope(engine) as db:
        if db.exec(select(User).where(User.email == email)).first() is not None:
            raise Conflict("an account with that email already exists")
        user = User(email=email, password_hash=hash_password(password))
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def authenticate(engine: Engine, email: str, password: str) -> User | None:
    try:
        email = normalize_email(email)
    except InvalidInput:
        return None
    with session_scope(engine) as db:
        user = db.exec(select(User).where(User.email == email)).first()
        if user is None or not verify_password(user.password_hash, password):
            return None
        return user


def get_or_create_test_user(engine: Engine) -> User:
    """Account behind the test-login button.

    The password is random and never shown, so only that button can sign in.
    """
    with session_scope(engine) as db:
        user = db.exec(select(User).where(User.email == TEST_USER_EMAIL)).first()
        if user is None:
            user = User(
                email=TEST_USER_EMAIL, password_hash=hash_password(secrets.token_urlsafe(32))
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        return user


def get_user(engine: Engine, user_id: int) -> User:
    with session_scope(engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise NotFound("account not found")
        return user


def reset_password(engine: Engine, email: str, password: str) -> None:
    """Operator reset. Sessions are signed cookies with no server-side record, so they stay valid."""
    email = normalize_email(email)
    _check_password(password)
    with session_scope(engine) as db:
        user = db.exec(select(User).where(User.email == email)).first()
        if user is None:
            raise NotFound("account not found")
        user.password_hash = hash_password(password)
        db.add(user)
        db.commit()
