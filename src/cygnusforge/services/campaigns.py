from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Engine
from sqlmodel import select

from cygnusforge.db.models import Campaign, Message
from cygnusforge.llm.base import LLMError, complete
from cygnusforge.llm.registry import ProviderFactory
from cygnusforge.services.common import InvalidInput, NotFound, session_scope

MAX_NAME = 120
MAX_MESSAGE = 8000
SYSTEM_PROMPT = "You are a helpful assistant."


def create_campaign(
    engine: Engine, owner_id: int, name: str, model: str, allowed_models: Iterable[str]
) -> Campaign:
    name = (name or "").strip()
    if not name or len(name) > MAX_NAME:
        raise InvalidInput(f"name must be 1 to {MAX_NAME} characters")
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    with session_scope(engine) as db:
        campaign = Campaign(owner_id=owner_id, name=name, model=model)
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


def list_campaigns(engine: Engine, owner_id: int) -> list[Campaign]:
    with session_scope(engine) as db:
        query = (
            select(Campaign)
            .where(Campaign.owner_id == owner_id)
            .order_by(Campaign.created_at.desc(), Campaign.id.desc())
        )
        return list(db.exec(query).all())


def get_campaign(engine: Engine, owner_id: int, campaign_id: int) -> Campaign:
    """Other accounts' campaigns look the same as missing ones, so nothing leaks."""
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.owner_id != owner_id:
            raise NotFound("campaign not found")
        return campaign


def list_messages(engine: Engine, campaign_id: int) -> list[Message]:
    with session_scope(engine) as db:
        query = select(Message).where(Message.campaign_id == campaign_id).order_by(Message.id)
        return list(db.exec(query).all())


async def send_message(
    engine: Engine, providers: ProviderFactory, owner_id: int, campaign_id: int, text: str
) -> str:
    """Send a user message and return the model's reply.

    Both messages are saved only after the model answers, so a failed turn leaves no trace
    and the user can retry."""
    text = (text or "").strip()
    if not text:
        raise InvalidInput("message cannot be empty")
    if len(text) > MAX_MESSAGE:
        raise InvalidInput(f"message must be at most {MAX_MESSAGE} characters")
    campaign = get_campaign(engine, owner_id, campaign_id)
    history = [{"role": m.role, "content": m.content} for m in list_messages(engine, campaign.id)]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": text},
    ]
    provider, model = providers(campaign.model)
    reply = await complete(provider, model, messages)
    if not reply:
        raise LLMError("the model returned an empty reply")
    with session_scope(engine) as db:
        db.add(Message(campaign_id=campaign.id, role="user", content=text))
        db.add(Message(campaign_id=campaign.id, role="assistant", content=reply))
        db.commit()
    return reply
