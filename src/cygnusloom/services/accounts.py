from __future__ import annotations

import json
from collections.abc import Iterable

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from cygnusloom.auth.claims import IdentityClaims
from cygnusloom.db.models import User, utcnow
from cygnusloom.services.common import InvalidInput, NotFound, session_scope

TEST_ISSUER = "test"
TEST_USER_EMAIL = "test-user@example.com"
LOCAL_ISSUER = "local"
LOCAL_SUBJECT = "player"
LOCAL_EMAIL = "player@localhost"
LOCAL_ROLE = "local-admin"


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
                display_name=claims.display_name[:120],
                roles=encode_roles(claims.roles),
                created_at=utcnow(),
            )
            db.add(user)
        else:
            if user.disabled:
                raise AccountDisabled("this account has been disabled")
            user.email = claims.email[:255]
            user.email_verified = claims.email_verified
            user.display_name = claims.display_name[:120]
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
            user.display_name = claims.display_name[:120]
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
                display_name=email.split("@", 1)[0],
                roles=encode_roles(roles or ()),
            )
            db.add(user)
        else:
            user.email = email
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
