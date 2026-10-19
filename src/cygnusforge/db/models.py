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
    """A chat owned by one account. The model is chosen per message.

    `world_template` and `rule_set` name directories under the bundled content.
    `context_summary` stands in for every message up to and including `summary_through`
    when the chat is too long to send whole; the transcript itself is never deleted."""

    __tablename__ = "campaign"

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    name: str = Field(max_length=120)
    world_template: str = Field(max_length=64)
    rule_set: str = Field(max_length=64)
    context_summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    summary_through: int = Field(default=0)
    created_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    __tablename__ = "message"

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    role: str = Field(max_length=16)
    content: str = Field(sa_column=Column(Text, nullable=False))
    model: str | None = Field(default=None, max_length=120)
    # A JSON list of player-facing lines about what happened during the reply.
    activity: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def activity_lines(self) -> list[str]:
        return json.loads(self.activity)


class TurnContext(SQLModel, table=True):
    """The prompt behind one assistant message, as a JSON list of context parts.

    Kept apart from `message` so transcript pages do not load every prompt."""

    __tablename__ = "turn_context"

    message_id: int = Field(foreign_key="message.id", primary_key=True)
    parts: str = Field(sa_column=Column(Text, nullable=False))
    # Null for replies sent before transcripts were recorded.
    transcript: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class PendingTurn(SQLModel, table=True):
    """A reply paused until the player answers a prompt, as a JSON turn state.

    Nothing from the turn is saved as messages until it finishes, so this row is the only
    record of a paused turn. A campaign has at most one."""

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
