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
    created_at: datetime = Field(default_factory=utcnow)
