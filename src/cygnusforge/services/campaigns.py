from __future__ import annotations

import json
import random
import threading
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import Engine
from sqlmodel import select

from cygnusforge.content.library import ContentLibrary, RuleSet, ToolCall
from cygnusforge.db.models import Campaign, Event
from cygnusforge.llm.base import (
    ChatReply,
    LLMError,
    Provider,
    TextDelta,
    ToolRequest,
    complete,
)
from cygnusforge.llm.registry import ProviderFactory
from cygnusforge.rules.context import ToolContext
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.common import Conflict, InvalidInput, NotFound, session_scope
from cygnusforge.services.events import (
    CHARS_PER_TOKEN,
    CampaignState,
    Channel,
    ChatGM,
    ChatPlayer,
    ContextPart,
    GameEvent,
    PromptShown,
    SpeechWritten,
    StateStepMade,
    SummaryUpdated,
    ToolRan,
    Turn,
    TurnBegun,
    TurnFailed,
    decode,
    estimate_tokens,
    events_since,
    last_event_id,
    load_state,
    record,
    truncate,
)
from cygnusforge.services.tools import (
    Ask,
    RollAnswer,
    TextAnswer,
    ToolRun,
    run_tool,
    tool_specs,
)
from cygnusforge.services.world import (
    WorldTools,
    WorldView,
    seed_campaign,
    snapshot,
    world_state_text,
)

MAX_NAME = 120
MAX_MESSAGE = 8000
SYSTEM_PROMPT = (
    "You are Cygnus, an AI game master (GM). You run the world of a roleplaying campaign: you "
    "describe the scene, voice its characters, decide the outcome of the player's actions, and keep "
    "the story consistent with what has already happened. You are not a general assistant; stay in "
    "the GM role for the whole campaign."
)
STATE_INSTRUCTION = (
    "State management step. Decide what this action changes and what needs to be said or done, "
    "then make it so with tool calls: create, update, or delete locations, characters, and notes as "
    "the story needs; move characters; record facts in notes so you stay consistent; ask for a roll "
    "when the outcome is uncertain; and call speak for each NPC who says something, without writing "
    "their words yourself. Do not narrate here; the narration comes in the next step. When nothing "
    "else needs to change, reply with one short sentence saying so."
)
NARRATE_INSTRUCTION = (
    "Narration step. Write the next beat as a good editor would: concise, specific, and free of "
    "filler. Aim for three to five sentences. Cut repetition and restated detail. Base the outcome "
    "on the state changes and tool results above. Narrate what the player can perceive: arrivals "
    "and departures, new or changed places, NPC speech, and roll outcomes. Work the queued NPC "
    "dialogue into the narration where it fits, copied exactly, and do not write any other words for "
    "an NPC. Do not narrate notes or other GM-only records, and do not mention tool names or ids. "
    "Do not call tools."
)
SPEECH_INSTRUCTION = (
    "Speech step. Write only the words the speaker says aloud to the player, in character, "
    "following the speaker's personality, accent, and quirks, and the intent given. Keep it short: "
    "one or two sentences, as the speaker would actually say it. Do not describe actions and do not "
    "narrate. Reply with the spoken words only."
)
SUMMARY_PROMPT = (
    "You are Cygnus, the AI game master of a roleplaying campaign, and you keep its memory. Given "
    "the summary so far and new events, write an updated summary. Keep names, places, facts the "
    "players established, open threads, "
    "and anything the players asked to remember. Reply with the summary only."
)
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
    allowed_models: Iterable[str],
    owner_id: int,
    name: str,
    world_template: str,
    rule_set: str,
    character_name: str,
    model: str,
    rng: random.Random | None = None,
) -> Campaign:
    name = (name or "").strip()
    if not name or len(name) > MAX_NAME:
        raise InvalidInput(f"name must be 1 to {MAX_NAME} characters")
    character_name = (character_name or "").strip()
    if not character_name or len(character_name) > MAX_NAME:
        raise InvalidInput(f"character name must be 1 to {MAX_NAME} characters")
    if world_template not in library.world_templates:
        raise InvalidInput("that world template is not available")
    if rule_set not in library.rule_sets:
        raise InvalidInput("that rule set is not available")
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    template = library.world_templates[world_template]
    with session_scope(engine) as db:
        campaign = Campaign(
            owner_id=owner_id,
            name=name,
            world_template=world_template,
            rule_set=rule_set,
            model=model,
        )
        db.add(campaign)
        db.flush()
        # Seeding commits the campaign's world with it. If it fails, nothing is kept.
        seed_campaign(
            db,
            campaign.id,
            template,
            library.core.tables,
            rng if rng is not None else random.Random(),
            character_name,
        )
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


# Campaigns with a turn, answer, rollback, or model change in progress. Only one of those may run
# per campaign at a time, so a second request is refused with Conflict instead of interleaving
# its writes. The set is guarded by a lock only for the moment it is read or changed, so a turn
# can span many awaits without holding the lock. This covers one server process.
_busy_lock = threading.Lock()
_busy_campaigns: set[int] = set()


@contextmanager
def _exclusive(campaign_id: int) -> Iterator[None]:
    with _busy_lock:
        if campaign_id in _busy_campaigns:
            raise Conflict("the GM is still working on this campaign; try again when it finishes")
        _busy_campaigns.add(campaign_id)
    try:
        yield
    finally:
        with _busy_lock:
            _busy_campaigns.discard(campaign_id)


def set_campaign_model(
    engine: Engine,
    allowed_models: Iterable[str],
    owner_id: int,
    campaign_id: int,
    model: str,
) -> Campaign:
    """Switch the model every later reply in the campaign uses, including a reply that is
    paused waiting for the player."""
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.model = model
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


@dataclass(frozen=True)
class CampaignWorld:
    """The folded campaign state, and the world as the page shows it."""

    state: CampaignState
    view: WorldView


def campaign_world(engine: Engine, owner_id: int, campaign_id: int) -> CampaignWorld:
    get_campaign(engine, owner_id, campaign_id)
    state = _state(engine, campaign_id)
    return CampaignWorld(state=state, view=snapshot(state))


@dataclass(frozen=True)
class ChatMessage:
    """A line of the chat: the player's, or the GM's reply with the model that wrote it. `id` is
    the id of the event that recorded it."""

    id: int
    role: str
    content: str
    model: str | None = None


def list_messages(engine: Engine, campaign_id: int) -> list[ChatMessage]:
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, channel=Channel.TRANSCRIPT)
        return [_chat_message(row) for row in rows]


def _chat_message(row: Event) -> ChatMessage:
    event = decode(row)
    if isinstance(event, ChatPlayer):
        return ChatMessage(id=row.id or 0, role="user", content=event.content)
    if isinstance(event, ChatGM):
        return ChatMessage(
            id=row.id or 0, role="assistant", content=event.content, model=event.model
        )
    raise TypeError(f"{row.kind} is not a chat event")


def system_parts(
    rule_set_id: str,
    rules: Mapping[str, str],
    summary: str,
    summary_through: int,
    world_text: str = "",
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
    if world_text:
        parts.append(ContextPart("system", "World state", world_text))
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


def _message_source(message: ChatMessage) -> str:
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
    campaign: Campaign,
    state: CampaignState,
    history: Sequence[ChatMessage],
    rules: Mapping[str, str],
    limit: int,
) -> ContextUsage:
    unsummarized = [m for m in history if m.id > state.through]
    used = sum(
        part.tokens
        for part in system_parts(
            campaign.rule_set, rules, state.summary, state.through, world_state_text(state)
        )
    )
    used += sum(estimate_tokens(m.content) for m in unsummarized)
    return ContextUsage(used=used, limit=limit)


def _verbatim_start(history: Sequence[ChatMessage], budget: int) -> int:
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
    messages: Sequence[ChatMessage],
    budget_tokens: int,
    parts: list[ContextPart],
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
    parts.extend(
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
class Logged:
    """Something that happened during the reply and is shown in the Log, such as a roll."""

    text: str


@dataclass(frozen=True)
class Prompt:
    """The reply is paused until the player rolls `dice` or types a reply to `text`.

    `details`, if set, is shown with the prompt, such as the modifiers for a roll."""

    text: str
    dice: str
    details: str | None = None


@dataclass(frozen=True)
class Saved:
    """The turn is stored. `event_id` is the GM's reply."""

    event_id: int


TurnEvent = Status | Delta | Logged | Prompt | Saved


def _add_instruction(
    messages: list[dict[str, Any]], context: list[ContextPart], source: str, text: str
) -> None:
    messages.append({"role": "system", "content": text})
    context.append(ContextPart("system", source, text))


def _add_speech(
    messages: list[dict[str, Any]], context: list[ContextPart], speech: list[dict[str, str]]
) -> None:
    lines = "\n".join(f'{line["speaker"]}: "{line["dialogue"]}"' for line in speech)
    _add_instruction(
        messages,
        context,
        "Queued dialogue",
        f"Dialogue spoken this turn, in the order it was said:\n{lines}",
    )


def _dicts(parts: Sequence[ContextPart]) -> list[dict[str, str]]:
    return [part.to_dict() for part in parts]


def _status_for(request: ToolRequest) -> str:
    if request.name != "speak":
        return f"Running {request.name}..."
    try:
        speaker = json.loads(request.arguments)["npc"]
    except (ValueError, KeyError, TypeError):
        return "Writing dialogue..."
    return f"Writing dialogue for {speaker}..."


def _tool_ran(round_no: int, request: ToolRequest, result: ToolRun) -> ToolRan:
    return ToolRan(
        request_id=request.id,
        messages=[{"role": "tool", "tool_call_id": request.id, "content": result.output}],
        context=_dicts(
            [
                ContextPart(
                    "assistant",
                    f"GM tool call (round {round_no})",
                    f"{request.name}({request.arguments})",
                ),
                ContextPart(
                    "tool", f"Result of '{request.name}' (round {round_no})", result.output
                ),
            ]
        ),
        activity=result.activity,
    )


def _record(engine: Engine, campaign_id: int, *events: GameEvent) -> int:
    """Record events in one commit. Returns the id of the last one."""
    with session_scope(engine) as db:
        event_id = 0
        for event in events:
            event_id = record(db, campaign_id, event)
        db.commit()
    return event_id


def _state(engine: Engine, campaign_id: int, before: int | None = None) -> CampaignState:
    with session_scope(engine) as db:
        return load_state(db, campaign_id, before)


def _newest(engine: Engine, campaign_id: int) -> int:
    with session_scope(engine) as db:
        return last_event_id(db, campaign_id)


def _turn(state: CampaignState) -> Turn:
    if state.turn is None:
        raise RuntimeError("no turn is open")
    return state.turn


@dataclass
class _Run:
    """What the steps of one reply need: where they are recorded, the model that writes them, and
    the tools they can call. `cursor` is the last event whose Log entries were shown."""

    engine: Engine
    campaign_id: int
    model: str
    rule_set: RuleSet
    context_tokens: int
    provider: Provider
    model_name: str
    ctx: ToolContext
    cursor: int

    @property
    def tool_calls(self) -> Mapping[str, ToolCall]:
        return self.rule_set.tool_calls


def _tool_context(
    engine: Engine,
    library: ContentLibrary,
    rng: random.Random,
    provider: Provider,
    model_name: str,
    campaign_id: int,
) -> ToolContext:
    async def speak(speaker: str, profile: str, intent: str) -> None:
        await _speak(engine, provider, model_name, campaign_id, speaker, profile, intent)

    return ToolContext(
        world=WorldTools(engine, campaign_id, library.core.tables, rng),
        speak=speak,
    )


def _start_run(
    engine: Engine,
    providers: ProviderFactory,
    library: ContentLibrary,
    rule_set: RuleSet,
    campaign: Campaign,
    context_tokens: int,
    rng: random.Random | None,
) -> _Run:
    provider, model_name = providers(campaign.model)
    return _Run(
        engine=engine,
        campaign_id=campaign.id,
        model=campaign.model,
        rule_set=rule_set,
        context_tokens=context_tokens,
        provider=provider,
        model_name=model_name,
        ctx=_tool_context(
            engine,
            library,
            rng if rng is not None else random.Random(),
            provider,
            model_name,
            campaign.id,
        ),
        cursor=_newest(engine, campaign.id),
    )


def _flush(run: _Run) -> list[Logged]:
    """The Log entries recorded since the last flush, which the player has not been shown yet."""
    with session_scope(run.engine) as db:
        rows = events_since(db, run.campaign_id, run.cursor, Channel.LOG)
    if rows:
        run.cursor = rows[-1].id or run.cursor
    return [Logged(text) for row in rows if (text := decode(row).render()) is not None]


async def _speak(
    engine: Engine,
    provider: Provider,
    model_name: str,
    campaign_id: int,
    speaker: str,
    profile: str,
    intent: str,
) -> None:
    """Ask the speech step for an NPC's words and record them for the narration. The speech step
    sees the conversation as text, without the tool calls, so it writes dialogue rather than
    narration."""
    turn = _turn(_state(engine, campaign_id))
    system = "\n\n".join(
        [SPEECH_INSTRUCTION, turn.messages[0]["content"], profile, f"Intent: {intent}"]
    )
    conversation = [
        m for m in turn.messages[1:] if m["role"] in ("user", "assistant") and "tool_calls" not in m
    ]
    label = f"Speech step (round {turn.rounds})"
    reply = await complete(
        provider, model_name, [{"role": "system", "content": system}, *conversation]
    )
    reply = reply.strip()
    _record(
        engine,
        campaign_id,
        SpeechWritten(
            speaker=speaker,
            dialogue=reply,
            context=_dicts(
                [
                    ContextPart("system", label, system),
                    ContextPart("assistant", f"{label} reply", reply),
                ]
            ),
        ),
    )
    if not reply:
        raise LLMError("the speech step returned no dialogue")


async def _begin(run: _Run, turn: Turn) -> AsyncIterator[TurnEvent]:
    """Set up the model's context for the turn. Once the prompt reaches the compression point, the
    older part of the chat is folded into the summary first."""
    state = _state(run.engine, run.campaign_id)
    rule_set = run.rule_set
    summary, watermark = state.summary, state.through
    world_text = world_state_text(state)
    history = [
        m for m in list_messages(run.engine, run.campaign_id) if watermark < m.id < turn.opener
    ]
    prompt_tokens = sum(
        p.tokens for p in system_parts(rule_set.id, rule_set.rules, summary, watermark, world_text)
    )
    prompt_tokens += estimate_tokens(turn.text) + sum(estimate_tokens(m.content) for m in history)
    summary_record: list[ContextPart] = []
    events: list[GameEvent] = []
    if prompt_tokens >= run.context_tokens * COMPRESS_AT:
        start = _verbatim_start(history, int(run.context_tokens * KEEP_RECENT))
        if start > 0:
            folded = history[:start]
            yield Status("Summarizing older messages...")
            summary = await _summarize(
                run.provider,
                run.model_name,
                summary,
                folded,
                int(run.context_tokens * SUMMARY_SHARE),
                summary_record,
            )
            watermark = folded[-1].id
            history = history[start:]
            events.append(SummaryUpdated(summary=summary, through=watermark))
    player = ContextPart("user", "This message (player)", turn.text)
    system = system_parts(rule_set.id, rule_set.rules, summary, watermark, world_text)
    conversation = [
        *(ContextPart(m.role, _message_source(m), m.content) for m in history),
        player,
    ]
    context = [*system, *tool_definition_parts(rule_set), *conversation]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(p.content for p in system)},
        *({"role": p.role, "content": p.content} for p in conversation),
    ]
    round_start = len(context)
    context.append(ContextPart("system", "State instruction", STATE_INSTRUCTION))
    events.append(
        TurnBegun(
            head=_dicts([*summary_record, player]),
            context=_dicts(context),
            messages=messages,
            round_start=round_start,
        )
    )
    _record(run.engine, run.campaign_id, *events)


async def _drive(run: _Run) -> AsyncIterator[TurnEvent]:
    """Run the reply until it is recorded or paused. Log entries are yielded as the steps that
    recorded them finish. A pause is recorded before the player hears of it."""
    tools = tool_specs(run.tool_calls)
    state = {"role": "system", "content": STATE_INSTRUCTION}
    while True:
        turn = _turn(_state(run.engine, run.campaign_id))
        if turn.queue:
            request = turn.queue[0]
            yield Status(_status_for(request))
            result = await run_tool(run.tool_calls, request, ctx=run.ctx)
            if isinstance(result, Ask):
                _record(
                    run.engine,
                    run.campaign_id,
                    PromptShown(result.text, result.dice, result.details),
                )
                for entry in _flush(run):
                    yield entry
                yield Prompt(result.text, result.dice, result.details)
                return
            _record(run.engine, run.campaign_id, _tool_ran(turn.rounds, request, result))
            for entry in _flush(run):
                yield entry
            continue
        if turn.narrating:
            break
        yield Status("Managing state...")
        round_no = turn.rounds + 1
        step = await run.provider.chat(run.model_name, [*turn.messages, state], tools)
        if not step.tool_requests:
            messages: list[dict[str, Any]] = []
            context: list[ContextPart] = []
            if step.text.strip():
                messages.append({"role": "assistant", "content": step.text})
                context.append(
                    ContextPart("assistant", f"GM state step (round {round_no})", step.text)
                )
            if turn.speech:
                _add_speech(messages, context, turn.speech)
            _add_instruction(messages, context, "Narrate instruction", NARRATE_INSTRUCTION)
            _record(
                run.engine,
                run.campaign_id,
                StateStepMade(requests=[], messages=messages, context=_dicts(context)),
            )
            continue
        context = []
        if step.text:
            context.append(ContextPart("assistant", f"GM text (round {round_no})", step.text))
        messages = [
            {
                "role": "assistant",
                "content": step.text or None,
                "tool_calls": [
                    {
                        "id": request.id,
                        "type": "function",
                        "function": {"name": request.name, "arguments": request.arguments},
                    }
                    for request in step.tool_requests
                ],
            }
        ]
        _record(
            run.engine,
            run.campaign_id,
            StateStepMade(
                requests=[asdict(request) for request in step.tool_requests],
                messages=messages,
                context=_dicts(context),
            ),
        )

    yield Status("Narrating...")
    messages = _turn(_state(run.engine, run.campaign_id)).messages
    narration: ChatReply | None = None
    async for item in run.provider.stream(run.model_name, messages, None):
        if isinstance(item, TextDelta):
            yield Delta(item.text)
        else:
            narration = item
    if narration is None:
        raise LLMError("the model returned no reply")
    reply = narration.text.strip()
    if not reply:
        raise LLMError("the model returned an empty reply")
    event_id = _record(run.engine, run.campaign_id, ChatGM(content=reply, model=run.model))
    yield Saved(event_id)


async def _carry_on(run: _Run) -> AsyncIterator[TurnEvent]:
    turn = _turn(_state(run.engine, run.campaign_id))
    if not turn.begun:
        async for event in _begin(run, turn):
            yield event
    async for event in _drive(run):
        yield event


async def _answer(run: _Run, answer: RollAnswer | TextAnswer | None) -> AsyncIterator[TurnEvent]:
    """Give the waiting tool the player's answer, record its result, then carry the reply on."""
    if answer is not None:
        turn = _turn(_state(run.engine, run.campaign_id))
        request = turn.queue[0]
        result = await run_tool(run.tool_calls, request, answer, ctx=run.ctx)
        assert isinstance(result, ToolRun)
        _record(run.engine, run.campaign_id, _tool_ran(turn.rounds, request, result))
        for entry in _flush(run):
            yield entry
        if isinstance(answer, TextAnswer):
            _record(run.engine, run.campaign_id, ChatPlayer(content=answer.text))
    async for event in _carry_on(run):
        yield event


async def _guarded(run: _Run, steps: AsyncIterator[TurnEvent]) -> AsyncIterator[TurnEvent]:
    """Pass the steps along. If one fails, record why, so the reply can be continued or rolled
    back. A step that is cancelled or abandoned records nothing, and its turn stays open."""
    try:
        async for event in steps:
            yield event
    except Exception as exc:
        message = str(exc) if isinstance(exc, LLMError) else "an unexpected error"
        _record(run.engine, run.campaign_id, TurnFailed(error=message))
        raise


async def stream_message(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    text: str,
    rng: random.Random | None = None,
) -> AsyncIterator[TurnEvent]:
    """Send a user message, to the campaign's model, and yield what happens while the GM replies.

    Yields Status while the GM works, Logged for each Log entry the turn adds, Delta for the
    narration only, and Prompt when the reply needs the player to roll or type something. The
    reply then continues through `resume_turn`. Saved comes once the reply is recorded.

    The message is recorded before the reply starts, so it survives a failed reply. A failed reply
    stays open: `resume_turn` with "continue" retries it, and rolling back removes it."""
    text = (text or "").strip()
    if not text:
        raise InvalidInput("message cannot be empty")
    if len(text) > MAX_MESSAGE:
        raise InvalidInput(f"message must be at most {MAX_MESSAGE} characters")
    campaign = get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _exclusive(campaign.id):
        open_turn = _state(engine, campaign.id).turn
        if open_turn is not None:
            if open_turn.prompt is not None:
                raise InvalidInput("answer the GM's prompt before sending another message")
            raise InvalidInput(
                "the GM's reply is unfinished; continue it or roll back before sending another message"
            )
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        run = _start_run(engine, providers, library, rule_set, campaign, context_tokens, rng)
        _record(engine, campaign.id, ChatPlayer(content=text))
        async for event in _guarded(run, _carry_on(run)):
            yield event


async def resume_turn(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    action: str,
    text: str = "",
    rng: random.Random | None = None,
) -> AsyncIterator[TurnEvent]:
    """Continue an open reply with the player's answer, using the campaign's current model.

    `action` is "roll" to roll the prompt's dice, "text" to answer with free-form `text`, or
    "continue" to retry a reply that stopped on an error or was interrupted. The answer is
    recorded before the reply continues, so a failure later does not roll again."""
    campaign = get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _exclusive(campaign.id):
        turn = _state(engine, campaign.id).turn
        if turn is None:
            raise InvalidInput("there is no reply waiting for an answer")
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        answer: RollAnswer | TextAnswer | None = None
        if action == "continue":
            if turn.prompt is not None:
                raise InvalidInput("answer the GM's prompt first")
        elif turn.prompt is None:
            raise InvalidInput("there is nothing to answer")
        elif action == "roll":
            answer = RollAnswer(roll_total(turn.prompt.dice, rng))
        elif action == "text":
            clean = (text or "").strip()
            if not clean:
                raise InvalidInput("the reply cannot be empty")
            if len(clean) > MAX_MESSAGE:
                raise InvalidInput(f"the reply must be at most {MAX_MESSAGE} characters")
            answer = TextAnswer(clean)
        else:
            raise InvalidInput("that action is not available")
        run = _start_run(engine, providers, library, rule_set, campaign, context_tokens, rng)
        async for event in _guarded(run, _answer(run, answer)):
            yield event


@dataclass(frozen=True)
class PendingView:
    """An open reply that needs attention: its prompt, or why it stopped. The player's words and
    the rest of the turn are already in the transcript and the Log."""

    prompt: Ask | None
    error: str | None = None


def pending_turn(engine: Engine, owner_id: int, campaign_id: int) -> PendingView | None:
    """The open reply's prompt, or why it stopped. None when no reply is open, or while one is
    running in this process."""
    get_campaign(engine, owner_id, campaign_id)
    with _busy_lock:
        if campaign_id in _busy_campaigns:
            return None
    turn = _state(engine, campaign_id).turn
    if turn is None:
        return None
    if turn.prompt is not None:
        return PendingView(prompt=turn.prompt)
    return PendingView(prompt=None, error=turn.failed or "The GM's reply did not finish.")


async def send_message(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    text: str,
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
    ):
        pass


def rollback_to(engine: Engine, owner_id: int, campaign_id: int, event_id: int) -> None:
    """Return the campaign to just after the GM reply `event_id`. Later events are removed,
    including any open reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        event = db.get(Event, event_id)
        if event is None or event.campaign_id != campaign_id or event.kind != ChatGM.kind:
            raise NotFound("message not found")
        truncate(db, campaign_id, event_id)
        db.commit()


def discard_pending(engine: Engine, owner_id: int, campaign_id: int) -> None:
    """Remove the open reply and the player's message that started it, so the player can send
    the message again. Used when a reply cannot be continued."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        turn = load_state(db, campaign_id).turn
        if turn is None:
            raise NotFound("there is no open reply to discard")
        truncate(db, campaign_id, turn.opener - 1)
        db.commit()


def campaign_log(engine: Engine, owner_id: int, campaign_id: int) -> list[str]:
    """The Log's entries, oldest first."""
    get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, channel=Channel.LOG)
        return [text for row in rows if (text := decode(row).render()) is not None]


def _turn_for_reply(
    engine: Engine, owner_id: int, campaign_id: int, event_id: int
) -> tuple[Turn | None, str]:
    """The turn that produced a GM reply, as it stood before the reply was recorded, and the
    reply's text. The turn is None when no turn was open, so nothing was recorded for the reply."""
    get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        event = db.get(Event, event_id)
        if event is None or event.campaign_id != campaign_id or event.kind != ChatGM.kind:
            raise NotFound("message not found")
        reply = decode(event)
        assert isinstance(reply, ChatGM)
        turn = load_state(db, campaign_id, before=event_id).turn
    return turn, reply.content


def get_turn_context(
    engine: Engine, owner_id: int, campaign_id: int, event_id: int
) -> list[ContextPart]:
    """The context the model was given for a GM reply."""
    turn, _ = _turn_for_reply(engine, owner_id, campaign_id, event_id)
    return [] if turn is None else list(turn.context)


def get_turn_transcript(
    engine: Engine, owner_id: int, campaign_id: int, event_id: int
) -> list[ContextPart]:
    """What happened in the round that produced a GM reply, from the player's message to the
    reply."""
    turn, reply = _turn_for_reply(engine, owner_id, campaign_id, event_id)
    if turn is None:
        return []
    return [
        *turn.head,
        *turn.context[turn.round_start :],
        ContextPart("assistant", "Reply", reply),
    ]
