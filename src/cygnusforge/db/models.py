from datetime import UTC, datetime

from sqlalchemy import Column, Text, UniqueConstraint
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
    be switched at any time. It is configuration, not part of the log.

    `world_template` and `rule_set` name directories under the bundled content."""

    __tablename__ = "campaign"

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    name: str = Field(max_length=120)
    world_template: str = Field(max_length=64)
    rule_set: str = Field(max_length=64)
    model: str = Field(max_length=120)
    created_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    """One entry in a campaign's log of what happened. Events are only appended, or removed
    from the end by a rollback. See `cygnusforge.services.events`.

    Ids are never reused, so a rolled-back event cannot be mistaken for a later one. `seq`
    numbers the events of one campaign from 1 without gaps. The unique constraint makes a second
    writer fail instead of interleaving its events."""

    __tablename__ = "event"
    __table_args__ = (UniqueConstraint("campaign_id", "seq"), {"sqlite_autoincrement": True})

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    seq: int
    channel: str = Field(max_length=16)
    kind: str = Field(max_length=64)
    payload: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)
