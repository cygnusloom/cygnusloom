from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete, func, select

from cygnusforge.db.models import User, UserSession, utcnow
from cygnusforge.services.common import session_scope


def as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; compare everything in aware UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def create_session(
    engine: Engine,
    user_id: int,
    *,
    auth_time: datetime,
    hours: int,
    ip: str = "",
    user_agent: str = "",
) -> UserSession:
    now = utcnow()
    row = UserSession(
        id=secrets.token_urlsafe(32),
        user_id=user_id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=hours),
        auth_time=as_utc(auth_time),
        ip=ip[:64],
        user_agent=user_agent[:255],
    )
    with session_scope(engine) as db:
        # Expired rows for this account are bookkeeping, not history.
        db.execute(
            delete(UserSession).where(UserSession.user_id == user_id, UserSession.expires_at <= now)
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


def get_session(engine: Engine, session_id: str) -> tuple[User, UserSession] | None:
    """The account and session behind a cookie, or None when the login is no longer valid."""
    now = utcnow()
    with session_scope(engine) as db:
        row = db.get(UserSession, session_id)
        if row is None:
            return None
        if as_utc(row.expires_at) <= now:
            db.delete(row)
            db.commit()
            return None
        user = db.get(User, row.user_id)
        if user is None or user.disabled:
            db.delete(row)
            db.commit()
            return None
        if (now - as_utc(row.last_seen_at)).total_seconds() > 60:
            row.last_seen_at = now
            db.add(row)
            db.commit()
            db.refresh(row)
        return user, row


def revoke_session(engine: Engine, session_id: str) -> None:
    with session_scope(engine) as db:
        row = db.get(UserSession, session_id)
        if row is not None:
            db.delete(row)
            db.commit()


def revoke_user_sessions(engine: Engine, user_id: int, *, keep: str | None = None) -> int:
    with session_scope(engine) as db:
        rows = db.execute(select(UserSession).where(UserSession.user_id == user_id)).scalars().all()
        count = 0
        for row in rows:
            if keep is not None and row.id == keep:
                continue
            db.delete(row)
            count += 1
        db.commit()
        return count


def active_sessions(engine: Engine, user_id: int) -> list[UserSession]:
    now = utcnow()
    with session_scope(engine) as db:
        return list(
            db.execute(
                select(UserSession)
                .where(UserSession.user_id == user_id, UserSession.expires_at > now)
                .order_by(UserSession.last_seen_at.desc())
            )
            .scalars()
            .all()
        )


def count_active_sessions(engine: Engine) -> int:
    now = utcnow()
    with session_scope(engine) as db:
        return int(
            db.execute(
                select(func.count()).select_from(UserSession).where(UserSession.expires_at > now)
            ).scalar_one()
        )


def purge_expired_sessions(engine: Engine) -> int:
    now = utcnow()
    with session_scope(engine) as db:
        result = db.execute(delete(UserSession).where(UserSession.expires_at <= now))
        db.commit()
        return int(result.rowcount or 0)
