from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator, Awaitable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine

from cygnusforge.content.library import (
    ContentLibrary,
    RuleSet,
    ToolCall,
    WorldContent,
    WorldTemplate,
)
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
from cygnusforge.rules.context import ToolContext, brief
from cygnusforge.rules.dice import roll_total
from cygnusforge.services import encounters
from cygnusforge.services.common import InvalidInput, NotFound, session_scope
from cygnusforge.services.events import (
    PC_KIND,
    CampaignState,
    Channel,
    ChatGM,
    ChatPlayer,
    ClockAdvanced,
    ContextPart,
    DebugInjection,
    DebugInjectionCleared,
    DebugInjectionRan,
    GameEvent,
    JournalStarted,
    NarrationUndone,
    OocStarted,
    PromptShown,
    ReviewStepMade,
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
    is_hidden,
    last_event_id,
    load_state,
    name_key,
    record,
    speaker_name,
    truncate,
)
from cygnusforge.services.level_up import advancement_due
from cygnusforge.services.prompts import (
    NARRATION_REVISION,
    OOC_INSTRUCTION,
    TURN_REVIEW_INSTRUCTION,
    journal_instruction,
    narrate_instruction,
    opening_prompt,
    speech_instruction,
    state_instruction,
    system_parts,
)
from cygnusforge.services.tools import (
    Ask,
    RollAnswer,
    TextAnswer,
    ToolRun,
    find_tool,
    run_tool,
    tool_specs,
)
from cygnusforge.services.views import present_rules_text, world_state_text

if TYPE_CHECKING:
    from cygnusforge.services.campaigns import ChatMessage

from cygnusforge.services.world import (
    WorldTools,
)

MAX_NAME = 120
MAX_MESSAGE = 8000
# Spoken words never contain angle brackets or call syntax, so either means a tool call was written.
SPOKEN_MARKUP = re.compile(r"<[^>]*>|\b\w+\(\s*\{")
# An empty speech reply is retried this many times before the step gives up.
SPEECH_ATTEMPTS = 3
SPEECH_RETRY = (
    "Your last reply was empty. Write the line now: only the words the speaker says aloud, "
    "one line, and nothing else."
)
# An empty narration is requested again this many times before the turn fails.
NARRATION_ATTEMPTS = 2
# The narration is reviewed after it is written. A review that sets it aside writes the story
# again, up to this many times; the last narration is accepted without another review.
NARRATION_REVIEWS = 2
# A review step's model rounds: enough to undo, fix the record, and stop.
REVIEW_ROUNDS = 3
# The state-step tool that marks a message out of character. The out-of-character step must not
# be offered it again, or the model can restart that step instead of replying.
OOC_TOOL = "ooc"
# The review tool that sets the narration aside.
UNDO_NARRATION = "undo_narration"
# The note recorded when a review changed the record without setting the narration aside first.
REVIEW_CHANGED_NOTE = "the review changed the record after the narration was written"
# A long model call would otherwise leave the player's stream silent, and a silent stream looks
# exactly like a hung page. Every this many seconds of waiting, the turn sends a status line.
HEARTBEAT_SECONDS = 15.0
HEARTBEAT_MESSAGE = "Still working..."
JOURNAL_ROUNDS = 3
# Once the prompt reaches this share of the context window, older history is summarized.
COMPRESS_AT = 0.5
# After a summary, the newest messages worth up to this share of the window stay verbatim.
KEEP_RECENT = 0.1
# A summary may take up to this share of the window. Longer ones keep only their newest text.
SUMMARY_SHARE = 0.25
WORDS_PER_TOKEN = 0.75


def _campaigns():
    """The campaign module, imported when a turn runs so the two modules can re-export each other."""
    from cygnusforge.services import campaigns

    return campaigns


@dataclass(frozen=True)
class Status:
    """What the GM is doing now, shown while the reply is pending."""

    text: str


@dataclass(frozen=True)
class Delta:
    """A piece of the GM's reply text, as it arrives."""

    text: str


@dataclass(frozen=True)
class LogLine:
    """An entry of the Log, shown in the chat between the messages. `id` is the id of the event
    that recorded it."""

    id: int
    content: str
    role: str = "log"
    hidden: bool = False


@dataclass(frozen=True)
class Logged:
    """Something that happened during the reply and is shown in the Log, such as a roll."""

    text: str
    hidden: bool = False


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


@dataclass(frozen=True)
class NextMessage:
    """The reply goes on in a new message, such as the story after an out-of-character reply."""


@dataclass(frozen=True)
class Retract:
    """The reply text streamed so far was set aside and will be written again in the same
    message."""


TurnEvent = Status | Delta | Logged | Prompt | Saved | NextMessage | Retract


def _add_instruction(
    messages: list[dict[str, Any]], context: list[ContextPart], source: str, text: str
) -> None:
    messages.append({"role": "system", "content": text})
    context.append(ContextPart("system", source, text))


def _add_speech(
    messages: list[dict[str, Any]], context: list[ContextPart], speech: list[dict[str, str]]
) -> None:
    lines = [{"speaker": line["speaker"], "dialogue": line["dialogue"]} for line in speech]
    payload = json.dumps({"queued_dialogue": lines}, ensure_ascii=False, indent=2)
    _add_instruction(messages, context, "Queued dialogue", payload)


def _queued_dialogue_note(speech: list[dict[str, str]]) -> dict[str, str]:
    """A note for the state step about the dialogue already queued this reply. It is not recorded,
    so the state step can see what it has done and amend a line rather than repeat it."""
    lines = "\n".join(f'{line["speaker"]}: "{line["dialogue"]}"' for line in speech)
    return {
        "role": "system",
        "content": "Dialogue already queued this reply, in the order it was said:\n"
        f"{lines}\nDo not speak these NPCs again. To amend a queued line, call speak with "
        "replace: true.",
    }


def _text_message(content: str, reasoning: str) -> dict[str, Any]:
    """A plain assistant message. Thinking models reject a request whose assistant message after
    the last user message has no reasoning_content key, even when the reasoning is empty."""
    return {"role": "assistant", "content": content, "reasoning_content": reasoning}


def _history_message(m: ChatMessage) -> dict[str, Any]:
    """A chat message as the model sees it. A GM reply keeps the reasoning it was written with."""
    if m.role == "assistant":
        return _text_message(m.content, m.reasoning)
    return {"role": m.role, "content": m.content}


def _tool_call_message(step: ChatReply) -> dict[str, Any]:
    """The assistant message that records a step's tool requests, as the model sees it. Thinking
    models reject it with HTTP 400 when the key is missing, even if the reasoning is empty."""
    message: dict[str, Any] = {
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
    message["reasoning_content"] = step.reasoning
    return message


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
    opening: str
    # The world's rules that no place, NPC, or monster attaches. They are listed for the GM as
    # campaign rules, so lookup_rule can read them.
    unattached_rules: Mapping[str, str] = field(default_factory=dict)
    # The campaign's world, or None when its template is gone. The prompt builders read its
    # setting to decide whether the GM invents one.
    world: WorldTemplate | None = None
    # The setting and story the player asked for at creation, or blank. It is prompt guidance,
    # not world state.
    setting: str = ""

    @property
    def tool_calls(self) -> Mapping[str, ToolCall]:
        return self.rule_set.tool_calls


def _tool_context(
    engine: Engine,
    rng: random.Random,
    provider: Provider,
    model_name: str,
    campaign_id: int,
    rule_set: RuleSet,
    world: WorldContent,
) -> ToolContext:
    async def speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        await _speak(
            engine,
            provider,
            model_name,
            campaign_id,
            speaker,
            profile,
            intent,
            replace,
            rule_set.sentence_cap,
        )

    def ooc(narrate: bool) -> None:
        _record(engine, campaign_id, OocStarted(narrate=narrate))

    world_tools = WorldTools(
        engine,
        campaign_id,
        {**rule_set.tables, **world.tables},
        rng,
        rule_set.character,
        rule_data=rule_set.rule_data,
        grid=rule_set.grid,
        terrain=rule_set.terrain,
        encounter_objects=rule_set.encounter_objects,
        encounter_templates=rule_set.encounter_templates,
        combat=rule_set.combat,
        monsters={**rule_set.monsters, **world.monsters},
        features={**world.features},
        checks=rule_set.checks,
        conditions=rule_set.conditions,
        projects=rule_set.projects,
        moves=rule_set.moves,
        counters=rule_set.counters,
        advancements=rule_set.advancements,
        periods=rule_set.periods,
        tallies=rule_set.tallies,
        relations=rule_set.relations,
        dice_tables={**rule_set.dice_tables, **world.dice_tables},
        clock_rules=rule_set.clock,
        rests=rule_set.rests,
        interrupted_rest=rule_set.interrupted_rest,
        hooks=rule_set.hooks,
        inventory=rule_set.inventory,
        item_sizes=rule_set.item_sizes,
        magic=rule_set.magic,
        marketplace=rule_set.marketplace,
        plus=rule_set.plus,
        npc_hp=rule_set.npc_hp,
        npc_hp_stat=rule_set.npc_hp_stat,
        travel=rule_set.travel,
        wilderness=rule_set.wilderness,
        world_terrain=rule_set.world_terrain,
    )

    async def check_new_location(name: str) -> None:
        repeated = await _campaigns()._repeated_place(
            provider, model_name, world_tools.place_names(), name
        )
        if repeated is not None:
            raise InvalidInput(
                f"there is already a location called {repeated!r}; use discover_location to reveal it"
            )

    return ToolContext(
        world=world_tools,
        speak=speak,
        ooc=ooc,
        rules={**rule_set.rules, **rule_set.attached_rules, **world.rules},
        lookup_tools={
            tool.name: brief(tool.description)
            for tool in rule_set.tool_calls.values()
            if tool.lookup
        },
        check_new_location=check_new_location,
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
    world = library.world_content(campaign.world_template, campaign.rule_set)
    template = library.world_templates.get(campaign.world_template)
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
            rng if rng is not None else random.Random(),
            provider,
            model_name,
            campaign.id,
            rule_set,
            world,
        ),
        cursor=_newest(engine, campaign.id),
        opening=opening_prompt(template),
        unattached_rules=world.unattached_rules,
        world=template,
        setting=campaign.setting,
    )


def _turn_log(engine: Engine, campaign_id: int, after: int) -> list[str]:
    """The Log lines recorded after event `after`, in order."""
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, after, Channel.LOG)
    return [
        text
        for row in rows
        if not is_hidden(row.kind) and (text := decode(row).render()) is not None
    ]


def _turn_has_day_end(engine: Engine, campaign_id: int, opener: int) -> bool:
    """Whether the clock moves into a new day since the player's message, which ends the day the
    journal records. A jump over several days counts once. Hidden events count, since the journal
    keeps what the player does not see."""
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, opener)
    for row in rows:
        event = decode(row)
        if isinstance(event, ClockAdvanced) and event.new_day:
            return True
    return False


def _world_text(run: _Run, state: CampaignState) -> str:
    rule_set = run.rule_set
    return world_state_text(
        state,
        rule_set.campaign,
        rule_set.grid,
        rule_set.combat,
        rule_set.conditions,
        clock_rules=rule_set.clock,
        inventory=rule_set.inventory,
        hooks=rule_set.hooks,
        counters=rule_set.counters,
    )


def _narration_messages(
    run: _Run, turn: Turn, log: list[str] | None = None, heading: str = "Turn log, in order:"
) -> list[dict[str, Any]]:
    """What the narration step reads. The turn's opening system message predates the moves made
    in the turn, so the scene is rebuilt from the state as it is now, which also brings in the
    rules for anything that came into the scene. The turn log follows the conversation, so the
    narration covers each entry. A state step's prose is a draft written before the tools ran, so
    the narration sees the tool calls but not that prose. `log` replaces the turn's own Log lines.
    The narration instruction is always last, after the log and any review note, even when a
    review step added its own tool exchange to the turn."""
    state = _state(run.engine, run.campaign_id)
    instruction = narrate_instruction(run.rule_set, run.world)
    scene = system_parts(
        run.rule_set.id,
        {**run.rule_set.rules, **run.unattached_rules},
        run.rule_set.important,
        state.summary,
        state.through,
        _world_text(run, state),
        present_rules_text(state, run.ctx.rules),
        run.setting,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(p.content for p in scene)},
        *(
            _without_draft(m)
            for m in turn.messages[1:]
            if not (m.get("role") == "system" and m.get("content") == instruction)
        ),
    ]
    log = _turn_log(run.engine, run.campaign_id, turn.opener) if log is None else log
    if log:
        lines = "\n".join(f"{number}. {line}" for number, line in enumerate(log, 1))
        messages.append({"role": "system", "content": f"{heading}\n{lines}"})
    if turn.speech:
        _add_speech(messages, [], turn.speech)
    if turn.revision:
        messages.append(
            {"role": "system", "content": NARRATION_REVISION.format(reason=turn.revision)}
        )
    messages.append({"role": "system", "content": instruction})
    return messages


def _without_draft(message: dict[str, Any]) -> dict[str, Any]:
    """A tool-call message minus the prose the state step wrote with it. The prose was written
    before the tools ran, so it can name outcomes that never happened."""
    if message.get("role") == "assistant" and message.get("tool_calls"):
        return {**message, "content": None}
    return message


def _flush(run: _Run) -> list[Logged]:
    """The Log entries recorded since the last flush, which the player has not been shown yet."""
    with session_scope(run.engine) as db:
        rows = events_since(db, run.campaign_id, run.cursor, Channel.LOG)
    if rows:
        run.cursor = rows[-1].id or run.cursor
    return [
        Logged(text, hidden=is_hidden(row.kind))
        for row in rows
        if (text := decode(row).render()) is not None
    ]


async def _await_with_heartbeat[T](
    call: Awaitable[T], result: list[T], message: str = HEARTBEAT_MESSAGE
) -> AsyncIterator[TurnEvent]:
    """Await one model call, sending a status line while it runs.

    Without this the player's stream says nothing at all between the tool result and the reply,
    which is indistinguishable from a hung page when a provider takes minutes.
    """
    task = asyncio.ensure_future(call)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
            if done:
                result.append(task.result())
                return
            yield Status(message)
    finally:
        if not task.done():
            task.cancel()


async def _stream_with_heartbeat(
    stream: AsyncIterator[Any], message: str = HEARTBEAT_MESSAGE
) -> AsyncIterator[tuple[bool, Any]]:
    """Yield each streamed item as (True, item), and a (False, None) while the model is silent.

    The pending read is carried across timeouts rather than cancelled, so a slow provider is
    never interrupted by the heartbeat.
    """
    iterator = stream.__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
            if not done:
                yield False, None
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = asyncio.ensure_future(iterator.__anext__())
            yield True, item
    finally:
        if not pending.done():
            pending.cancel()


async def _speak(
    engine: Engine,
    provider: Provider,
    model_name: str,
    campaign_id: int,
    speaker: str,
    profile: str,
    intent: str,
    replace: bool,
    sentence_cap: int | None,
) -> None:
    """Ask the speech step for an NPC's words and record them for the narration. The speech step
    sees the conversation as text, without the tool calls, so it writes dialogue rather than
    narration. Each NPC gets one queued line per reply, so the narration never repeats a speaker.
    With `replace`, the new line takes the queued line's place; a failed attempt keeps the old one."""
    state = _state(engine, campaign_id)
    turn = _turn(state)
    queued = next(
        (line for line in turn.speech if line["speaker"].casefold() == speaker.casefold()), None
    )
    if queued and not replace:
        raise InvalidInput(
            f"{speaker} already has dialogue for this reply; put everything they say in one intent, "
            "or pass replace: true to rewrite that line"
        )
    # The speech step gets no rules or tool instructions: it only writes words, and those
    # instructions get copied into dialogue as tool-call markup.
    parts = [speech_instruction(sentence_cap)]
    if state.summary:
        parts.append("Summary of earlier events in this campaign:\n" + state.summary)
    parts.append(profile)
    if queued:
        parts.append(
            f"The line already queued for {speaker}, which this intent replaces: "
            f"{queued['dialogue']}"
        )
    parts.append(f"Intent: {intent}")
    system = "\n\n".join(parts)
    conversation = [
        m for m in turn.messages[1:] if m["role"] in ("user", "assistant") and "tool_calls" not in m
    ]
    label = f"Speech step (round {turn.rounds})"
    speech = [{"role": "system", "content": system}, *conversation]
    reply = ""
    for attempt in range(SPEECH_ATTEMPTS):
        request = speech if attempt == 0 else [*speech, {"role": "system", "content": SPEECH_RETRY}]
        reply = (await complete(provider, model_name, request)).strip()
        if reply:
            break
    malformed = SPOKEN_MARKUP.search(reply) is not None
    _record(
        engine,
        campaign_id,
        SpeechWritten(
            speaker=speaker,
            dialogue="" if malformed else reply,
            replace=replace,
            context=_dicts(
                [
                    ContextPart("system", label, system),
                    ContextPart("assistant", f"{label} reply", reply),
                ]
            ),
        ),
    )
    if malformed:
        raise InvalidInput(f"the speech step wrote a tool call instead of {speaker}'s words")
    if not reply:
        # A tool error rather than a dead turn: the GM sees the failure in the tool result, and
        # can narrate around the missing line or ask for it again more simply. Ending the whole
        # reply because one NPC had nothing to say loses work the player already waited for.
        raise InvalidInput(
            f"the speech step returned no words for {speaker}; narrate the reply without their "
            "quoted line, or call speak again with a simpler intent"
        )


async def _begin(run: _Run, turn: Turn) -> AsyncIterator[TurnEvent]:
    """Set up the model's context for the turn. Once the prompt reaches the compression point, the
    older part of the chat is folded into the summary first."""
    state = _state(run.engine, run.campaign_id)
    rule_set = run.rule_set
    summary, watermark = state.summary, state.through
    world_text = _world_text(run, state)
    message = run.opening if turn.opening else turn.text
    history = [
        m
        for m in _campaigns().list_messages(run.engine, run.campaign_id)
        if watermark < m.id < turn.opener
    ]
    present_text = present_rules_text(state, run.ctx.rules)
    prompt_tokens = sum(
        p.tokens
        for p in system_parts(
            rule_set.id,
            {**rule_set.rules, **run.unattached_rules},
            rule_set.important,
            summary,
            watermark,
            world_text,
            present_text,
            run.setting,
        )
    )
    prompt_tokens += estimate_tokens(message) + sum(estimate_tokens(m.content) for m in history)
    summary_record: list[ContextPart] = []
    events: list[GameEvent] = []
    if prompt_tokens >= run.context_tokens * COMPRESS_AT:
        start = _campaigns()._verbatim_start(history, int(run.context_tokens * KEEP_RECENT))
        if start > 0:
            folded = history[:start]
            yield Status("Summarizing older messages...")
            summary = await _campaigns()._summarize(
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
    if turn.opening:
        label = "Opening instruction"
    elif turn.actor is not None:
        label = "Encounter instruction"
    else:
        label = "This message (player)"
    player = ContextPart("user", label, message)
    system = system_parts(
        rule_set.id,
        {**rule_set.rules, **run.unattached_rules},
        rule_set.important,
        summary,
        watermark,
        world_text,
        present_text,
        run.setting,
    )
    conversation = [
        *(ContextPart(m.role, _campaigns()._message_source(m), m.content) for m in history),
        player,
    ]
    context = [*system, *_campaigns().tool_definition_parts(rule_set), *conversation]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(p.content for p in system)},
        *(_history_message(m) for m in history),
        {"role": player.role, "content": player.content},
    ]
    round_start = len(context)
    context.append(
        ContextPart("system", "State instruction", state_instruction(run.rule_set, run.world))
    )
    events.append(
        TurnBegun(
            head=_dicts([*summary_record, player]),
            context=_dicts(context),
            messages=messages,
            round_start=round_start,
        )
    )
    _record(run.engine, run.campaign_id, *events)


async def _write_journal(
    run: _Run, turn: Turn, intro: str = "", reasoning: str = ""
) -> AsyncIterator[TurnEvent]:
    """Let the GM write what happened since the journal was last written into the journal before
    narrating the turn. That log is what the step sees, along with the turn's messages. The
    narrate prompt is left out of the view, since narration has not happened yet. An opening's
    introduction is passed as intro, since it is written before the journal step and the player
    reads it. The journal tools record their own events, so the turn's messages and Log are left
    as they are."""
    journal_tools = tool_specs(run.tool_calls, journal_step=True)
    if journal_tools is None:
        return
    yield Status("Updating the journal...")
    state = _state(run.engine, run.campaign_id)
    log = _turn_log(run.engine, run.campaign_id, state.journal_through or turn.opener)
    _record(run.engine, run.campaign_id, JournalStarted())
    instruction = journal_instruction(state.clock.day, state.journal)
    narrate = narrate_instruction(run.rule_set, run.world)
    view: list[dict[str, Any]] = [
        *(
            m
            for m in _narration_messages(
                run, turn, log=log, heading="Log since the journal was last written, in order:"
            )
            if m.get("content") != narrate
        ),
    ]
    if intro:
        view.append(_text_message(intro, reasoning))
    view.append({"role": "system", "content": instruction})
    for _round in range(JOURNAL_ROUNDS):
        step = await run.provider.chat(run.model_name, view, journal_tools)
        if not step.tool_requests:
            return
        view.append(_tool_call_message(step))
        for request in step.tool_requests:
            result = await run_tool(run.tool_calls, request, ctx=run.ctx)
            if isinstance(result, Ask):
                raise LLMError("the journal step asked the player a question")
            view.append({"role": "tool", "tool_call_id": request.id, "content": result.output})


@dataclass(frozen=True)
class _ReviewResult:
    """What a turn review decided. `undone` means the narration was set aside and the story must
    be written again from the record, after any tools the review ran to fix it."""

    undone: bool


def _tool_failed(result: ToolRun) -> bool:
    """Whether a tool result is the error object `run_tool` returns instead of an answer."""
    try:
        payload = json.loads(result.output)
    except (TypeError, ValueError):
        return True
    return isinstance(payload, Mapping) and "error" in payload


def _take_debug_injection(run: _Run) -> DebugInjection | None:
    """Take the debug panel's one-shot injection for this turn, clearing it from the campaign."""
    state = _state(run.engine, run.campaign_id)
    injection = state.debug_injection
    if injection is None:
        return None
    _record(run.engine, run.campaign_id, DebugInjectionCleared())
    return injection


async def _inject_debug_tool(
    run: _Run, turn: Turn, injection: DebugInjection
) -> AsyncIterator[TurnEvent]:
    """Run a debug-injected tool call as the GM's own action just before the narration, so the
    narrator and the review see a deliberately wrong action in the record."""
    yield Status(f"Injecting {injection.tool}...")
    request = ToolRequest(
        id=f"debug-injection-{turn.opener}",
        name=injection.tool,
        arguments=injection.arguments,
    )
    result = await run_tool(run.tool_calls, request, ctx=run.ctx)
    if isinstance(result, Ask):
        result = ToolRun(
            json.dumps(
                {
                    "error": "the debug-injected tool needs the player's answer; "
                    "choose a tool that runs without one"
                }
            ),
            None,
        )
    step = ChatReply(text="", tool_requests=(request,))
    messages = [
        _tool_call_message(step),
        {"role": "tool", "tool_call_id": request.id, "content": result.output},
    ]
    context = [
        ContextPart(
            "assistant",
            "Debug-injected GM tool call",
            f"{request.name}({request.arguments})",
        ),
        ContextPart("tool", f"Result of '{request.name}' (debug injection)", result.output),
    ]
    _record(
        run.engine,
        run.campaign_id,
        DebugInjectionRan(tool=request.name, messages=messages, context=_dicts(context)),
    )
    for entry in _flush(run):
        yield entry


async def _narrate(
    run: _Run, turn: Turn, result: list[tuple[str, str]], forced: str = ""
) -> AsyncIterator[TurnEvent]:
    """Stream the narration and put its text and reasoning in `result`. An empty reply is asked
    for again before the turn fails. `forced` is the debug panel's injected narration, which is
    streamed as if the model wrote it; it is not recorded itself, so the review sees it first."""
    yield Status("Narrating...")
    if forced:
        yield Delta(forced)
        result.append((forced, ""))
        return
    messages = _narration_messages(run, turn)
    reply = ""
    reasoning = ""
    for _attempt in range(NARRATION_ATTEMPTS):
        narration: ChatReply | None = None
        async for arrived, item in _stream_with_heartbeat(
            run.provider.stream(run.model_name, messages, None)
        ):
            if not arrived:
                yield Status(HEARTBEAT_MESSAGE)
            elif isinstance(item, TextDelta):
                yield Delta(item.text)
            else:
                narration = item
        if narration is None:
            raise LLMError("the model returned no reply")
        reply = narration.text.strip()
        reasoning = narration.reasoning
        if reply:
            break
    if not reply:
        raise LLMError("the model returned an empty reply")
    result.append((reply, reasoning))


async def _review_narration(
    run: _Run,
    turn: Turn,
    narration: str,
    reasoning: str,
    result: list[_ReviewResult],
) -> AsyncIterator[TurnEvent]:
    """Check the narration just written against the record. A review that finds a fault calls
    undo_narration, then may fix the record with the usual tools; the caller writes the story
    again when `result` says the narration was undone. A review that changes the record without
    undoing first still sends the story back, since the narration was written from the old state."""
    tools = tool_specs(run.tool_calls, review_step=True)
    if tools is None:
        result.append(_ReviewResult(undone=False))
        return
    yield Status("Reviewing the turn...")
    view = _narration_messages(run, turn)
    view.append(_text_message(narration, reasoning))
    view.append({"role": "system", "content": TURN_REVIEW_INSTRUCTION})
    undone = False
    changed = False
    for round_no in range(1, REVIEW_ROUNDS + 1):
        replied: list[ChatReply] = []
        async for event in _await_with_heartbeat(
            run.provider.chat(run.model_name, view, tools), replied
        ):
            yield event
        step = replied[0]
        if not step.tool_requests:
            break
        view.append(_tool_call_message(step))
        messages: list[dict[str, Any]] = [_tool_call_message(step)]
        context: list[ContextPart] = []
        if step.text.strip():
            context.append(ContextPart("assistant", f"Turn review (round {round_no})", step.text))
        for request in step.tool_requests:
            tool_result = await run_tool(run.tool_calls, request, ctx=run.ctx)
            if isinstance(tool_result, Ask):
                tool_result = ToolRun(
                    json.dumps(
                        {
                            "error": "the turn review cannot wait for the player; fix what "
                            "you can without asking, or undo the narration"
                        }
                    ),
                    None,
                )
            failed = _tool_failed(tool_result)
            if not failed and request.name == UNDO_NARRATION:
                if not undone:
                    yield Retract()
                    yield Status("Fixing the story...")
                undone = True
            elif not failed:
                tool = find_tool(run.tool_calls, request.name)
                if tool is not None and not tool.lookup:
                    changed = True
            message = {"role": "tool", "tool_call_id": request.id, "content": tool_result.output}
            messages.append(message)
            view.append(message)
            context.append(
                ContextPart(
                    "tool",
                    f"Result of '{request.name}' (review round {round_no})",
                    tool_result.output,
                )
            )
        _record(
            run.engine,
            run.campaign_id,
            ReviewStepMade(messages=messages, context=_dicts(context)),
        )
    if changed and not undone:
        _record(run.engine, run.campaign_id, NarrationUndone(reason=REVIEW_CHANGED_NOTE))
        yield Retract()
        undone = True
    result.append(_ReviewResult(undone=undone))


async def _drive(run: _Run) -> AsyncIterator[TurnEvent]:
    """Run the reply until it is recorded or paused. Log entries are yielded as the steps that
    recorded them finish. A pause is recorded before the player hears of it."""
    tools = tool_specs(run.tool_calls)
    ooc_tools = (
        [spec for spec in tools if spec["function"]["name"] != OOC_TOOL]
        if tools is not None
        else None
    )
    state = {"role": "system", "content": state_instruction(run.rule_set, run.world)}
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
        if turn.ooc:
            yield Status("Replying out of character...")
            round_no = turn.rounds + 1
            view = list(turn.messages)
            if turn.speech:
                view.append(_queued_dialogue_note(turn.speech))
            view.append({"role": "system", "content": OOC_INSTRUCTION})
            step: ChatReply | None = None
            async for arrived, item in _stream_with_heartbeat(
                run.provider.stream(run.model_name, view, ooc_tools)
            ):
                if not arrived:
                    yield Status(HEARTBEAT_MESSAGE)
                elif isinstance(item, TextDelta):
                    yield Delta(item.text)
                else:
                    step = item
            if step is None:
                raise LLMError("the model returned no reply")
            # The ooc tool is not offered here. A model that calls it anyway must not restart the
            # out-of-character step; the state step already decided whether the story continues.
            requests = [request for request in step.tool_requests if request.name != OOC_TOOL]
            if requests:
                filtered = ChatReply(
                    text=step.text, tool_requests=tuple(requests), reasoning=step.reasoning
                )
                context = []
                if step.text:
                    context.append(
                        ContextPart(
                            "assistant", f"GM out-of-character text (round {round_no})", step.text
                        )
                    )
                _record(
                    run.engine,
                    run.campaign_id,
                    StateStepMade(
                        requests=[asdict(request) for request in requests],
                        messages=[_tool_call_message(filtered)],
                        context=_dicts(context),
                    ),
                )
                continue
            reply = step.text.strip()
            if not reply:
                raise LLMError("the model returned an empty reply")
            if turn.ooc_narrate:
                messages: list[dict[str, Any]] = [_text_message(reply, step.reasoning)]
                context = [ContextPart("assistant", "GM out-of-character reply", reply)]
                if turn.speech:
                    _add_speech(messages, context, turn.speech)
                _add_instruction(
                    messages,
                    context,
                    "Narrate instruction",
                    narrate_instruction(run.rule_set, run.world),
                )
                _record(
                    run.engine,
                    run.campaign_id,
                    StateStepMade(requests=[], messages=messages, context=_dicts(context)),
                    ChatGM(
                        content=reply,
                        model=run.model,
                        final=False,
                        reasoning=step.reasoning,
                    ),
                )
                yield NextMessage()
                continue
            if turn.opening:
                async for event in _write_journal(run, turn, reply, step.reasoning):
                    yield event
            event_id = _record(
                run.engine,
                run.campaign_id,
                ChatGM(content=reply, model=run.model, reasoning=step.reasoning),
            )
            yield Saved(event_id)
            return
        yield Status("Managing state...")
        round_no = turn.rounds + 1
        view = [*turn.messages, state]
        if turn.speech:
            view.append(_queued_dialogue_note(turn.speech))
        replied: list[ChatReply] = []
        async for event in _await_with_heartbeat(
            run.provider.chat(run.model_name, view, tools), replied
        ):
            yield event
        step = replied[0]
        if not step.tool_requests:
            messages: list[dict[str, Any]] = []
            context: list[ContextPart] = []
            if step.text.strip():
                context.append(
                    ContextPart(
                        "assistant",
                        f"GM state step (round {round_no}), not shown to the narration",
                        step.text,
                    )
                )
            if turn.speech:
                _add_speech(messages, context, turn.speech)
            _add_instruction(
                messages,
                context,
                "Narrate instruction",
                narrate_instruction(run.rule_set, run.world),
            )
            _record(
                run.engine,
                run.campaign_id,
                StateStepMade(requests=[], messages=messages, context=_dicts(context)),
            )
            continue
        context = []
        if step.text:
            context.append(ContextPart("assistant", f"GM text (round {round_no})", step.text))
        _record(
            run.engine,
            run.campaign_id,
            StateStepMade(
                requests=[asdict(request) for request in step.tool_requests],
                messages=[_tool_call_message(step)],
                context=_dicts(context),
            ),
        )

    injection = _take_debug_injection(run)
    forced_narration = ""
    if injection is not None and injection.mode == "tool":
        async for event in _inject_debug_tool(run, turn, injection):
            yield event
        turn = _turn(_state(run.engine, run.campaign_id))
    elif injection is not None and injection.mode == "narration":
        forced_narration = injection.text
    if _turn_has_day_end(run.engine, run.campaign_id, turn.opener):
        async for event in _write_journal(run, turn):
            yield event
    reply = ""
    reasoning = ""
    for review_round in range(NARRATION_REVIEWS + 1):
        turn = _turn(_state(run.engine, run.campaign_id))
        written: list[tuple[str, str]] = []
        async for event in _narrate(
            run,
            turn,
            written,
            forced=forced_narration if review_round == 0 else "",
        ):
            yield event
        reply, reasoning = written[0]
        if review_round >= NARRATION_REVIEWS:
            break
        reviewed: list[_ReviewResult] = []
        async for event in _review_narration(run, turn, reply, reasoning, reviewed):
            yield event
        if not reviewed[0].undone:
            break
    event_id = _record(
        run.engine,
        run.campaign_id,
        ChatGM(content=reply, model=run.model, reasoning=reasoning),
    )
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
            speaker = speaker_name(_state(run.engine, run.campaign_id))
            _record(run.engine, run.campaign_id, ChatPlayer(content=answer.text, speaker=speaker))
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
    campaign = _campaigns().get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _campaigns()._exclusive(campaign.id):
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
        current = _state(engine, campaign.id)
        player = current.pc()
        if player is None:
            raise InvalidInput("create your character before playing")
        if player.out_of_play:
            state = "is dead" if player.dead else "has left the story"
            raise InvalidInput(
                f"your character {state}: choose a new character or a hireling first"
            )
        if advancement_due(current, rule_set):
            raise InvalidInput("level up or choose your Advancement before sending another message")
        run = _start_run(engine, providers, library, rule_set, campaign, context_tokens, rng)
        speaker = speaker_name(_state(engine, campaign.id))
        _record(engine, campaign.id, ChatPlayer(content=text, speaker=speaker))
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
    campaign = _campaigns().get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _campaigns()._exclusive(campaign.id):
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
            answer = RollAnswer(roll_total(turn.prompt.dice, rng, plus=rule_set.plus))
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


async def start_encounter_turn(
    engine: Engine,
    providers: ProviderFactory,
    allowed_models: Iterable[str],
    library: ContentLibrary,
    context_tokens: int,
    owner_id: int,
    campaign_id: int,
    name: str,
    rng: random.Random | None = None,
) -> AsyncIterator[TurnEvent]:
    """Start the NPC whose turn is next in the encounter order. The GM plays it out and it is
    streamed like a reply. The engine ends the NPC's turn when the reply finishes, so the player
    only presses Start for another actor's turn."""
    campaign = _campaigns().get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _campaigns()._exclusive(campaign.id):
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        if advancement_due(_state(engine, campaign.id), rule_set):
            raise InvalidInput("level up or choose your Advancement before starting a turn")
        with session_scope(engine) as db:
            character = encounters.begin_turn(db, campaign.id, name)
        if character.kind == PC_KIND:
            raise InvalidInput(
                f"{character.name} is a player character; press Start on the page instead"
            )
        run = _start_run(engine, providers, library, rule_set, campaign, context_tokens, rng)
        async for event in _guarded(run, _carry_on(run)):
            yield event
        # A reply that finished normally leaves no open turn, so the NPC's turn is over. A reply
        # waiting on the player's roll keeps the turn open until that answer is resolved.
        with session_scope(engine) as db:
            state = load_state(db, campaign_id)
            encounter = state.encounter
            if (
                state.turn is None
                and encounter is not None
                and encounter.acting is not None
                and name_key(encounter.acting) == name_key(character.name)
            ):
                encounters.end_turn(db, campaign_id, character.name, by_player=False)


def begin_encounter_turn(engine: Engine, owner_id: int, campaign_id: int, name: str) -> None:
    """Start a player character's turn from the encounter controls. The player then acts in chat
    and presses End your turn. Refused while the GM's reply is still open."""
    _campaigns().get_campaign(engine, owner_id, campaign_id)
    with _campaigns()._exclusive(campaign_id):
        if _state(engine, campaign_id).turn is not None:
            raise InvalidInput("the GM's reply is unfinished; finish it before starting a turn")
        with session_scope(engine) as db:
            character = encounters.begin_turn(db, campaign_id, name)
        if character.kind != PC_KIND:
            raise InvalidInput(
                f"{character.name} is not a player character; start their turn from the page"
            )


def end_encounter_turn(engine: Engine, owner_id: int, campaign_id: int, name: str) -> None:
    """End the acting character's turn from the encounter controls. The player ends their own
    character's turn and the GM ends an NPC's, but both are button-driven: a reply ending does not
    advance the order by itself. Refused while the GM's reply is still open."""
    _campaigns().get_campaign(engine, owner_id, campaign_id)
    with _campaigns()._exclusive(campaign_id):
        if _state(engine, campaign_id).turn is not None:
            raise InvalidInput("the GM's reply is unfinished; finish it before ending the turn")
        with session_scope(engine) as db:
            character = load_state(db, campaign_id).characters.get(name_key(name))
            if character is None:
                raise InvalidInput(f"there is no character named {name!r}")
            encounters.end_turn(db, campaign_id, name, by_player=character.kind == PC_KIND)


def end_player_turn(engine: Engine, owner_id: int, campaign_id: int, name: str) -> None:
    """End a player character's turn. Refused while the GM's reply is still open."""
    _campaigns().get_campaign(engine, owner_id, campaign_id)
    with _campaigns()._exclusive(campaign_id):
        if _state(engine, campaign_id).turn is not None:
            raise InvalidInput("the GM's reply is unfinished; finish it before ending your turn")
        with session_scope(engine) as db:
            encounters.end_turn(db, campaign_id, name, by_player=True)


@dataclass(frozen=True)
class PendingView:
    """An open reply that needs attention: its prompt, or why it stopped. The player's words and
    the rest of the turn are already in the transcript and the Log. `auto_continue` is set for the
    campaign's opening before it has failed, so the page can start it without the player."""

    prompt: Ask | None
    error: str | None = None
    opening: bool = False
    auto_continue: bool = False


def pending_turn(engine: Engine, owner_id: int, campaign_id: int) -> PendingView | None:
    """The open reply's prompt, or why it stopped. None when no reply is open, or while one is
    running in this process."""
    _campaigns().get_campaign(engine, owner_id, campaign_id)
    with _campaigns()._busy_lock:
        if campaign_id in _campaigns()._busy_campaigns:
            return None
    turn = _state(engine, campaign_id).turn
    if turn is None:
        return None
    if turn.prompt is not None:
        return PendingView(prompt=turn.prompt, opening=turn.opening)
    auto = turn.opening and turn.failed is None
    return PendingView(
        prompt=None,
        error=turn.failed or "The GM's reply did not finish.",
        opening=turn.opening,
        auto_continue=auto,
    )


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
    _campaigns().get_campaign(engine, owner_id, campaign_id)
    with _campaigns()._exclusive(campaign_id), session_scope(engine) as db:
        event = db.get(Event, event_id)
        if event is None or event.campaign_id != campaign_id or event.kind != ChatGM.kind:
            raise NotFound("message not found")
        truncate(db, campaign_id, event_id)
        db.commit()


def discard_pending(engine: Engine, owner_id: int, campaign_id: int) -> None:
    """Remove the open reply and the player's message that started it, so the player can send
    the message again. Used when a reply cannot be continued. The opening cannot be discarded: it
    is recorded with the player character, and it is continued instead."""
    _campaigns().get_campaign(engine, owner_id, campaign_id)
    with _campaigns()._exclusive(campaign_id), session_scope(engine) as db:
        turn = load_state(db, campaign_id).turn
        if turn is None:
            raise NotFound("there is no open reply to discard")
        if turn.opening:
            raise InvalidInput("the campaign's opening cannot be discarded; continue it instead")
        truncate(db, campaign_id, turn.opener - 1)
        db.commit()


def campaign_log(engine: Engine, owner_id: int, campaign_id: int) -> list[str]:
    """The Log's entries, oldest first."""
    _campaigns().get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, channel=Channel.LOG)
        return [text for row in rows if (text := decode(row).render()) is not None]


def _turn_for_reply(
    engine: Engine, owner_id: int, campaign_id: int, event_id: int
) -> tuple[Turn | None, str]:
    """The turn that produced a GM reply, as it stood before the reply was recorded, and the
    reply's text. The turn is None when no turn was open, so nothing was recorded for the reply."""
    _campaigns().get_campaign(engine, owner_id, campaign_id)
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
