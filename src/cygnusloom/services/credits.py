"""Credits for AI GM replies, measured token usage, and the append-only balance ledger.

A campaign owner pays a flat price per completed AI GM reply, set on the model
(`LlmModel.credits_per_turn`). Administrators, the single-player account, and members of the
fixed `cygnusloom-credit-exempt` role are exempt unless a debug campaign forces charges. Payment
is not handled here; an administrator grants credits.

Every model call is measured. When the provider reports exact usage it is stored as is; otherwise
the meter estimates from the text so the system still works with providers that omit usage. The
stored per-reply totals are what an administrator reads to choose a model's turn price.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, case, func, select, update
from sqlmodel import Session

from cygnusloom.auth.claims import has_admin_role
from cygnusloom.config import DEFAULT_ADMIN_VALUES, Settings
from cygnusloom.db.models import Campaign, CreditEntry, LlmModel, LlmUsage, User, utcnow
from cygnusloom.llm.base import ChatReply, Provider, TextDelta
from cygnusloom.services.accounts import user_roles
from cygnusloom.services.common import InvalidInput, NotFound, session_scope
from cygnusloom.services.state import estimate_tokens

TURN_REASON = "turn"
REFUND_REASON = "refund"
ADMIN_REASON = "admin"
DEBUG_REASON = "debug"
MAX_BALANCE = 1_000_000_000
MAX_TURN_PRICE = 1_000_000
# Fixed by the bundled Authentik blueprint, unlike the configurable administrator roles.
CREDIT_EXEMPT_ROLE = "cygnusloom-credit-exempt"


def validate_credits(value: int, *, label: str = "credits") -> int:
    """A whole, non-negative credit amount that fits the database and the UI."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_BALANCE:
        raise InvalidInput(f"{label} must be a whole number from 0 to {MAX_BALANCE}")
    return value


def validate_turn_price(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TURN_PRICE:
        raise InvalidInput(f"credits per turn must be a whole number from 0 to {MAX_TURN_PRICE}")
    return value


def model_price(engine: Engine, model_id: str) -> int:
    """The flat price of one completed AI GM reply with `provider/model`, or 0 when unset."""
    provider, separator, model = model_id.partition("/")
    if not separator or not provider or not model:
        return 0
    with session_scope(engine) as db:
        price = db.execute(
            select(LlmModel.credits_per_turn).where(
                LlmModel.provider == provider, LlmModel.model == model
            )
        ).scalar_one_or_none()
    return int(price or 0)


def balance(engine: Engine, user_id: int) -> int:
    with session_scope(engine) as db:
        value = db.execute(
            select(User.credit_balance).where(User.id == user_id)
        ).scalar_one_or_none()
    return int(value or 0)


def _admin_values(settings: Settings | None) -> tuple[str, ...]:
    return settings.oidc_admin_values if settings is not None else DEFAULT_ADMIN_VALUES


def _has_credit_exempt_role(roles: Iterable[str]) -> bool:
    wanted = CREDIT_EXEMPT_ROLE.casefold()
    return any(role.casefold() == wanted for role in roles)


def is_exempt(engine: Engine, settings: Settings | None, campaign: Campaign) -> bool:
    """Administrators, single-player accounts, and accounts in the credit-exempt role are not
    charged. The role is read from the campaign owner, who pays for every reply, so joining
    someone else's campaign does not exempt it. A debug campaign can force charges so the flow
    can be tested with an exempt account."""
    if campaign.force_credit_charges:
        return False
    if settings is not None and settings.single_player:
        return True
    with session_scope(engine) as db:
        user = db.get(User, campaign.owner_id)
        if user is None:
            return False
        roles = user_roles(user)
        return has_admin_role(roles, _admin_values(settings)) or _has_credit_exempt_role(roles)


def can_afford(engine: Engine, settings: Settings | None, campaign: Campaign) -> bool:
    """Whether the campaign owner can start another reply. Free models always pass."""
    price = model_price(engine, campaign.model)
    if price <= 0 or is_exempt(engine, settings, campaign):
        return True
    return balance(engine, campaign.owner_id) >= price


def shortfall_message(engine: Engine, campaign: Campaign, price: int | None = None) -> str:
    """A player-facing explanation of why another reply cannot start."""
    price = model_price(engine, campaign.model) if price is None else price
    current = balance(engine, campaign.owner_id)
    noun = "credit" if price == 1 else "credits"
    return (
        f"This campaign needs {price} {noun} per reply with {campaign.model}, but its owner has "
        f"{current}. An administrator can add credits."
    )


@dataclass(frozen=True)
class CreditPolicy:
    """The charge decision for one run of AI GM replies. The price is snapshotted when the run
    starts, so an administrator changing it mid-turn cannot change the price the player was
    shown. Exemption is decided for the campaign owner, who pays for every reply."""

    engine: Engine
    campaign: Campaign
    payer_id: int
    price: int
    exempt: bool

    @classmethod
    def for_campaign(
        cls, engine: Engine, settings: Settings | None, campaign: Campaign
    ) -> CreditPolicy:
        price = model_price(engine, campaign.model)
        return cls(
            engine=engine,
            campaign=campaign,
            payer_id=campaign.owner_id,
            price=price,
            # A free model never needs the account lookup.
            exempt=price <= 0 or is_exempt(engine, settings, campaign),
        )

    def require(self) -> None:
        """Refuse to start a reply the owner cannot pay for. A zero price or an exempt payer is
        always allowed."""
        if self.exempt or self.price <= 0:
            return
        current = balance(self.engine, self.payer_id)
        if current < self.price:
            raise InvalidInput(shortfall_message(self.engine, self.campaign, self.price))

    def charge(self, event_id: int) -> int:
        """Deduct one reply price. Returns the amount charged."""
        if self.exempt or self.price <= 0:
            return 0
        return charge_reply(
            self.engine,
            self.payer_id,
            self.price,
            campaign_id=self.campaign.id or 0,
            model=self.campaign.model,
            event_id=event_id,
        )


def charge_reply(
    engine: Engine,
    user_id: int,
    price: int,
    *,
    campaign_id: int,
    model: str,
    event_id: int,
) -> int:
    """Deduct one completed reply from a balance and record why. A finished reply is always
    charged, even if the balance goes negative because the turn began before the price changed."""
    if price <= 0:
        return 0
    with session_scope(engine) as db:
        exists = db.execute(select(User.id).where(User.id == user_id)).scalar_one_or_none()
        if exists is None:
            raise NotFound("account not found")
        # An atomic subtract keeps two campaigns played at the same time from losing a charge.
        db.execute(
            update(User)
            .where(User.id == user_id)
            .values(credit_balance=User.credit_balance - price)
        )
        after = int(db.execute(select(User.credit_balance).where(User.id == user_id)).scalar_one())
        db.add(
            CreditEntry(
                user_id=user_id,
                campaign_id=campaign_id,
                event_id=event_id,
                model=model,
                delta=-price,
                balance_after=after,
                reason=TURN_REASON,
                note="AI GM reply",
            )
        )
        db.commit()
    return price


def set_balance(
    engine: Engine,
    user_id: int,
    value: int,
    *,
    reason: str,
    note: str = "",
    campaign_id: int | None = None,
    actor_id: int | None = None,
) -> int:
    """Set an account's balance and write the difference to the ledger. Admin and debug grants
    use this; a charge uses `charge_reply` so it keeps a link to the reply."""
    value = validate_credits(value)
    with session_scope(engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise NotFound("account not found")
        delta = value - user.credit_balance
        if delta:
            user.credit_balance = value
            db.add(user)
            db.add(
                CreditEntry(
                    user_id=user_id,
                    campaign_id=campaign_id,
                    model="",
                    delta=delta,
                    balance_after=value,
                    reason=reason,
                    note=note or (f"set by account {actor_id}" if actor_id is not None else ""),
                )
            )
            db.commit()
        else:
            db.rollback()
    return value


def refund_replies(db: Session, event_ids: Iterable[int]) -> int:
    """Return the credits charged for removed GM replies. Called inside a rollback's transaction
    so removing the replies and returning their price happen together."""
    ids = tuple(dict.fromkeys(event_ids))
    if not ids:
        return 0
    entries = (
        db.exec(
            select(CreditEntry).where(
                CreditEntry.reason == TURN_REASON, CreditEntry.event_id.in_(ids)
            )
        )
        .scalars()
        .all()
    )
    refunded = 0
    for entry in entries:
        if entry.delta >= 0:
            continue
        exists = db.execute(select(User.id).where(User.id == entry.user_id)).scalar_one_or_none()
        if exists is None:
            continue
        amount = -entry.delta
        db.execute(
            update(User)
            .where(User.id == entry.user_id)
            .values(credit_balance=User.credit_balance + amount)
        )
        after = int(
            db.execute(select(User.credit_balance).where(User.id == entry.user_id)).scalar_one()
        )
        refunded += amount
        db.add(
            CreditEntry(
                user_id=entry.user_id,
                campaign_id=entry.campaign_id,
                event_id=entry.event_id,
                model=entry.model,
                delta=amount,
                balance_after=after,
                reason=REFUND_REASON,
                note=f"refund of charge {entry.id}",
            )
        )
    db.flush()
    return refunded


@dataclass(frozen=True)
class UsageRecord:
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False


def _estimate_prompt_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, Mapping) else None
            if isinstance(function, Mapping):
                total += estimate_tokens(str(function.get("name") or ""))
                total += estimate_tokens(str(function.get("arguments") or ""))
    return total


def _estimate_reply_tokens(reply: ChatReply) -> int:
    parts = [reply.text, reply.reasoning]
    parts.extend(f"{request.name}{request.arguments}" for request in reply.tool_requests)
    text = "\n".join(part for part in parts if part)
    return estimate_tokens(text) if text.strip() else 0


def measure_usage(
    model: str, messages: Sequence[Mapping[str, Any]], reply: ChatReply
) -> UsageRecord:
    """Exact provider counts when present, otherwise about four characters per token."""
    if reply.usage is not None:
        return UsageRecord(
            model=model,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            cached_tokens=reply.usage.cached_tokens,
            reasoning_tokens=reply.usage.reasoning_tokens,
            estimated=False,
        )
    return UsageRecord(
        model=model,
        input_tokens=_estimate_prompt_tokens(messages),
        output_tokens=_estimate_reply_tokens(reply),
        estimated=True,
    )


class UsageMeter:
    """Collects each model call's usage during one run and writes it in one batch when the reply
    that used it is saved. Failed replies therefore leave no measurement, which is the same rule
    as charging: only a completed AI GM reply is billed."""

    def __init__(self, engine: Engine, campaign_id: int, user_id: int, model: str):
        self._engine = engine
        self._campaign_id = campaign_id
        self._user_id = user_id
        self._model = model
        self._pending: list[UsageRecord] = []

    def record(self, messages: Sequence[Mapping[str, Any]], reply: ChatReply) -> UsageRecord:
        measured = measure_usage(self._model, messages, reply)
        self._pending.append(measured)
        return measured

    def assign(self, event_id: int) -> None:
        """Attach the calls collected since the last reply to that reply's event."""
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        with session_scope(self._engine) as db:
            for record in pending:
                db.add(
                    LlmUsage(
                        campaign_id=self._campaign_id,
                        user_id=self._user_id,
                        reply_event_id=event_id,
                        model=record.model,
                        input_tokens=record.input_tokens,
                        output_tokens=record.output_tokens,
                        cached_tokens=record.cached_tokens,
                        reasoning_tokens=record.reasoning_tokens,
                        estimated=record.estimated,
                        created_at=utcnow(),
                    )
                )
            db.commit()


class MeteredProvider:
    """Wraps a provider so every reply is measured. The wrapper delegates unchanged, so the
    turn code sees the same stream and tool calls; only completed replies add a usage record."""

    def __init__(self, provider: Provider, meter: UsageMeter):
        self._provider = provider
        self._meter = meter
        self.name = provider.name

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        reply = await self._provider.chat(model, messages, tools)
        self._meter.record(messages, reply)
        return reply

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[TextDelta | ChatReply]:
        async for item in self._provider.stream(model, messages, tools):
            if isinstance(item, ChatReply):
                self._meter.record(messages, item)
            yield item


@dataclass(frozen=True)
class ModelUsage:
    """Per-model totals for the administrator's pricing table."""

    model: str
    calls: int
    replies: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    estimated_calls: int
    credits_charged: int

    @property
    def avg_input_tokens(self) -> int:
        return math.ceil(self.input_tokens / self.replies) if self.replies else 0

    @property
    def avg_cached_tokens(self) -> int:
        return math.ceil(self.cached_tokens / self.replies) if self.replies else 0

    @property
    def avg_output_tokens(self) -> int:
        return math.ceil(self.output_tokens / self.replies) if self.replies else 0


def usage_by_model(engine: Engine) -> tuple[ModelUsage, ...]:
    """Measured calls and completed replies per model, plus the credits actually charged. This is
    the data an administrator uses to set a model's turn price."""
    with session_scope(engine) as db:
        usage_rows = db.execute(
            select(
                LlmUsage.model,
                func.count(LlmUsage.id),
                func.count(func.distinct(LlmUsage.reply_event_id)),
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.cached_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
                func.coalesce(func.sum(LlmUsage.reasoning_tokens), 0),
                func.coalesce(func.sum(case((LlmUsage.estimated.is_(True), 1), else_=0)), 0),
            )
            .group_by(LlmUsage.model)
            .order_by(LlmUsage.model)
        ).all()
        charge_rows = db.execute(
            select(CreditEntry.model, func.coalesce(func.sum(-CreditEntry.delta), 0))
            .where(CreditEntry.reason == TURN_REASON)
            .group_by(CreditEntry.model)
        ).all()
    charges = {str(model): int(amount or 0) for model, amount in charge_rows}
    rows = []
    for model, calls, replies, input_tokens, cached, output, reasoning, estimated in usage_rows:
        name = str(model)
        rows.append(
            ModelUsage(
                model=name,
                calls=int(calls),
                replies=int(replies),
                input_tokens=int(input_tokens),
                cached_tokens=int(cached),
                output_tokens=int(output),
                reasoning_tokens=int(reasoning),
                estimated_calls=int(estimated),
                credits_charged=charges.pop(name, 0),
            )
        )
    # A model can have charges from before usage tables were populated, or a reply whose calls
    # were never measured. Keep its price visible.
    for model, charged in sorted(charges.items()):
        if charged:
            rows.append(
                ModelUsage(
                    model=model,
                    calls=0,
                    replies=0,
                    input_tokens=0,
                    cached_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=0,
                    estimated_calls=0,
                    credits_charged=charged,
                )
            )
    return tuple(rows)
