from datetime import UTC, datetime

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    """An account mirrored from the identity provider. Cygnus Loom never stores credentials.

    The provider's issuer and subject identify the account; email is private and only
    administrators see it. `display_name` is the player-chosen name other players and the GM
    see; it is seeded from the provider's name or generated from random words. `roles` is a
    JSON list snapshotted from the ID token at the last sign-in."""

    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)

    id: int | None = Field(default=None, primary_key=True)
    issuer: str = Field(default="", index=True, max_length=255)
    subject: str = Field(default="", max_length=255)
    email: str = Field(default="", index=True, max_length=255)
    email_verified: bool = Field(default=False)
    display_name: str = Field(default="", max_length=120)
    # A view preference, not campaign state: whether this account sees the GM's hidden Log lines
    # and can open the Context and Transcript of each reply.
    show_internal_thinking: bool = Field(default=False)
    roles: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    # Credits a player spends on AI GM replies. Administrators, the single-player account, and
    # credit-exempt accounts are exempt, so the balance is informational for them unless a debug
    # campaign forces charges.
    credit_balance: int = Field(default=0)
    disabled: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = Field(default=None)


class LlmModel(SQLModel, table=True):
    """A language model an administrator made available. `provider` and `model` form the id
    `provider/model` used by campaigns. API keys are encrypted with the application secret.

    `credits_per_turn` is the average price of one completed AI GM reply with this model. It is
    shown in the campaign's model picker and deducted from the campaign owner's balance.
    `stream_usage` asks the provider for token counts in streamed replies; turn it off for an
    OpenAI-compatible server that rejects the `stream_options` request field.
    `calculator_json` holds the model's saved cost-calculator token prices and optional peak
    hours as JSON; it is configuration for administrators, not part of gameplay."""

    __tablename__ = "llm_model"
    __table_args__ = (UniqueConstraint("provider", "model"),)

    id: int | None = Field(default=None, primary_key=True)
    provider: str = Field(index=True, max_length=64)
    model: str = Field(max_length=120)
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", sa_column=Column(Text, nullable=False))
    credits_per_turn: int = Field(default=0)
    stream_usage: bool = Field(default=True)
    calculator_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    enabled: bool = Field(default=True)
    is_default: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)


class LlmUsage(SQLModel, table=True):
    """Measured token use for one model call. Rows are grouped by `reply_event_id` so an
    administrator can see what a completed AI GM reply actually costs. Usage is kept for exempt
    accounts too, which makes it the source for choosing each model's `credits_per_turn`.
    User and campaign ids are snapshots: deleting either does not erase the measurement."""

    __tablename__ = "llm_usage"

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(index=True)
    user_id: int = Field(index=True)
    reply_event_id: int | None = Field(default=None, index=True)
    model: str = Field(default="", index=True, max_length=120)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cached_tokens: int = Field(default=0)
    reasoning_tokens: int = Field(default=0)
    # True when the provider reported no usage and the counts were estimated from text.
    estimated: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)


class CreditEntry(SQLModel, table=True):
    """An append-only change to an account's credit balance. `balance_after` makes the history
    readable even after later charges, and `event_id` links a turn charge to the GM reply it paid
    for so a rollback can refund it. User and campaign ids are snapshots."""

    __tablename__ = "credit_entry"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    campaign_id: int | None = Field(default=None, index=True)
    event_id: int | None = Field(default=None, index=True)
    model: str = Field(default="", max_length=120)
    delta: int
    balance_after: int
    reason: str = Field(max_length=32)
    note: str = Field(default="", sa_column=Column(Text, nullable=False))
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
    JSON list of genres the player narrowed the generation tables to at creation; blank means the
    rule set's own choice. `force_credit_charges` is a debug-panel switch that charges an
    otherwise exempt account in this campaign. `use_inventory_space` is a per-campaign switch, on
    by default, for the rule set's slot limits; turning it off lets characters carry anything
    reasonable."""

    __tablename__ = "campaign"

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    name: str = Field(max_length=120)
    world_template: str = Field(max_length=64)
    rule_set: str = Field(max_length=64)
    model: str = Field(max_length=120)
    setting: str = Field(default="", sa_column=Column(Text, nullable=False))
    table_genres: str = Field(default="", sa_column=Column(Text, nullable=False))
    force_credit_charges: bool = Field(default=False)
    use_inventory_space: bool = Field(default=True)
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
