from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine
from sqlmodel import select

from cygnusforge.content.library import ContentLibrary, ToolCall
from cygnusforge.db.models import Campaign, Message
from cygnusforge.llm.base import LLMError, Provider, complete
from cygnusforge.llm.registry import ProviderFactory
from cygnusforge.services.common import InvalidInput, NotFound, session_scope
from cygnusforge.services.tools import run_tool, tool_specs

MAX_NAME = 120
MAX_MESSAGE = 8000
MAX_TOOL_ROUNDS = 3
SYSTEM_PROMPT = "You are a helpful assistant."
SUMMARY_PROMPT = (
    "You keep the memory of a roleplaying campaign. Given the summary so far and new events, "
    "write an updated summary. Keep names, places, facts the players established, open threads, "
    "and anything the players asked to remember. Reply with the summary only."
)
# Token counts are estimates: about four characters per token.
CHARS_PER_TOKEN = 4
# Once the prompt reaches this share of the context window, older history is summarized.
COMPRESS_AT = 0.5
# After a summary, the newest messages worth up to this share of the window stay verbatim.
KEEP_RECENT = 0.1
# A summary may take up to this share of the window. Longer ones keep only their newest text.
SUMMARY_SHARE = 0.25
WORDS_PER_TOKEN = 0.75
SUMMARY_HEADING = "Summary so far:"


def create_campaign(
    engine: Engine,
    library: ContentLibrary,
    owner_id: int,
    name: str,
    world_template: str,
    rule_set: str,
) -> Campaign:
    name = (name or "").strip()
    if not name or len(name) > MAX_NAME:
        raise InvalidInput(f"name must be 1 to {MAX_NAME} characters")
    if world_template not in library.world_templates:
        raise InvalidInput("that world template is not available")
    if rule_set not in library.rule_sets:
        raise InvalidInput("that rule set is not available")
    with session_scope(engine) as db:
        campaign = Campaign(
            owner_id=owner_id,
            name=name,
            world_template=world_template,
            rule_set=rule_set,
        )
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


def system_prompt(rules: Iterable[str], summary: str) -> str:
    parts = [SYSTEM_PROMPT]
    rule_lines = [f"- {rule}" for rule in rules]
    if rule_lines:
        parts.append("Rules for this campaign:\n" + "\n".join(rule_lines))
    if summary:
        parts.append("Summary of earlier events in this campaign:\n" + summary)
    return "\n\n".join(parts)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class ContextUsage:
    """Tokens the next prompt would start with, against the window size."""

    used: int
    limit: int

    @property
    def summary_at(self) -> int:
        return int(self.limit * COMPRESS_AT)


def context_usage(
    campaign: Campaign, history: Sequence[Message], rules: Iterable[str], limit: int
) -> ContextUsage:
    unsummarized = [m for m in history if m.id > campaign.summary_through]
    used = estimate_tokens(system_prompt(rules, campaign.context_summary))
    used += sum(estimate_tokens(m.content) for m in unsummarized)
    return ContextUsage(used=used, limit=limit)


def _verbatim_start(history: Sequence[Message], budget: int) -> int:
    """Index of the first message in the newest run that fits within `budget` tokens."""
    used = 0
    start = len(history)
    for index in range(len(history) - 1, -1, -1):
        cost = estimate_tokens(history[index].content)
        if used + cost > budget:
            break
        used += cost
        start = index
    return start


def _keep_newest(text: str, budget_tokens: int) -> str:
    """Keep the newest text that fits the budget, starting at a word boundary."""
    limit = budget_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    kept = text[-limit:]
    first_space = kept.find(" ")
    return kept[first_space + 1 :] if first_space != -1 else kept


async def _summarize(
    provider: Provider,
    model_name: str,
    previous: str,
    messages: Sequence[Message],
    budget_tokens: int,
) -> str:
    transcript = "\n".join(f"{m.role}: {m.content}" for m in messages)
    words = int(budget_tokens * WORDS_PER_TOKEN)
    prompt = [
        {"role": "system", "content": f"{SUMMARY_PROMPT} Keep it under {words} words."},
        {
            "role": "user",
            "content": f"{SUMMARY_HEADING}\n{previous or '(none)'}\n\nNew events:\n{transcript}",
        },
    ]
    summary = (await complete(provider, model_name, prompt)).strip()
    # Models often repeat the heading they were given. It is not part of the summary.
    if summary.startswith(SUMMARY_HEADING):
        summary = summary[len(SUMMARY_HEADING) :].strip()
    if not summary:
        raise LLMError("the model returned an empty summary")
    return _keep_newest(summary, budget_tokens)


async def _reply_with_tools(
    provider: Provider,
    model_name: str,
    messages: list[dict[str, Any]],
    tool_calls: Mapping[str, ToolCall],
) -> str:
    """Let the GM call tools until it gives a reply. Tool calls and results stay in this turn."""
    specs = tool_specs(tool_calls)
    for _ in range(MAX_TOOL_ROUNDS + 1):
        result = await provider.chat(model_name, messages, specs)
        if not result.tool_requests:
            reply = result.text.strip()
            if not reply:
                raise LLMError("the model returned an empty reply")
            return reply
        messages.append(
            {
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": [
                    {
                        "id": request.id,
                        "type": "function",
                        "function": {"name": request.name, "arguments": request.arguments},
                    }
                    for request in result.tool_requests
                ],
            }
        )
        for request in result.tool_requests:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": request.id,
                    "content": run_tool(tool_calls, request),
                }
            )
    raise LLMError("the model kept calling tools without answering")


async def send_message(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    text: str,
    model: str,
) -> str:
    """Send a user message to `model` and return its reply.

    When the prompt reaches half of `context_tokens`, older history is folded into the
    campaign's summary first. Everything is saved only after the model answers, so a failed
    turn leaves no trace and the user can retry."""
    text = (text or "").strip()
    if not text:
        raise InvalidInput("message cannot be empty")
    if len(text) > MAX_MESSAGE:
        raise InvalidInput(f"message must be at most {MAX_MESSAGE} characters")
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    campaign = get_campaign(engine, owner_id, campaign_id)
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        raise InvalidInput("that campaign's rule set is no longer available")
    rules = list(rule_set.rules.values())
    provider, model_name = providers(model)

    summary = campaign.context_summary
    watermark = campaign.summary_through
    history = [m for m in list_messages(engine, campaign.id) if m.id > watermark]
    prompt_tokens = estimate_tokens(system_prompt(rules, summary)) + estimate_tokens(text)
    prompt_tokens += sum(estimate_tokens(m.content) for m in history)
    if prompt_tokens >= context_tokens * COMPRESS_AT:
        start = _verbatim_start(history, int(context_tokens * KEEP_RECENT))
        if start > 0:
            folded = history[:start]
            summary = await _summarize(
                provider, model_name, summary, folded, int(context_tokens * SUMMARY_SHARE)
            )
            watermark = folded[-1].id or watermark
            history = history[start:]

    messages = [
        {"role": "system", "content": system_prompt(rules, summary)},
        *({"role": m.role, "content": m.content} for m in history),
        {"role": "user", "content": text},
    ]
    reply = await _reply_with_tools(provider, model_name, messages, rule_set.tool_calls)
    with session_scope(engine) as db:
        stored = db.get(Campaign, campaign.id)
        if stored is None:
            raise NotFound("campaign not found")
        stored.context_summary = summary
        stored.summary_through = watermark
        db.add(Message(campaign_id=campaign.id, role="user", content=text))
        db.add(Message(campaign_id=campaign.id, role="assistant", content=reply, model=model))
        db.commit()
    return reply
