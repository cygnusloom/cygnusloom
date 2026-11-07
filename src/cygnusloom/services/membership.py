"""Campaign sharing: memberships and the join codes an owner shares.

A campaign is owned by one account. The owner can create join codes, and any signed-in account
that opens one becomes a member. Membership is stored beside the campaign rather than in its
event log: it is access, not part of the story. Removing a member can either leave their
character in the story as a shared, unclaimed part or retire it.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from cygnusloom.db.models import Campaign, CampaignInvite, CampaignMember, User, utcnow
from cygnusloom.services.accounts import public_name
from cygnusloom.services.common import InvalidInput, NotFound, session_scope
from cygnusloom.services.events import (
    CharacterRetired,
    PcUnclaimed,
    load_state,
    name_key,
    record,
)

MAX_INVITE_LABEL = 120


@dataclass(frozen=True)
class MemberView:
    """One account at the campaign's table, as the Players panel lists it."""

    user_id: int
    display_name: str
    owner: bool = False
    joined_at: datetime | None = None
    character: str = ""
    character_out_of_play: bool = False
    unclaimed: bool = False


@dataclass(frozen=True)
class InviteView:
    """An active join code the owner can share or deactivate."""

    id: int
    code: str
    label: str
    created_at: datetime


@dataclass(frozen=True)
class InviteDetails:
    """What a join code stands for on the page that asks the player to accept it."""

    campaign: Campaign
    owner_name: str
    label: str = ""


def _campaign(db, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    return campaign


def _member_row(db, campaign_id: int, user_id: int) -> CampaignMember | None:
    return db.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.user_id == user_id,
        )
    ).first()


def _active_member_row(db, campaign_id: int, user_id: int) -> CampaignMember | None:
    row = _member_row(db, campaign_id, user_id)
    return row if row is not None and row.removed_at is None else None


def is_member(engine: Engine, user_id: int, campaign_id: int) -> bool:
    with session_scope(engine) as db:
        return _active_member_row(db, campaign_id, user_id) is not None


def is_removed(engine: Engine, user_id: int, campaign_id: int) -> bool:
    """Whether the owner removed this account, so an active join code must not let it back in."""
    with session_scope(engine) as db:
        row = _member_row(db, campaign_id, user_id)
        return row is not None and row.removed_at is not None


def list_members(engine: Engine, campaign_id: int, state=None) -> list[MemberView]:
    """The owner first, then every member in the order they joined, with the character each
    account currently plays. Pass the already folded `state` when the caller has it, so the
    log is not replayed just to find the characters."""
    with session_scope(engine) as db:
        campaign = _campaign(db, campaign_id)
        rows = db.exec(
            select(CampaignMember, User)
            .join(User, User.id == CampaignMember.user_id)
            .where(
                CampaignMember.campaign_id == campaign_id,
                CampaignMember.removed_at.is_(None),
            )
            .order_by(CampaignMember.created_at, CampaignMember.id)
        ).all()
        if state is None:
            state = load_state(db, campaign_id)
        owner_character = state.player_character(campaign.owner_id)
        members = [
            MemberView(
                user_id=campaign.owner_id,
                display_name=_user_name(db, campaign.owner_id),
                owner=True,
                character=owner_character.name if owner_character is not None else "",
                character_out_of_play=bool(owner_character and owner_character.out_of_play),
                unclaimed=bool(owner_character and owner_character.unclaimed),
            )
        ]
        for member, user in rows:
            character = state.player_character(member.user_id)
            members.append(
                MemberView(
                    user_id=user.id or 0,
                    display_name=public_name(user),
                    joined_at=member.created_at,
                    character=character.name if character is not None else "",
                    character_out_of_play=bool(character and character.out_of_play),
                    unclaimed=bool(character and character.unclaimed),
                )
            )
        return members


def list_removed_members(engine: Engine, campaign_id: int, state=None) -> list[MemberView]:
    """Accounts the owner removed, newest removal first, so the owner can invite one back."""
    with session_scope(engine) as db:
        rows = db.exec(
            select(CampaignMember, User)
            .join(User, User.id == CampaignMember.user_id)
            .where(
                CampaignMember.campaign_id == campaign_id,
                CampaignMember.removed_at.is_not(None),
            )
            .order_by(CampaignMember.removed_at.desc(), CampaignMember.id.desc())
        ).all()
        if state is None:
            state = load_state(db, campaign_id)
        removed: list[MemberView] = []
        for member, user in rows:
            character = state.player_character(member.user_id)
            removed.append(
                MemberView(
                    user_id=user.id or 0,
                    display_name=public_name(user),
                    joined_at=member.created_at,
                    character=character.name if character is not None else "",
                    character_out_of_play=bool(character and character.out_of_play),
                )
            )
        return removed


def _user_name(db, user_id: int) -> str:
    user = db.get(User, user_id)
    return public_name(user) if user is not None else f"account {user_id}"


def list_invites(engine: Engine, campaign_id: int) -> list[InviteView]:
    """The active join codes, newest first. Deactivated codes stay in the table so their labels
    and creation times are not lost, but the owner no longer sees them."""
    with session_scope(engine) as db:
        rows = db.exec(
            select(CampaignInvite)
            .where(
                CampaignInvite.campaign_id == campaign_id,
                CampaignInvite.active.is_(True),
            )
            .order_by(CampaignInvite.created_at.desc(), CampaignInvite.id.desc())
        ).all()
        return [
            InviteView(id=row.id or 0, code=row.code, label=row.label, created_at=row.created_at)
            for row in rows
        ]


def create_invite(
    engine: Engine, owner_user_id: int, campaign_id: int, label: str = ""
) -> CampaignInvite:
    """Make a shareable join code for the campaign. Only the owner may."""
    clean = (label or "").strip()
    if len(clean) > MAX_INVITE_LABEL:
        raise InvalidInput(f"invite label must be at most {MAX_INVITE_LABEL} characters")
    with session_scope(engine) as db:
        campaign = _campaign(db, campaign_id)
        if campaign.owner_id != owner_user_id:
            raise NotFound("campaign not found")
        invite = CampaignInvite(
            campaign_id=campaign_id,
            code=_new_code(db),
            label=clean,
            created_by=owner_user_id,
            created_at=utcnow(),
        )
        db.add(invite)
        db.commit()
        db.refresh(invite)
        return invite


def _new_code(db) -> str:
    """A URL-safe code that no campaign has used."""
    for _attempt in range(10):
        code = secrets.token_urlsafe(16)
        if db.exec(select(CampaignInvite.id).where(CampaignInvite.code == code)).first() is None:
            return code
    raise InvalidInput("could not make a join code; try again")


def deactivate_invite(engine: Engine, owner_user_id: int, campaign_id: int, invite_id: int) -> None:
    """Stop a code from letting anyone else in. Existing members stay at the table."""
    with session_scope(engine) as db:
        campaign = _campaign(db, campaign_id)
        if campaign.owner_id != owner_user_id:
            raise NotFound("campaign not found")
        invite = db.get(CampaignInvite, invite_id)
        if invite is None or invite.campaign_id != campaign_id:
            raise NotFound("join code not found")
        invite.active = False
        db.add(invite)
        db.commit()


def invite_details(engine: Engine, code: str) -> InviteDetails | None:
    """The campaign a join code opens, for the page that asks a player to accept it."""
    with session_scope(engine) as db:
        invite = db.exec(
            select(CampaignInvite).where(
                CampaignInvite.code == code, CampaignInvite.active.is_(True)
            )
        ).first()
        if invite is None:
            return None
        campaign = db.get(Campaign, invite.campaign_id)
        if campaign is None:
            return None
        return InviteDetails(
            campaign=campaign,
            owner_name=_user_name(db, campaign.owner_id),
            label=invite.label,
        )


def accept_invite(engine: Engine, user_id: int, code: str) -> Campaign:
    """Join the campaign the code names. An owner or an existing member is already at the
    table, so accepting again changes nothing."""
    with session_scope(engine) as db:
        invite = db.exec(
            select(CampaignInvite).where(
                CampaignInvite.code == code, CampaignInvite.active.is_(True)
            )
        ).first()
        if invite is None:
            raise NotFound("that join link is not valid")
        campaign = _campaign(db, invite.campaign_id)
        row = _member_row(db, campaign.id or 0, user_id)
        if campaign.owner_id == user_id or (row is not None and row.removed_at is None):
            return campaign
        if row is not None:
            # The owner removed this account. An active link must not undo that.
            raise InvalidInput(
                "the campaign owner removed this account; ask them to invite you again"
            )
        db.add(CampaignMember(campaign_id=campaign.id, user_id=user_id, created_at=utcnow()))
        try:
            db.commit()
        except IntegrityError:
            # Two tabs accepted the same code at once; the membership exists either way.
            db.rollback()
        db.refresh(campaign)
        return campaign


def add_member(engine: Engine, campaign_id: int, user_id: int) -> None:
    """Put an account at the table without a join code. The debug panel uses this to set up
    another player quickly; normal joining goes through a code."""
    with session_scope(engine) as db:
        campaign = _campaign(db, campaign_id)
        row = _member_row(db, campaign_id, user_id)
        if campaign.owner_id == user_id:
            return
        if row is not None:
            # Adding the account back clears the removal block left by `remove_member`.
            row.removed_at = None
            db.add(row)
            db.commit()
            return
        db.add(CampaignMember(campaign_id=campaign_id, user_id=user_id, created_at=utcnow()))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()


def remove_member(
    engine: Engine,
    owner_user_id: int,
    campaign_id: int,
    member_user_id: int,
    *,
    retire_character: bool,
) -> dict[str, str]:
    """Take a player off the campaign. The owner chooses whether their character leaves the
    story with them or stays as a character the whole table shares. Refused while that player's
    reply is open, because the reply belongs to them."""
    from cygnusloom.services import campaigns as campaigns_service

    with campaigns_service._exclusive(campaign_id), session_scope(engine) as db:
        campaign = _campaign(db, campaign_id)
        if campaign.owner_id != owner_user_id:
            raise NotFound("campaign not found")
        if member_user_id == campaign.owner_id:
            raise InvalidInput("the owner cannot be removed from their own campaign")
        row = _member_row(db, campaign_id, member_user_id)
        if row is None:
            raise NotFound("that account is not at this campaign's table")
        state = load_state(db, campaign_id)
        if state.turn is not None and state.turn.player_id == member_user_id:
            raise InvalidInput(
                "that player's reply is still open; let it finish or discard it before removing them"
            )
        character = state.player_character(member_user_id)
        outcome = ""
        if character is not None:
            # If the character is in the middle of an encounter turn, end that turn before the
            # character changes hands, so the fight does not wait on a player who has left.
            acting = state.encounter.acting if state.encounter is not None else None
            if acting is not None and name_key(acting) == name_key(character.name):
                from cygnusloom.services import encounters

                encounters.end_turn(db, campaign_id, character.name, by_player=True)
            if retire_character:
                record(
                    db,
                    campaign_id,
                    CharacterRetired(name=character.name, cause="their player left the campaign"),
                )
                # A retired character must not hold a place in an encounter the table is
                # still fighting through.
                if state.encounter is not None and any(
                    name_key(name) == name_key(character.name) for name in state.encounter.order
                ):
                    from cygnusloom.services import encounters

                    encounters.remove_participant(db, campaign_id, character.name)
                outcome = "retired"
            else:
                record(db, campaign_id, PcUnclaimed(name=character.name))
                outcome = "unclaimed"
        # Keep the row as a block: an active join code must not let the removed account back
        # in. The debug add-player control, or an owner adding them back, clears it.
        row.removed_at = utcnow()
        db.add(row)
        db.commit()
        return {"removed": str(member_user_id), "character": outcome}


def merge_debug_member(engine: Engine, owner_user_id: int, campaign_id: int, email: str) -> User:
    """Debug helper: put the account with this email at the table at once, creating a local test
    account when no account has that email. The debug panel only offers this when enabled."""
    from cygnusloom.services import campaigns as campaigns_service
    from cygnusloom.services.accounts import get_or_create_test_user, normalize_email

    address = normalize_email(email)
    campaign = campaigns_service.get_owned_campaign(engine, owner_user_id, campaign_id)
    with session_scope(engine) as db:
        found = db.exec(select(User).where(User.email == address)).first()
    user = found if found is not None else get_or_create_test_user(engine, address)
    if user.id is None:
        raise InvalidInput("that account could not be added")
    add_member(engine, campaign.id, user.id)
    return user
