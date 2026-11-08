"""Administrator queries and actions.

Cygnus Loom does not manage roles. It only checks the role snapshot from the identity
provider, and records every administrative change in an append-only audit table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, String, cast, func, or_, select

from cygnusloom.db.models import (
    AdminAudit,
    Campaign,
    CampaignInvite,
    CampaignMember,
    Event,
    User,
    UserSession,
    utcnow,
)
from cygnusloom.services import credits as credit_service
from cygnusloom.services.accounts import decode_roles, get_user
from cygnusloom.services.common import (
    Conflict,
    InvalidInput,
    NotFound,
    bounded_text,
    session_scope,
)
from cygnusloom.services.sessions import active_sessions, revoke_user_sessions

PAGE_SIZE = 25


@dataclass(frozen=True)
class UserSummary:
    user: User
    campaign_count: int
    active_sessions: int


@dataclass(frozen=True)
class CampaignSummary:
    campaign: Campaign
    owner_email: str
    event_count: int
    last_event_at: datetime | None


@dataclass(frozen=True)
class UserDetail:
    user: User
    roles: tuple[str, ...]
    campaigns: tuple[CampaignSummary, ...]
    sessions: tuple[UserSession, ...]
    audit: tuple[AdminAudit, ...]


@dataclass(frozen=True)
class CampaignDetail:
    campaign: Campaign
    owner: User
    event_count: int
    events_by_channel: tuple[tuple[str, int], ...]
    events_by_kind: tuple[tuple[str, int], ...]
    last_event_at: datetime | None


@dataclass(frozen=True)
class Dashboard:
    user_count: int
    disabled_users: int
    new_users: int
    campaign_count: int
    event_count: int
    active_sessions: int
    campaigns_by_model: tuple[tuple[str, int], ...]
    recent_users: tuple[User, ...]
    recent_campaigns: tuple[CampaignSummary, ...]


def _campaign_counts():
    return (
        select(Campaign.owner_id, func.count(Campaign.id).label("campaigns"))
        .group_by(Campaign.owner_id)
        .subquery()
    )


def _session_counts():
    now = utcnow()
    return (
        select(UserSession.user_id, func.count(UserSession.id).label("sessions"))
        .where(UserSession.expires_at > now)
        .group_by(UserSession.user_id)
        .subquery()
    )


def _event_stats():
    return (
        select(
            Event.campaign_id,
            func.count(Event.id).label("events"),
            func.max(Event.created_at).label("last_event_at"),
        )
        .group_by(Event.campaign_id)
        .subquery()
    )


def dashboard(engine: Engine) -> Dashboard:
    now = utcnow()
    with session_scope(engine) as db:
        user_count = int(db.execute(select(func.count()).select_from(User)).scalar_one())
        disabled_users = int(
            db.execute(
                select(func.count()).select_from(User).where(User.disabled.is_(True))
            ).scalar_one()
        )
        new_users = int(
            db.execute(
                select(func.count())
                .select_from(User)
                .where(User.created_at >= now - timedelta(days=7))
            ).scalar_one()
        )
        campaign_count = int(db.execute(select(func.count()).select_from(Campaign)).scalar_one())
        event_count = int(db.execute(select(func.count()).select_from(Event)).scalar_one())
        session_count = int(
            db.execute(
                select(func.count()).select_from(UserSession).where(UserSession.expires_at > now)
            ).scalar_one()
        )
        by_model = tuple(
            (str(model), int(count))
            for model, count in db.execute(
                select(Campaign.model, func.count(Campaign.id))
                .group_by(Campaign.model)
                .order_by(func.count(Campaign.id).desc())
            ).all()
        )
        recent_users = tuple(
            db.execute(select(User).order_by(User.created_at.desc()).limit(5)).scalars().all()
        )
        stats = _event_stats()
        recent_campaigns = _campaign_summaries(
            db,
            select(
                Campaign,
                User.email,
                func.coalesce(stats.c.events, 0),
                stats.c.last_event_at,
            )
            .join(User, User.id == Campaign.owner_id)
            .outerjoin(stats, stats.c.campaign_id == Campaign.id)
            .order_by(Campaign.created_at.desc())
            .limit(5),
        )
    return Dashboard(
        user_count=user_count,
        disabled_users=disabled_users,
        new_users=new_users,
        campaign_count=campaign_count,
        event_count=event_count,
        active_sessions=session_count,
        campaigns_by_model=by_model,
        recent_users=recent_users,
        recent_campaigns=recent_campaigns,
    )


def _campaign_summaries(db, statement) -> tuple[CampaignSummary, ...]:
    return tuple(
        CampaignSummary(
            campaign=campaign,
            owner_email=owner_email,
            event_count=int(event_count),
            last_event_at=last_event_at,
        )
        for campaign, owner_email, event_count, last_event_at in db.exec(statement).all()
    )


def search_users(
    engine: Engine,
    query: str = "",
    status: str = "",
    page: int = 1,
    per_page: int = PAGE_SIZE,
) -> tuple[tuple[UserSummary, ...], int]:
    """Find accounts by any detail an administrator knows: account id, email, display name,
    or identity-provider subject. The account page and the campaign transfer picker share this
    search, so both accept the same queries."""
    page = max(1, page)
    conditions = []
    if query.strip():
        like = f"%{query.strip().lstrip('#').lower()}%"
        conditions.append(
            or_(
                func.lower(User.email).like(like),
                func.lower(User.display_name).like(like),
                func.lower(User.subject).like(like),
                cast(User.id, String).like(like),
            )
        )
    if status == "disabled":
        conditions.append(User.disabled.is_(True))
    elif status == "active":
        conditions.append(User.disabled.is_(False))
    counts = _campaign_counts()
    sessions = _session_counts()
    with session_scope(engine) as db:
        total = int(
            db.execute(select(func.count()).select_from(User).where(*conditions)).scalar_one()
        )
        statement = (
            select(
                User,
                func.coalesce(counts.c.campaigns, 0),
                func.coalesce(sessions.c.sessions, 0),
            )
            .outerjoin(counts, counts.c.owner_id == User.id)
            .outerjoin(sessions, sessions.c.user_id == User.id)
            .where(*conditions)
            .order_by(User.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        rows = tuple(
            UserSummary(
                user=user,
                campaign_count=int(campaign_count),
                active_sessions=int(session_count),
            )
            for user, campaign_count, session_count in db.exec(statement).all()
        )
    return rows, total


def user_detail(engine: Engine, user_id: int) -> UserDetail:
    user = get_user(engine, user_id)
    stats = _event_stats()
    with session_scope(engine) as db:
        rows = db.exec(
            select(
                Campaign,
                User.email,
                func.coalesce(stats.c.events, 0),
                stats.c.last_event_at,
            )
            .join(User, User.id == Campaign.owner_id)
            .outerjoin(stats, stats.c.campaign_id == Campaign.id)
            .where(Campaign.owner_id == user_id)
            .order_by(Campaign.created_at.desc())
        ).all()
        campaigns = tuple(
            CampaignSummary(
                campaign=campaign,
                owner_email=owner_email,
                event_count=int(event_count),
                last_event_at=last_event_at,
            )
            for campaign, owner_email, event_count, last_event_at in rows
        )
        audit_rows = (
            db.execute(
                select(AdminAudit)
                .where(
                    or_(
                        (AdminAudit.target_type == "user") & (AdminAudit.target_id == str(user_id)),
                        AdminAudit.actor_id == user_id,
                    )
                )
                .order_by(AdminAudit.created_at.desc())
                .limit(20)
            )
            .scalars()
            .all()
        )
    return UserDetail(
        user=user,
        roles=decode_roles(user.roles),
        campaigns=campaigns,
        sessions=tuple(active_sessions(engine, user_id)),
        audit=tuple(audit_rows),
    )


def search_campaigns(
    engine: Engine,
    query: str = "",
    page: int = 1,
    per_page: int = PAGE_SIZE,
) -> tuple[tuple[CampaignSummary, ...], int]:
    page = max(1, page)
    conditions = []
    if query.strip():
        like = f"%{query.strip().lower()}%"
        conditions.append(
            or_(
                func.lower(Campaign.name).like(like),
                func.lower(User.email).like(like),
                func.lower(User.display_name).like(like),
            )
        )
    stats = _event_stats()
    with session_scope(engine) as db:
        total = int(
            db.execute(
                select(func.count())
                .select_from(Campaign)
                .join(User, User.id == Campaign.owner_id)
                .where(*conditions)
            ).scalar_one()
        )
        statement = (
            select(
                Campaign,
                User.email,
                func.coalesce(stats.c.events, 0),
                stats.c.last_event_at,
            )
            .join(User, User.id == Campaign.owner_id)
            .outerjoin(stats, stats.c.campaign_id == Campaign.id)
            .where(*conditions)
            .order_by(Campaign.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        campaigns = _campaign_summaries(db, statement)
    return campaigns, total


def campaign_detail(engine: Engine, campaign_id: int) -> CampaignDetail:
    with session_scope(engine) as db:
        row = db.execute(
            select(Campaign, User)
            .join(User, User.id == Campaign.owner_id)
            .where(Campaign.id == campaign_id)
        ).first()
        if row is None:
            raise NotFound("campaign not found")
        campaign, owner = row
        event_count = int(
            db.execute(
                select(func.count()).select_from(Event).where(Event.campaign_id == campaign_id)
            ).scalar_one()
        )
        by_channel = tuple(
            (str(channel), int(count))
            for channel, count in db.execute(
                select(Event.channel, func.count(Event.id))
                .where(Event.campaign_id == campaign_id)
                .group_by(Event.channel)
                .order_by(func.count(Event.id).desc())
            ).all()
        )
        by_kind = tuple(
            (str(kind), int(count))
            for kind, count in db.execute(
                select(Event.kind, func.count(Event.id))
                .where(Event.campaign_id == campaign_id)
                .group_by(Event.kind)
                .order_by(func.count(Event.id).desc())
                .limit(15)
            ).all()
        )
        last_event_at = db.execute(
            select(func.max(Event.created_at)).where(Event.campaign_id == campaign_id)
        ).scalar_one()
    return CampaignDetail(
        campaign=campaign,
        owner=owner,
        event_count=event_count,
        events_by_channel=by_channel,
        events_by_kind=by_kind,
        last_event_at=last_event_at,
    )


def record_audit(
    engine: Engine,
    *,
    actor: User,
    action: str,
    target_type: str,
    target_id: str,
    details: dict | None = None,
    ip: str = "",
) -> None:
    row = AdminAudit(
        actor_id=actor.id or 0,
        actor_email=actor.email,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=json.dumps(details or {}, sort_keys=True),
        ip=ip[:64],
    )
    with session_scope(engine) as db:
        db.add(row)
        db.commit()


def set_user_credits(
    engine: Engine, actor: User, user_id: int, balance: int, *, ip: str = ""
) -> int:
    """Set an account's credit balance. Payment is handled outside Cygnus Loom, so an
    administrator is the only source of credits."""
    user = get_user(engine, user_id)
    balance = credit_service.validate_credits(balance)
    previous = user.credit_balance
    credit_service.set_balance(
        engine,
        user_id,
        balance,
        reason=credit_service.ADMIN_REASON,
        note=f"set by administrator {actor.email or actor.id}",
        actor_id=actor.id,
    )
    record_audit(
        engine,
        actor=actor,
        action="user.credits_set",
        target_type="user",
        target_id=str(user_id),
        details={"before": previous, "after": balance},
        ip=ip,
    )
    return balance


def set_user_disabled(
    engine: Engine, actor: User, user_id: int, disabled: bool, *, ip: str = ""
) -> None:
    if actor.id == user_id and disabled:
        raise Conflict("you cannot disable your own account")
    user = get_user(engine, user_id)
    with session_scope(engine) as db:
        user.disabled = disabled
        db.add(user)
        db.commit()
    if disabled:
        revoke_user_sessions(engine, user_id)
    record_audit(
        engine,
        actor=actor,
        action="user.disabled" if disabled else "user.enabled",
        target_type="user",
        target_id=str(user_id),
        details={"email": user.email},
        ip=ip,
    )


def revoke_sessions(engine: Engine, actor: User, user_id: int, *, ip: str = "") -> int:
    count = revoke_user_sessions(engine, user_id)
    record_audit(
        engine,
        actor=actor,
        action="user.sessions_revoked",
        target_type="user",
        target_id=str(user_id),
        details={"count": count},
        ip=ip,
    )
    return count


def delete_user(engine: Engine, actor: User, user_id: int, *, ip: str = "") -> None:
    if actor.id == user_id:
        raise Conflict("you cannot delete your own account")
    user = get_user(engine, user_id)
    with session_scope(engine) as db:
        campaign_count = int(
            db.execute(
                select(func.count()).select_from(Campaign).where(Campaign.owner_id == user_id)
            ).scalar_one()
        )
        if campaign_count:
            raise Conflict("transfer or delete the account's campaigns first")
        revoke_user_sessions(engine, user_id)
        db.delete(user)
        db.commit()
    record_audit(
        engine,
        actor=actor,
        action="user.deleted",
        target_type="user",
        target_id=str(user_id),
        details={"email": user.email},
        ip=ip,
    )


def rename_campaign(
    engine: Engine, actor: User, campaign_id: int, name: str, *, ip: str = ""
) -> None:
    name = bounded_text(name, "campaign name", 120)
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFound("campaign not found")
        before = campaign.name
        campaign.name = name
        db.add(campaign)
        db.commit()
    record_audit(
        engine,
        actor=actor,
        action="campaign.renamed",
        target_type="campaign",
        target_id=str(campaign_id),
        details={"before": before, "after": name},
        ip=ip,
    )


def set_campaign_model(
    engine: Engine,
    actor: User,
    campaign_id: int,
    model: str,
    allowed_models: tuple[str, ...],
    *,
    ip: str = "",
) -> None:
    model = bounded_text(model, "model", 120)
    if model not in allowed_models:
        raise InvalidInput("that model is not enabled")
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFound("campaign not found")
        before = campaign.model
        campaign.model = model
        db.add(campaign)
        db.commit()
    record_audit(
        engine,
        actor=actor,
        action="campaign.model_changed",
        target_type="campaign",
        target_id=str(campaign_id),
        details={"before": before, "after": model},
        ip=ip,
    )


def transfer_campaign(
    engine: Engine, actor: User, campaign_id: int, owner_id: int, *, ip: str = ""
) -> None:
    """Make an account the campaign owner. Accounts are chosen by numeric id because email
    addresses are not unique across identity providers."""
    if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0:
        raise InvalidInput("choose the new owner by account ID")
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFound("campaign not found")
        new_owner = db.get(User, owner_id)
        if new_owner is None:
            raise NotFound("account not found")
        if campaign.owner_id == new_owner.id:
            raise Conflict("that account already owns this campaign")
        before = campaign.owner_id
        campaign.owner_id = new_owner.id or before
        # The new owner is implicit, so any membership row for them would be redundant.
        db.execute(
            CampaignMember.__table__.delete().where(
                CampaignMember.campaign_id == campaign_id,
                CampaignMember.user_id == campaign.owner_id,
            )
        )
        db.add(campaign)
        db.commit()
    record_audit(
        engine,
        actor=actor,
        action="campaign.transferred",
        target_type="campaign",
        target_id=str(campaign_id),
        details={"from": before, "to": new_owner.id, "to_email": new_owner.email},
        ip=ip,
    )


def delete_campaign(engine: Engine, actor: User, campaign_id: int, *, ip: str = "") -> None:
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFound("campaign not found")
        name = campaign.name
        owner_id = campaign.owner_id
        db.execute(Event.__table__.delete().where(Event.campaign_id == campaign_id))
        db.execute(
            CampaignMember.__table__.delete().where(CampaignMember.campaign_id == campaign_id)
        )
        db.execute(
            CampaignInvite.__table__.delete().where(CampaignInvite.campaign_id == campaign_id)
        )
        db.delete(campaign)
        db.commit()
    record_audit(
        engine,
        actor=actor,
        action="campaign.deleted",
        target_type="campaign",
        target_id=str(campaign_id),
        details={"name": name, "owner_id": owner_id},
        ip=ip,
    )


def audit_entries(
    engine: Engine, page: int = 1, per_page: int = PAGE_SIZE
) -> tuple[tuple[AdminAudit, ...], int]:
    page = max(1, page)
    with session_scope(engine) as db:
        total = int(db.execute(select(func.count()).select_from(AdminAudit)).scalar_one())
        rows = (
            db.execute(
                select(AdminAudit)
                .order_by(AdminAudit.created_at.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
            .scalars()
            .all()
        )
    return tuple(rows), total
