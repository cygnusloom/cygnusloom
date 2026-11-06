from datetime import UTC, datetime

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    """An account mirrored from the identity provider. Cygnus Loom never stores credentials.

    The provider's issuer and subject identify the account; email is only a label and can
    change. `roles` is a JSON list snapshotted from the ID token at the last sign-in."""

    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)

    id: int | None = Field(default=None, primary_key=True)
    issuer: str = Field(default="", index=True, max_length=255)
    subject: str = Field(default="", max_length=255)
    email: str = Field(default="", index=True, max_length=255)
    email_verified: bool = Field(default=False)
    display_name: str = Field(default="", max_length=120)
    roles: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    disabled: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = Field(default=None)


class LlmModel(SQLModel, table=True):
    """A language model an administrator made available. `provider` and `model` form the id
    `provider/model` used by campaigns. API keys are encrypted with the application secret."""

    __tablename__ = "llm_model"
    __table_args__ = (UniqueConstraint("provider", "model"),)

    id: int | None = Field(default=None, primary_key=True)
    provider: str = Field(index=True, max_length=64)
    model: str = Field(max_length=120)
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", sa_column=Column(Text, nullable=False))
    enabled: bool = Field(default=True)
    is_default: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)


class UserSession(SQLModel, table=True):
    """A server-side login session. The cookie holds only this opaque id, so an administrator
    can revoke it and sign-in state survives without trusting a large signed cookie."""

    __tablename__ = "user_session"

    id: str = Field(primary_key=True, max_length=64)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    auth_time: datetime = Field(default_factory=utcnow)
    ip: str = Field(default="", max_length=64)
    user_agent: str = Field(default="", max_length=255)
    # Presented to the identity provider as id_token_hint when this session signs out.
    oidc_id_token: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class AdminAudit(SQLModel, table=True):
    """One administrator action. Actor and target are snapshots, so the record survives a
    deleted account. This table is append-only: there is no update or delete route."""

    __tablename__ = "admin_audit"

    id: int | None = Field(default=None, primary_key=True)
    actor_id: int = Field(index=True)
    actor_email: str = Field(default="", max_length=255)
    action: str = Field(max_length=80)
    target_type: str = Field(max_length=32)
    target_id: str = Field(default="", max_length=64)
    details: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)
    ip: str = Field(default="", max_length=64)


class Campaign(SQLModel, table=True):
    """A chat owned by one account. `name` is the campaign's label: the name the campaign list
    shows, which the player can change. `model` is the LLM every reply in the chat uses, and it
    can be switched at any time. It is configuration, not part of the log.

    `world_template` and `rule_set` name directories under the bundled content. `setting` is the
    optional free-form setting or story the player asked for at creation; it reaches the GM's
    prompt every turn and is blank when the player left it out. `table_genres` is the optional
    JSON list of genres the player narrowed the generation tables to at creation; blank means
    the rule set's own choice."""

    __tablename__ = "campaign"

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    name: str = Field(max_length=120)
    world_template: str = Field(max_length=64)
    rule_set: str = Field(max_length=64)
    model: str = Field(max_length=120)
    setting: str = Field(default="", sa_column=Column(Text, nullable=False))
    table_genres: str = Field(default="", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)


class CampaignMember(SQLModel, table=True):
    """A player an owner shared a campaign with. The owner is implicit and never needs a row.
    A membership gives the account access to play; it is configuration, not part of the log, so
    removing it takes the account's access without changing the story. A removed row is kept with
    `removed_at` set, so an active join code cannot let that account walk back in."""

    __tablename__ = "campaign_member"
    __table_args__ = (UniqueConstraint("campaign_id", "user_id"),)

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    removed_at: datetime | None = Field(default=None)


class CampaignInvite(SQLModel, table=True):
    """A shareable join code for a campaign. A code stays usable by any signed-in account until
    the owner deactivates it; deactivating it never removes an existing member."""

    __tablename__ = "campaign_invite"

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    code: str = Field(index=True, unique=True, max_length=64)
    label: str = Field(default="", max_length=120)
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    active: bool = Field(default=True)


class Event(SQLModel, table=True):
    """One entry in a campaign's log of what happened. Events are only appended, or removed
    from the end by a rollback. See `cygnusloom.services.events`.

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
