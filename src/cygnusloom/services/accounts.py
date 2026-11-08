from __future__ import annotations

import json
from collections.abc import Iterable

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from cygnusloom.auth.claims import IdentityClaims, looks_like_email
from cygnusloom.db.models import User, utcnow
from cygnusloom.services.common import InvalidInput, NotFound, session_scope
from cygnusloom.services.names import random_display_name

TEST_ISSUER = "test"
TEST_USER_EMAIL = "test-user@example.com"
LOCAL_ISSUER = "local"
LOCAL_SUBJECT = "player"
LOCAL_EMAIL = "player@localhost"
LOCAL_ROLE = "local-admin"

MAX_DISPLAY_NAME = 120


def clean_display_name(value: str) -> str:
    """A display name safe to show other players, or "" when the value is blank or an email."""
    name = (value or "").strip()
    if not name or looks_like_email(name):
        return ""
    return name[:MAX_DISPLAY_NAME]


def public_name(user: User | None) -> str:
    """The name other players and the GM see. The account's email is never used."""
    if user is None:
        return ""
    return public_name_for(user.id, user.display_name)


def public_name_for(user_id: int | None, display_name: str) -> str:
    """A stored display name for a player, with a neutral fallback when it is missing."""
    name = clean_display_name(display_name)
    if name:
        return name
    return f"account {user_id}" if user_id is not None else "another player"


def set_display_name(engine: Engine, user_id: int, value: str) -> User:
    """Change the public name of an account. Players choose this on the Account page."""
    name = (value or "").strip()
    if not name:
        raise InvalidInput("display name is required")
    if len(name) > MAX_DISPLAY_NAME:
        raise InvalidInput(f"display name must be at most {MAX_DISPLAY_NAME} characters")
    if looks_like_email(name):
        raise InvalidInput("choose a display name, not an email address")
    with session_scope(engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise NotFound("account not found")
        user.display_name = name
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def set_show_internal_thinking(engine: Engine, user_id: int, enabled: bool) -> User:
    """Store whether an account sees the GM's hidden Log lines and reply details.

    This is a view preference, not campaign state: every member of a campaign chooses their own.
    """
    with session_scope(engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise NotFound("account not found")
        user.show_internal_thinking = enabled
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def _initial_display_name(claims: IdentityClaims) -> str:
    return clean_display_name(claims.display_name) or random_display_name()


class AccountDisabled(Exception):
    """The account exists but a Cygnus Loom administrator has suspended it."""


def normalize_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if not cleaned or "@" not in cleaned or len(cleaned) > 255:
        raise InvalidInput("email must be a valid email address")
    return cleaned


def is_test_user(user: User) -> bool:
    return user.issuer == TEST_ISSUER


def is_local_user(user: User) -> bool:
    return user.issuer == LOCAL_ISSUER


def encode_roles(roles: Iterable[str]) -> str:
    return json.dumps(sorted({role.strip() for role in roles if role.strip()}))


def decode_roles(value: str | None) -> tuple[str, ...]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(role) for role in parsed if isinstance(role, str) and role)


def user_roles(user: User) -> tuple[str, ...]:
    return decode_roles(user.roles)


def provision_user(engine: Engine, claims: IdentityClaims) -> User:
    """Create or refresh the account behind a verified provider identity."""
    issuer = claims.issuer.strip().rstrip("/")
    subject = claims.subject.strip()
    if not issuer or not subject:
        raise InvalidInput("the identity provider did not return a stable account id")
    with session_scope(engine) as db:
        user = db.exec(select(User).where(User.issuer == issuer, User.subject == subject)).first()
        if user is None:
            user = User(
                issuer=issuer,
                subject=subject,
                email=claims.email[:255],
                email_verified=claims.email_verified,
                display_name=_initial_display_name(claims),
                roles=encode_roles(claims.roles),
                created_at=utcnow(),
            )
            db.add(user)
        else:
            if user.disabled:
                raise AccountDisabled("this account has been disabled")
            user.email = claims.email[:255]
            user.email_verified = claims.email_verified
            # A player's chosen display name belongs to them. The provider only seeds one the
            # first time, and a blank or email-looking legacy value is replaced.
            if not clean_display_name(user.display_name):
                user.display_name = _initial_display_name(claims)
            user.roles = encode_roles(claims.roles)
        user.last_login_at = utcnow()
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # Two tabs can finish the authorization code flow at the same time. The
            # (issuer, subject) constraint lets one win; this one refreshes that row.
            db.rollback()
            user = db.exec(
                select(User).where(User.issuer == issuer, User.subject == subject)
            ).first()
            if user is None:
                raise
            if user.disabled:
                raise AccountDisabled("this account has been disabled")
            user.email = claims.email[:255]
            user.email_verified = claims.email_verified
            if not clean_display_name(user.display_name):
                user.display_name = _initial_display_name(claims)
            user.roles = encode_roles(claims.roles)
            user.last_login_at = utcnow()
            db.add(user)
            db.commit()
        db.refresh(user)
        return user


def get_or_create_test_user(
    engine: Engine,
    email: str = TEST_USER_EMAIL,
    roles: Iterable[str] | None = None,
) -> User:
    """Development-only account for the test-login button.

    An existing account keeps its roles unless `roles` is given, so tests can create an
    administrator once and then sign in through the normal button.
    """
    email = normalize_email(email)
    with session_scope(engine) as db:
        user = db.exec(
            select(User).where(User.issuer == TEST_ISSUER, User.subject == email)
        ).first()
        if user is None:
            user = User(
                issuer=TEST_ISSUER,
                subject=email,
                email=email,
                email_verified=True,
                display_name=clean_display_name(email.split("@", 1)[0]) or random_display_name(),
                roles=encode_roles(roles or ()),
            )
            db.add(user)
        else:
            user.email = email
            if not clean_display_name(user.display_name):
                user.display_name = (
                    clean_display_name(email.split("@", 1)[0]) or random_display_name()
                )
            if roles is not None:
                user.roles = encode_roles(roles)
        user.last_login_at = utcnow()
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def get_or_create_local_user(engine: Engine) -> User:
    """The one account behind the single-player edition. It is always an administrator."""
    with session_scope(engine) as db:
        user = db.exec(
            select(User).where(User.issuer == LOCAL_ISSUER, User.subject == LOCAL_SUBJECT)
        ).first()
        if user is None:
            user = User(
                issuer=LOCAL_ISSUER,
                subject=LOCAL_SUBJECT,
                email=LOCAL_EMAIL,
                email_verified=True,
                display_name="Local player",
                roles=encode_roles((LOCAL_ROLE,)),
                created_at=utcnow(),
            )
            db.add(user)
            try:
                db.commit()
            except IntegrityError:
                # Two tabs can create the local account at the same moment.
                db.rollback()
                user = db.exec(
                    select(User).where(User.issuer == LOCAL_ISSUER, User.subject == LOCAL_SUBJECT)
                ).first()
                if user is None:
                    raise
        user.last_login_at = utcnow()
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
