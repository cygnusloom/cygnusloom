from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine
from sqlmodel import select

from cygnusforge.content.library import ContentLibrary, RuleSet, ToolCall
from cygnusforge.db.models import Campaign, Message, TurnContext
from cygnusforge.llm.base import ChatReply, LLMError, Provider, TextDelta, complete
from cygnusforge.llm.registry import ProviderFactory
from cygnusforge.services.common import InvalidInput, NotFound, session_scope
from cygnusforge.services.tools import run_tool, tool_specs

MAX_NAME = 120
MAX_MESSAGE = 8000
MAX_TOOL_ROUNDS = 3
SYSTEM_PROMPT = "You are a helpful assistant."
DECIDE_INSTRUCTION = (
    "Decision step. Decide whether this action needs a roll or another tool, and call the tool "
    "if it does. Do not narrate here; the narration comes in the next step. If no tool is "
    "needed, reply with one short sentence saying so."
)
NARRATE_INSTRUCTION = (
    "Narration step. Write what the player sees next, in a few sentences (guidance, not a hard "
    "limit). Base the outcome on the decision and any tool results above. Do not call tools."
)
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


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class ContextPart:
    """One piece of what the model was given: its role, where it came from, and its text."""

    role: str
    source: str
    content: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content)

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "source": self.source, "content": self.content}


def system_parts(
    rule_set_id: str, rules: Mapping[str, str], summary: str, summary_through: int
) -> list[ContextPart]:
    """The system prompt in sections. Joined with blank lines, they are the system message."""
    parts = [ContextPart("system", "Base prompt", SYSTEM_PROMPT)]
    if rules:
        rule_lines = "\n".join(f"- {rule}" for rule in rules.values())
        ids = ", ".join(rules)
        parts.append(
            ContextPart(
                "system",
                f"Rules from rule set '{rule_set_id}': {ids}",
                "Rules for this campaign:\n" + rule_lines,
            )
        )
    if summary:
        parts.append(
            ContextPart(
                "system",
                f"Summary of messages up to #{summary_through}",
                "Summary of earlier events in this campaign:\n" + summary,
            )
        )
    return parts


def tool_definition_parts(rule_set: RuleSet) -> list[ContextPart]:
    """Tool definitions are sent beside the messages, so they are recorded as their own parts."""
    specs = tool_specs(rule_set.tool_calls) or []
    return [
        ContextPart(
            "tool definition",
            f"Tool '{spec['function']['name']}' from rule set '{rule_set.id}'",
            json.dumps(spec["function"], indent=2),
        )
        for spec in specs
    ]


def _message_source(message: Message) -> str:
    if message.role == "user":
        return f"Message #{message.id} (player)"
    return f"Message #{message.id} (GM, {message.model or 'unknown model'})"


@dataclass(frozen=True)
class ContextUsage:
    """Tokens the next prompt would start with, against the window size."""

    used: int
    limit: int

    @property
    def summary_at(self) -> int:
        return int(self.limit * COMPRESS_AT)


def context_usage(
    campaign: Campaign, history: Sequence[Message], rules: Mapping[str, str], limit: int
) -> ContextUsage:
    unsummarized = [m for m in history if m.id > campaign.summary_through]
    used = sum(
        part.tokens
        for part in system_parts(
            campaign.rule_set, rules, campaign.context_summary, campaign.summary_through
        )
    )
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
    record: list[ContextPart],
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
    record.extend(
        [
            ContextPart("user", "Summary request", "\n\n".join(m["content"] for m in prompt)),
            ContextPart("assistant", "Summary reply", summary),
        ]
    )
    # Models often repeat the heading they were given. It is not part of the summary.
    if summary.startswith(SUMMARY_HEADING):
        summary = summary[len(SUMMARY_HEADING) :].strip()
    if not summary:
        raise LLMError("the model returned an empty summary")
    return _keep_newest(summary, budget_tokens)


@dataclass(frozen=True)
class Status:
    """What the GM is doing now, shown while the reply is pending."""

    text: str


@dataclass(frozen=True)
class Delta:
    """A piece of the GM's reply text, as it arrives."""

    text: str


@dataclass(frozen=True)
class Saved:
    """The turn is stored. `message_id` is the GM's reply."""

    message_id: int


TurnEvent = Status | Delta | Saved


@dataclass(frozen=True)
class _Final:
    text: str


def _add_instruction(
    messages: list[dict[str, Any]], context: list[ContextPart], source: str, text: str
) -> None:
    messages.append({"role": "system", "content": text})
    context.append(ContextPart("system", source, text))


async def _reply_with_tools(
    provider: Provider,
    model_name: str,
    messages: list[dict[str, Any]],
    context: list[ContextPart],
    tool_calls: Mapping[str, ToolCall],
) -> AsyncIterator[TurnEvent | _Final]:
    """Reply in two phases. First the GM decides, calling tools until it needs none, and its text
    is not streamed. Then it narrates the outcome, and only that text is streamed.

    Every message kept for the model is appended to `messages` and recorded in `context`. The
    decide instruction is sent with each decision call but not kept. The last item is always the
    `_Final` reply."""
    specs = tool_specs(tool_calls)
    decide = {"role": "system", "content": DECIDE_INSTRUCTION}
    context.append(ContextPart("system", "Decide instruction", DECIDE_INSTRUCTION))
    for round_number in range(1, MAX_TOOL_ROUNDS + 2):
        yield Status("Deciding...")
        decision = await provider.chat(model_name, [*messages, decide], specs)
        if not decision.tool_requests:
            if decision.text.strip():
                messages.append({"role": "assistant", "content": decision.text})
                context.append(
                    ContextPart("assistant", f"GM decision (round {round_number})", decision.text)
                )
            break
        messages.append(
            {
                "role": "assistant",
                "content": decision.text or None,
                "tool_calls": [
                    {
                        "id": request.id,
                        "type": "function",
                        "function": {"name": request.name, "arguments": request.arguments},
                    }
                    for request in decision.tool_requests
                ],
            }
        )
        if decision.text:
            context.append(
                ContextPart("assistant", f"GM text (round {round_number})", decision.text)
            )
        for request in decision.tool_requests:
            yield Status(f"Running {request.name}...")
            output = run_tool(tool_calls, request)
            messages.append({"role": "tool", "tool_call_id": request.id, "content": output})
            context.append(
                ContextPart(
                    "assistant",
                    f"GM tool call (round {round_number})",
                    f"{request.name}({request.arguments})",
                )
            )
            context.append(
                ContextPart("tool", f"Result of '{request.name}' (round {round_number})", output)
            )
    else:
        raise LLMError("the model kept calling tools without answering")

    _add_instruction(messages, context, "Narrate instruction", NARRATE_INSTRUCTION)
    yield Status("Narrating...")
    narration: ChatReply | None = None
    async for item in provider.stream(model_name, messages, None):
        if isinstance(item, TextDelta):
            yield Delta(item.text)
        else:
            narration = item
    if narration is None:
        raise LLMError("the model returned no reply")
    reply = narration.text.strip()
    if not reply:
        raise LLMError("the model returned an empty reply")
    yield _Final(reply)


async def stream_message(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    text: str,
    model: str,
) -> AsyncIterator[TurnEvent]:
    """Send a user message to `model` and yield what happens while the GM replies.

    Yields Status events for each phase and Delta events for the narration only, then Saved once
    the reply is stored.

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
    provider, model_name = providers(model)

    summary = campaign.context_summary
    watermark = campaign.summary_through
    history = [m for m in list_messages(engine, campaign.id) if m.id > watermark]
    prompt_tokens = sum(
        p.tokens for p in system_parts(rule_set.id, rule_set.rules, summary, watermark)
    )
    prompt_tokens += estimate_tokens(text) + sum(estimate_tokens(m.content) for m in history)
    summary_record: list[ContextPart] = []
    if prompt_tokens >= context_tokens * COMPRESS_AT:
        start = _verbatim_start(history, int(context_tokens * KEEP_RECENT))
        if start > 0:
            folded = history[:start]
            yield Status("Summarizing older messages...")
            summary = await _summarize(
                provider,
                model_name,
                summary,
                folded,
                int(context_tokens * SUMMARY_SHARE),
                summary_record,
            )
            watermark = folded[-1].id or watermark
            history = history[start:]

    system = system_parts(rule_set.id, rule_set.rules, summary, watermark)
    player = ContextPart("user", "This message (player)", text)
    conversation = [
        *(ContextPart(m.role, _message_source(m), m.content) for m in history),
        player,
    ]
    context = [*system, *tool_definition_parts(rule_set), *conversation]
    messages = [
        {"role": "system", "content": "\n\n".join(p.content for p in system)},
        *({"role": p.role, "content": p.content} for p in conversation),
    ]
    round_start = len(context)
    reply: str | None = None
    async for event in _reply_with_tools(
        provider, model_name, messages, context, rule_set.tool_calls
    ):
        if isinstance(event, _Final):
            reply = event.text
        else:
            yield event
    if reply is None:
        raise LLMError("the model returned no reply")
    transcript = [
        *summary_record,
        player,
        *context[round_start:],
        ContextPart("assistant", "Reply", reply),
    ]
    with session_scope(engine) as db:
        stored = db.get(Campaign, campaign.id)
        if stored is None:
            raise NotFound("campaign not found")
        stored.context_summary = summary
        stored.summary_through = watermark
        db.add(Message(campaign_id=campaign.id, role="user", content=text))
        assistant = Message(campaign_id=campaign.id, role="assistant", content=reply, model=model)
        db.add(assistant)
        db.flush()
        db.add(
            TurnContext(
                message_id=assistant.id,
                parts=json.dumps([part.to_dict() for part in context]),
                transcript=json.dumps([part.to_dict() for part in transcript]),
            )
        )
        db.commit()
        message_id = assistant.id
    yield Saved(message_id)


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
) -> None:
    """Send a user message and wait for the GM's reply to be stored."""
    async for _ in stream_message(
        engine,
        providers,
        allowed_models,
        library,
        context_tokens,
        owner_id,
        campaign_id,
        text,
        model,
    ):
        pass


def get_turn_context(
    engine: Engine, owner_id: int, campaign_id: int, message_id: int
) -> list[ContextPart] | None:
    """The context the model was given for an assistant message.

    None means the turn was sent before contexts were recorded."""
    return _recorded_parts(engine, owner_id, campaign_id, message_id, lambda row: row.parts)


def get_turn_transcript(
    engine: Engine, owner_id: int, campaign_id: int, message_id: int
) -> list[ContextPart] | None:
    """What happened in the round that produced an assistant message, from the player's message
    to the reply.

    None means the turn was sent before transcripts were recorded."""
    return _recorded_parts(engine, owner_id, campaign_id, message_id, lambda row: row.transcript)


def _recorded_parts(
    engine: Engine,
    owner_id: int,
    campaign_id: int,
    message_id: int,
    pick: Callable[[TurnContext], str | None],
) -> list[ContextPart] | None:
    get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        message = db.get(Message, message_id)
        if message is None or message.campaign_id != campaign_id or message.role != "assistant":
            raise NotFound("message not found")
        row = db.get(TurnContext, message_id)
        raw = pick(row) if row is not None else None
        if raw is None:
            return None
        return [ContextPart(**part) for part in json.loads(raw)]
