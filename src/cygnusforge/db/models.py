import json
from datetime import UTC, datetime

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    __tablename__ = "user"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True, max_length=255)
    password_hash: str = Field(max_length=255)
    created_at: datetime = Field(default_factory=utcnow)


class Campaign(SQLModel, table=True):
    """A chat owned by one account. `model` is the LLM every reply in the chat uses, and it can
    be switched at any time.

    `world_template` and `rule_set` name directories under the bundled content.
    `context_summary` stands in for every chat event up to and including the event id
    `summary_through` when the chat is too long to send whole. Both are projections of the
    campaign's event log, so rolling back restores them too."""

    __tablename__ = "campaign"

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    name: str = Field(max_length=120)
    world_template: str = Field(max_length=64)
    rule_set: str = Field(max_length=64)
    model: str = Field(max_length=120)
    context_summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    summary_through: int = Field(default=0)
    created_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    """One entry in a campaign's log of what happened. Events are only appended, or removed
    from the end by a rollback. See `cygnusforge.services.events`.

    Ids are never reused, so a rolled-back event cannot be mistaken for a later one."""

    __tablename__ = "event"
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    channel: str = Field(max_length=16)
    kind: str = Field(max_length=64)
    payload: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)


class TurnContext(SQLModel, table=True):
    """The prompt behind one GM reply event, as a JSON list of context parts.

    Kept apart from `event` so transcript pages do not load every prompt."""

    __tablename__ = "turn_context"

    event_id: int = Field(foreign_key="event.id", primary_key=True)
    parts: str = Field(sa_column=Column(Text, nullable=False))
    # Null for replies sent before transcripts were recorded.
    transcript: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class PendingTurn(SQLModel, table=True):
    """A reply paused until the player answers a prompt, as a JSON turn state.

    Its player messages are already events, but the GM's reply is not recorded until it
    finishes, so this row is the only record of the rest of a paused turn. A campaign has at
    most one."""

    __tablename__ = "pending_turn"

    campaign_id: int = Field(foreign_key="campaign.id", primary_key=True)
    state: str = Field(sa_column=Column(Text, nullable=False))


class Location(SQLModel, table=True):
    """A place in a campaign. Created from the world definition or by the GM."""

    __tablename__ = "location"

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    name: str = Field(max_length=120)
    description: str = Field(sa_column=Column(Text, nullable=False))


class Actor(SQLModel, table=True):
    """A character in a campaign: the player character (`kind` "pc") or an NPC.

    The PC has no personality, accent, or quirks. Every actor is at exactly one location."""

    __tablename__ = "actor"

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    kind: str = Field(max_length=8)
    name: str = Field(max_length=120)
    location_id: int = Field(foreign_key="location.id")
    description: str = Field(default="", sa_column=Column(Text, nullable=False))
    personality: str = Field(default="", max_length=255)
    accent: str = Field(default="", max_length=255)
    # A JSON list of short strings.
    quirks: str = Field(default="[]", sa_column=Column(Text, nullable=False))

    @property
    def quirk_list(self) -> list[str]:
        return json.loads(self.quirks)


class Note(SQLModel, table=True):
    """A fact the GM recorded. Campaign-wide when both subject ids are null."""

    __tablename__ = "note"

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    text: str = Field(sa_column=Column(Text, nullable=False))
    location_id: int | None = Field(default=None, foreign_key="location.id")
    actor_id: int | None = Field(default=None, foreign_key="actor.id")
    created_at: datetime = Field(default_factory=utcnow)
