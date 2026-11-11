from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import AsyncIterator, Awaitable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, select

from cygnusloom.content.library import (
    ContentLibrary,
    RuleSet,
    ToolCall,
    WorldContent,
    WorldTemplate,
    effective_table_genres,
    tables_for_genres,
)
from cygnusloom.db.models import Campaign, Event
from cygnusloom.llm.base import (
    ChatReply,
    LLMError,
    Provider,
    TextDelta,
    ToolRequest,
    complete,
)
from cygnusloom.llm.registry import ProviderFactory
from cygnusloom.rules.context import ToolContext, brief
from cygnusloom.rules.dice import roll_total
from cygnusloom.services import encounters
from cygnusloom.services.common import InvalidInput, NotFound, session_scope
from cygnusloom.services.credits import (
    CreditPolicy,
    MeteredProvider,
    UsageMeter,
    refund_replies,
)
from cygnusloom.services.events import (
    NPC_KIND,
    PC_KIND,
    CampaignState,
    Channel,
    Character,
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
from cygnusloom.services.level_up import advancement_due
from cygnusloom.services.prompts import (
    NARRATION_REVISION,
    OOC_INSTRUCTION,
    OPENING_REPLY_INSTRUCTION,
    TURN_REVIEW_INSTRUCTION,
    journal_instruction,
    journal_review_instruction,
    narrate_instruction,
    opening_prompt,
    speech_instruction,
    state_instruction,
    system_parts,
)
from cygnusloom.services.tools import (
    LOAD_TOOL,
    TOOL_GROUPS,
    Ask,
    ChoiceAnswer,
    RollAnswer,
    TextAnswer,
    ToolRun,
    run_tool,
    state_tool_groups,
    wire_tool_specs,
)
from cygnusloom.services.views import present_rules_text, world_state_text

if TYPE_CHECKING:
    from cygnusloom.services.campaigns import ChatMessage

from cygnusloom.services.world import (
    WorldTools,
)

logger = logging.getLogger(__name__)

MAX_NAME = 120
MAX_MESSAGE = 8000
# A tool call written as text instead of a structured request shows up as a DSML block, an
# OpenAI-style special token, an XML invoke/function tag, or `name({...})`. Plain angle brackets
# alone are not enough: a player may write HTML, and the narration may quote it.
TOOL_MARKUP = re.compile(
    r"｜｜DSML｜｜|<\|[^>\n]*\|>|<tool_calls?\b|<function_call\b|<invoke\b|<function\s*=|"
    r"\b\w+\(\s*\{"
)
# An XML tag whose name is a real tool is a tool call written as text, such as
# `<get_statblock name="Mara"/>`.
_TAG_NAME = re.compile(r"</?([A-Za-z_][\w-]*)\b")


def _is_tool_markup(text: str, tool_names: Iterable[str]) -> bool:
    if TOOL_MARKUP.search(text):
        return True
    names = {name.casefold() for name in tool_names}
    return any(match.group(1).casefold() in names for match in _TAG_NAME.finditer(text))


# An empty speech reply is retried this many times before the step gives up.
SPEECH_ATTEMPTS = 3
SPEECH_RETRY = (
    "Your last reply was empty. Write the line now: only the words the speaker says aloud, "
    "one line, and nothing else."
)
# An empty or malformed narration is requested again this many times before the turn fails.
NARRATION_ATTEMPTS = 2
NARRATION_RETRY = (
    "Your last reply was empty or was a tool call written as text, not narration. The tool step "
    "is over and this step writes prose only. Write the narration now, with no tool calls, and "
    "use only the queued dialogue already given above for an NPC's words."
)
# The narration is reviewed after it is written. A review that sets it aside writes the story
# again, up to this many times; the last narration is accepted without another review, but it
# must still be non-empty prose rather than a tool call.
NARRATION_REVIEWS = 2
# A review step's model rounds: enough to undo, fix the record, and stop.
REVIEW_ROUNDS = 3
# The state-step tool that marks a message out of character. The out-of-character step must not
# be offered it again, or the model can restart that step instead of replying.
OOC_TOOL = "ooc"
# The review tool that sets the narration aside.
UNDO_NARRATION = "undo_narration"
# The note recorded when the review set the narration aside but gave no reason.
REVIEW_UNDONE_NOTE = "the narration did not match the record"
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
    from cygnusloom.services import campaigns

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


def _history_message(m: ChatMessage, multiplayer: bool = False) -> dict[str, Any]:
    """A chat message as the model sees it. A GM reply keeps the reasoning it was written with.
    In a campaign with several players, each player message names the character it speaks for
    and the account that wrote it, so the GM can tell the players apart."""
    if m.role == "assistant":
        return _text_message(m.content, m.reasoning)
    who = ""
    if multiplayer and m.speaker:
        who = f"{m.speaker} ({m.player_name})" if m.player_name else m.speaker
    return {"role": m.role, "content": f"{who}: {m.content}" if who else m.content}


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


def _loaded_group(request: ToolRequest) -> str:
    try:
        arguments = json.loads(request.arguments or "{}")
    except ValueError:
        return ""
    group = arguments.get("group") if isinstance(arguments, dict) else None
    return group.strip().casefold() if isinstance(group, str) else ""


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


def _choice_fields(choice: object) -> dict[str, str]:
    """One prompt choice's id, label, and text, from a MoveChoice or the JSON the tool returned."""
    if isinstance(choice, Mapping):
        return {key: str(choice.get(key, "")) for key in ("id", "label", "text")}
    return {
        "id": str(getattr(choice, "id", "")),
        "label": str(getattr(choice, "label", "")),
        "text": str(getattr(choice, "text", "")),
    }


def _prompt_shown(ask: Ask) -> PromptShown:
    """The event that stores a prompt, so a reload or another player sees it."""
    return PromptShown(
        text=ask.text,
        dice=ask.dice,
        details=ask.details,
        choices=[_choice_fields(choice) for choice in ask.choices],
        move=ask.move,
        band=ask.band,
        roll=ask.roll,
        total=ask.total,
    )


def _record(engine: Engine, campaign_id: int, *events: GameEvent) -> int:
    """Record events in one commit. Returns the id of the last one."""
    with session_scope(engine) as db:
        event_id = 0
        for event in events:
            event_id = record(db, campaign_id, event)
        db.commit()
    return event_id


def _after_reply(run: _Run, event_id: int) -> None:
    """Finish a GM reply's accounting: attach its measured usage, then charge its price. Usage
    is recorded even for an exempt account so administrators can see real model cost. A reply
    that is already saved must not be lost if accounting fails; the failure is logged."""
    try:
        run.meter.assign(event_id)
    except Exception:
        logger.exception("could not record model usage for reply %s", event_id)
    try:
        run.credits.charge(event_id)
    except Exception:
        logger.exception("could not charge credits for reply %s", event_id)


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
    credits: CreditPolicy
    meter: UsageMeter
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
    # Whether the campaign enforces inventory space. Off means anything reasonable is carried.
    tracks_inventory: bool = True
    # The account answering this turn's prompt, when a player does. An opening or an NPC's turn
    # has none, and any player at the table may continue it. Its display name is resolved from
    # the account when the log is folded, so a name change reaches an open turn.
    player_id: int | None = None
    # How much of each tool schema and rule list reaches the model. The engine keeps the full
    # authored schema for validation; these modes only change the wire prompt.
    tool_schema_mode: str = "brief"
    rule_info_mode: str = "brief"
    # Groups the model asked for with load_tool during this turn.
    extra_groups: set[str] = field(default_factory=set)

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
    table_genres: Sequence[str] | None = None,
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
            (tool.name for tool in rule_set.tool_calls.values()),
        )

    def ooc(narrate: bool) -> None:
        _record(engine, campaign_id, OocStarted(narrate=narrate))

    # A campaign narrows the rule set's generation tables; a world's own tables are authored for
    # it and stay as they are. No selection keeps the rule set's choice rather than dropping every
    # tagged table.
    genres = tuple(table_genres) if table_genres else rule_set.table_genres
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
        faction_assets=rule_set.faction_assets,
        moves=rule_set.moves,
        counters=rule_set.counters,
        advancements=rule_set.advancements,
        periods=rule_set.periods,
        tallies=rule_set.tallies,
        relations=rule_set.relations,
        dice_tables={**tables_for_genres(rule_set.dice_tables, genres), **world.dice_tables},
        clock_rules=rule_set.clock,
        rests=rule_set.rests,
        interrupted_rest=rule_set.interrupted_rest,
        hooks=rule_set.hooks,
        condition_ends=rule_set.condition_ends,
        inventory=rule_set.inventory,
        item_sizes=rule_set.item_sizes,
        magic=rule_set.magic,
        marketplace=rule_set.marketplace,
        plus=rule_set.plus,
        npc_hp=rule_set.npc_hp,
        npc_hp_stat=rule_set.npc_hp_stat,
        travel_modes=rule_set.travel,
        journey=rule_set.journey,
        wilderness=rule_set.wilderness,
        world_terrain=rule_set.world_terrain,
        housekeeping=rule_set.housekeeping,
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
    player_id: int | None = None,
) -> _Run:
    provider, model_name = providers(campaign.model)
    settings = getattr(providers, "settings", None)
    credits = CreditPolicy.for_campaign(engine, settings, campaign)
    meter = UsageMeter(engine, campaign.id or 0, campaign.owner_id, campaign.model)
    metered = MeteredProvider(provider, meter)
    world = library.world_content(campaign.world_template, campaign.rule_set)
    template = library.world_templates.get(campaign.world_template)
    return _Run(
        engine=engine,
        campaign_id=campaign.id or 0,
        model=campaign.model,
        rule_set=rule_set,
        context_tokens=context_tokens,
        provider=metered,
        model_name=model_name,
        credits=credits,
        meter=meter,
        ctx=_tool_context(
            engine,
            rng if rng is not None else random.Random(),
            metered,
            model_name,
            campaign.id,
            rule_set,
            world,
            table_genres=effective_table_genres(campaign.table_genres, rule_set),
        ),
        cursor=_newest(engine, campaign.id),
        opening=opening_prompt(template),
        unattached_rules=world.unattached_rules,
        world=template,
        setting=campaign.setting,
        tracks_inventory=campaign.use_inventory_space,
        player_id=player_id,
        tool_schema_mode=getattr(settings, "tool_schema_mode", "brief"),
        rule_info_mode=getattr(settings, "rule_info_mode", "brief"),
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


def _multiplayer(state: CampaignState) -> bool:
    """Whether more than one player character is at the table. A shared campaign whose other
    players have not made a character yet plays exactly like a single-player one."""
    return len(state.pcs()) > 1


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
        tallies=rule_set.tallies,
        unwritten_world=run.world is not None and not run.world.has_setting,
        track_inventory=run.tracks_inventory,
        currency=rule_set.marketplace.currency if rule_set.marketplace else "",
        housekeeping=rule_set.housekeeping,
    )


def _narration_messages(
    run: _Run,
    turn: Turn,
    log: list[str] | None = None,
    heading: str = "Turn log, everything in this turn so far, in order:",
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
        rule_info_mode=run.rule_info_mode,
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
    # The review's own words are lost from the tool-call messages, which keep no prose. Put the
    # latest fault back for the rewrite so it corrects what the reviewer actually named. Passing
    # verdicts and older faults are left out: they would describe a narration that is gone.
    review_notes = _review_fault_notes(turn)
    if review_notes:
        notes = "\n\n".join(f"{part.source}:\n{part.content}" for part in review_notes)
        messages.append(
            {
                "role": "system",
                "content": "The turn review's notes on the narration it set aside:\n\n" + notes,
            }
        )
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
    tool_names: Iterable[str],
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
    malformed = _is_tool_markup(reply, tool_names)
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
    state_text = state_instruction(run.rule_set, run.world)
    groups = state_tool_groups(state, message) | run.extra_groups
    tool_parts = _campaigns().tool_definition_parts(
        rule_set, run.tool_schema_mode, include_groups=groups, load_tools=True
    )
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
            rule_info_mode=run.rule_info_mode,
        )
    )
    prompt_tokens += estimate_tokens(state_text)
    prompt_tokens += sum(part.tokens for part in tool_parts)
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
    # With several players at the table, the GM needs to know whose character is acting and
    # which account wrote the message.
    multiplayer = _multiplayer(state)
    if multiplayer and not turn.opening and turn.player_id is not None:
        speaker = speaker_name(state, turn.player_id)
        who = f"{speaker} ({turn.player_name})" if turn.player_name else speaker
        if who:
            message = f"{who}: {message}"
            label = f"This message (player {who})"
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
        rule_info_mode=run.rule_info_mode,
    )
    conversation = [
        *(ContextPart(m.role, _campaigns()._message_source(m), m.content) for m in history),
        player,
    ]
    context = [*system, *tool_parts, *conversation]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(p.content for p in system)},
        *(_history_message(m, multiplayer) for m in history),
        {"role": player.role, "content": player.content},
    ]
    round_start = len(context)
    context.append(ContextPart("system", "State instruction", state_text))
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
    journal_tools = wire_tool_specs(run.tool_calls, journal_step=True, mode=run.tool_schema_mode)
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


# Context the narration never sees: the speech step's profile and intent. The review is shown it
# so it can judge whether the narration invented dialogue. The state step's prose is a draft
# written before its tools ran, not part of the record, so neither the narration nor the review
# reads it.
_REVIEW_ONLY_SOURCES = ("Speech step (round",)


def _review_fault_notes(turn: Turn) -> list[ContextPart]:
    """The reviewer's prose from its latest undo. A tool-call message keeps no prose, so this is
    where the reviewer explains the fault beyond the undo reason. Passing verdicts are left out:
    they say the narration was sound and would confuse a rewrite."""
    notes = [part for part in turn.context if part.source.startswith("Turn review fault (round")]
    return notes[-1:]


def _reviewed_undo_request(request: ToolRequest, review_text: str) -> ToolRequest:
    """Use the review's own words as the undo reason when the review left it blank. An explicit
    reason is kept, since it is the fault the reviewer chose to name. A malformed call is left
    alone and fails."""
    if not review_text:
        return request
    try:
        arguments = json.loads(request.arguments)
    except (TypeError, ValueError):
        return request
    if not isinstance(arguments, Mapping) or arguments.get("reason", "").strip():
        return request
    return ToolRequest(
        id=request.id,
        name=request.name,
        arguments=json.dumps({**arguments, "reason": review_text}),
    )


def _review_only_context(turn: Turn) -> list[ContextPart]:
    """The parts of the turn's context that the narration is not given. The speech step's profile
    and intent are included. Its reply repeats dialogue the review already has from the queued
    lines, so only the system part is added."""
    return [
        part
        for part in turn.context
        if part.source.startswith(_REVIEW_ONLY_SOURCES) and part.role == "system"
    ]


def _review_view(
    run: _Run,
    turn: Turn,
    log: list[str] | None = None,
    heading: str = "Turn log, everything in this turn so far, in order:",
) -> list[dict[str, Any]]:
    """What the review reads before the narration. It leaves out the narration instruction and
    the narrator's rewrite note, and replaces the latter with a review note naming the fault the
    rewrite was meant to fix. Earlier reviews' prose is left out: the review judges this narration
    against the record, not a review of an earlier version."""
    instruction = narrate_instruction(run.rule_set, run.world)
    view = [
        message
        for message in _narration_messages(run, turn, log=log, heading=heading)
        if not (
            message.get("role") == "system"
            and (
                message.get("content") == instruction
                or str(message.get("content", "")).startswith(
                    (
                        "The last narration was set aside because:",
                        "The turn review's notes on the narration it set aside:",
                    )
                )
            )
        )
    ]
    if turn.revision:
        view.append(
            {
                "role": "system",
                "content": "The last narration was set aside because: "
                + turn.revision
                + " This narration is the rewrite. Check that it fixes that fault, and judge "
                "the rest of the turn afresh.",
            }
        )
    return view


def _end_npc_encounter_turn(run: _Run, actor: str) -> None:
    """End the NPC encounter turn this reply plays out, before the reply is recorded. A reply
    that completed must be saved even if the turn can no longer be ended, so a missing or
    mismatched acting character is ignored."""
    with session_scope(run.engine) as db:
        state = load_state(db, run.campaign_id)
        encounter = state.encounter
        if (
            encounter is None
            or encounter.acting is None
            or name_key(encounter.acting) != name_key(actor)
        ):
            return
        character = state.characters.get(name_key(actor))
        if character is None or character.kind != NPC_KIND:
            return
        try:
            encounters.end_turn(db, run.campaign_id, character.name, by_player=False)
        except (InvalidInput, KeyError):
            # The story is done; leaving the turn open is better than losing the reply.
            db.rollback()


def _finish_reply(run: _Run, turn: Turn, reply: str, reasoning: str) -> int:
    """Record the end of the NPC encounter turn a final reply plays out, then the final reply
    itself. Both go through the same driver path, so a resumed or continued reply ends its turn
    too, and a rollback to the reply keeps the turn-end event before it."""
    if turn.actor is not None:
        _end_npc_encounter_turn(run, turn.actor)
    event_id = _record(
        run.engine,
        run.campaign_id,
        ChatGM(content=reply, model=run.model, reasoning=reasoning),
    )
    _after_reply(run, event_id)
    # With the reply closed, the next player character's turn can start on its own. This also
    # catches a player's initiative roll that settled the order during the reply.
    with session_scope(run.engine) as db:
        encounters.begin_player_turn_if_next(db, run.campaign_id)
    return event_id


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
    """Stream the narration and put its text and reasoning in `result`. An empty reply, or a
    reply that is a tool call written as text, is asked for again before the turn fails. `forced`
    is the debug panel's injected narration, which is streamed as if the model wrote it; it is
    not recorded itself, so the review sees it first."""
    yield Status("Narrating...")
    if forced:
        yield Delta(forced)
        result.append((forced, ""))
        return
    messages = _narration_messages(run, turn)
    reply = ""
    reasoning = ""
    malformed = False
    for attempt in range(NARRATION_ATTEMPTS):
        request = (
            messages
            if attempt == 0
            else [*messages, {"role": "system", "content": NARRATION_RETRY}]
        )
        narration: ChatReply | None = None
        streamed = False
        async for arrived, item in _stream_with_heartbeat(
            run.provider.stream(run.model_name, request, None)
        ):
            if not arrived:
                yield Status(HEARTBEAT_MESSAGE)
            elif isinstance(item, TextDelta):
                streamed = True
                yield Delta(item.text)
            else:
                narration = item
        if narration is None:
            raise LLMError("the model returned no reply")
        text = narration.text.strip()
        malformed = bool(narration.tool_requests) or _is_tool_markup(
            text, (tool.name for tool in run.tool_calls.values())
        )
        if text and not malformed:
            reply = text
            reasoning = narration.reasoning
            break
        if streamed:
            # The player saw the rejected text; clear it before the retry writes in its place.
            yield Retract()
    if not reply:
        if malformed:
            raise LLMError("the model wrote a tool call instead of the narration")
        raise LLMError("the model returned an empty reply")
    result.append((reply, reasoning))


async def _review_narration(
    run: _Run,
    turn: Turn,
    narration: str,
    reasoning: str,
    result: list[_ReviewResult],
    journal: bool = False,
) -> AsyncIterator[TurnEvent]:
    """Check the narration just written against the record. A review that finds a fault calls
    undo_narration; only after that may it use the other tools to correct the record. A record
    change without an undo is refused, so a passing narration is never sent back just because
    the review touched the record. The caller writes the story again when `result` says the
    narration was undone. With `journal`, the same step first brings the journal up to date, so a
    normal turn needs one model call for both instead of a separate journal call."""
    tools = wire_tool_specs(run.tool_calls, review_step=True, mode=run.tool_schema_mode)
    if tools is None:
        result.append(_ReviewResult(undone=False))
        return
    journal_tools = (
        wire_tool_specs(run.tool_calls, journal_step=True, mode=run.tool_schema_mode) or []
    )
    journal = journal and bool(journal_tools)
    journal_names: set[str] = set()
    if journal:
        tools = [*tools, *journal_tools]
        journal_names = {spec["function"]["name"] for spec in journal_tools}
    allowed = {spec["function"]["name"] for spec in tools}
    read_only = {tool.name for tool in run.tool_calls.values() if tool.lookup or tool.review_step}
    yield Status(
        "Updating the journal and reviewing the turn..." if journal else "Reviewing the turn..."
    )
    journal_state = None
    if journal:
        # The journal covers the log since the last journal step, which can reach back past this
        # turn after an out-of-character reply or a failed reply. Read that log before this step's
        # JournalStarted marks it covered.
        journal_state = _state(run.engine, run.campaign_id)
        log = _turn_log(run.engine, run.campaign_id, journal_state.journal_through or turn.opener)
        _record(run.engine, run.campaign_id, JournalStarted())
        view = _review_view(
            run, turn, log=log, heading="Log since the journal was last written, in order:"
        )
    else:
        view = _review_view(run, turn)
    view.append(_text_message(narration, reasoning))
    hidden = _review_only_context(turn)
    if hidden:
        lines = "\n\n".join(f"{part.source}:\n{part.content}" for part in hidden)
        view.append(
            {
                "role": "system",
                "content": "The speech step's profiles and intents for the queued dialogue:\n\n"
                + lines,
            }
        )
    if journal and journal_state is not None:
        view.append(
            {
                "role": "system",
                "content": journal_review_instruction(
                    journal_state.clock.day, journal_state.journal
                ),
            }
        )
    else:
        view.append({"role": "system", "content": TURN_REVIEW_INSTRUCTION})
    undone = False
    for round_no in range(1, REVIEW_ROUNDS + 1):
        replied: list[ChatReply] = []
        async for event in _await_with_heartbeat(
            run.provider.chat(run.model_name, view, tools), replied
        ):
            yield event
        step = replied[0]
        step_text = step.text.strip()
        if not step.tool_requests:
            context: list[ContextPart] = []
            if step.text.strip():
                context.append(
                    ContextPart("assistant", f"Turn review verdict (round {round_no})", step.text)
                )
            _record(
                run.engine,
                run.campaign_id,
                ReviewStepMade(messages=[], context=_dicts(context)),
            )
            break
        view.append(_tool_call_message(step))
        messages: list[dict[str, Any]] = [_tool_call_message(step)]
        context = []
        if step.text.strip():
            # Only the prose on an undo round is a fault the rewrite must correct. Prose on a
            # lookup round is background, and a passing verdict says the narration was sound.
            label = (
                "Turn review fault"
                if any(request.name == UNDO_NARRATION for request in step.tool_requests)
                else "Turn review"
            )
            context.append(ContextPart("assistant", f"{label} (round {round_no})", step.text))
        for request in step.tool_requests:
            if request.name not in allowed:
                # The tool list is the review's whole authority. A model that names a world tool
                # anyway must not change the record after the narration was written.
                tool_result = ToolRun(
                    json.dumps(
                        {
                            "error": "that tool is not available to the turn review; "
                            "call undo_narration, then use an offered tool to correct the "
                            "record"
                        }
                    ),
                    None,
                )
            elif request.name not in read_only and request.name not in journal_names and not undone:
                # A record change before the narration is set aside would leave the player
                # reading a story written from the old state. Refuse it instead of guessing. The
                # journal is the exception: it is written from the record, not the narration.
                tool_result = ToolRun(
                    json.dumps(
                        {
                            "error": "call undo_narration before changing the record; a "
                            "passing turn changes nothing"
                        }
                    ),
                    None,
                )
            elif request.name == UNDO_NARRATION and undone:
                tool_result = ToolRun(
                    json.dumps({"undone": "narration", "reason": step_text}), None
                )
            else:
                if request.name == UNDO_NARRATION:
                    request = _reviewed_undo_request(request, step_text)
                tool_result = await run_tool(run.tool_calls, request, ctx=run.ctx)
            if isinstance(tool_result, Ask):
                tool_result = ToolRun(
                    json.dumps(
                        {
                            "error": "the turn review cannot wait for the player; undo the "
                            "narration or pass without asking"
                        }
                    ),
                    None,
                )
            if request.name == UNDO_NARRATION:
                if _tool_failed(tool_result):
                    # The undo tool records nothing when it fails, so the review records the
                    # fault itself and sends the narration back anyway.
                    _record(
                        run.engine,
                        run.campaign_id,
                        NarrationUndone(reason=step_text or REVIEW_UNDONE_NOTE),
                    )
                if not undone:
                    yield Retract()
                    yield Status("Fixing the story...")
                undone = True
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
        if undone:
            # The undo records the fault, and any correction belongs in the same reply. Another
            # round would only let the review rewrite the story or pile on more changes.
            break
    result.append(_ReviewResult(undone=undone))


async def _drive(run: _Run) -> AsyncIterator[TurnEvent]:
    """Run the reply until it is recorded or paused. Log entries are yielded as the steps that
    recorded them finish. A pause is recorded before the player hears of it."""
    state_message = {"role": "system", "content": state_instruction(run.rule_set, run.world)}
    while True:
        state = _state(run.engine, run.campaign_id)
        turn = _turn(state)
        text = run.opening if turn.opening else turn.text
        groups = state_tool_groups(state, text) | run.extra_groups
        tools = wire_tool_specs(
            run.tool_calls,
            mode=run.tool_schema_mode,
            include_groups=groups,
            load_tools=True,
        )
        ooc_tools = (
            [spec for spec in tools if spec["function"]["name"] != OOC_TOOL]
            if tools is not None
            else None
        )
        if turn.queue:
            request = turn.queue[0]
            yield Status(_status_for(request))
            if request.name == LOAD_TOOL:
                group = _loaded_group(request)
                if group in TOOL_GROUPS:
                    run.extra_groups.add(group)
                    result = ToolRun(f"Loaded the {group} tools.", None)
                else:
                    result = ToolRun(
                        json.dumps(
                            {
                                "error": "unknown tool group",
                                "groups": sorted(TOOL_GROUPS),
                            }
                        ),
                        None,
                    )
            else:
                result = await run_tool(run.tool_calls, request, ctx=run.ctx)
            if isinstance(result, Ask):
                _record(
                    run.engine,
                    run.campaign_id,
                    _prompt_shown(result),
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
            # An opening is not an out-of-character question: the opening instruction is already
            # the user message, so ask for the introduction instead of a GM answer about rules.
            if turn.opening:
                instruction = OPENING_REPLY_INSTRUCTION
            else:
                instruction = OOC_INSTRUCTION
            view.append({"role": "system", "content": instruction})
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
                event_id = _record(
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
                _after_reply(run, event_id)
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
            _after_reply(run, event_id)
            yield Saved(event_id)
            return
        yield Status("Managing state...")
        round_no = turn.rounds + 1
        view = [*turn.messages, state_message]
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
    # The journal is updated every turn, in the same model call as the first review. Only the
    # campaign opening writes it before there is a narration to review.
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
        async for event in _review_narration(
            run, turn, reply, reasoning, reviewed, journal=review_round == 0
        ):
            yield event
        if not reviewed[0].undone:
            break
    event_id = _finish_reply(run, turn, reply, reasoning)
    yield Saved(event_id)


async def _carry_on(run: _Run) -> AsyncIterator[TurnEvent]:
    turn = _turn(_state(run.engine, run.campaign_id))
    if not turn.begun:
        async for event in _begin(run, turn):
            yield event
    async for event in _drive(run):
        yield event


async def _answer(
    run: _Run, answer: RollAnswer | TextAnswer | ChoiceAnswer | None
) -> AsyncIterator[TurnEvent]:
    """Give the waiting tool the player's answer, record its result, then carry the reply on. A
    choice runs the move band's chosen effects instead of the tool again, since the roll already
    happened. A tool that answers with a band's choices pauses here for the player to pick one."""
    if isinstance(answer, ChoiceAnswer):
        turn = _turn(_state(run.engine, run.campaign_id))
        request = turn.queue[0]
        prompt = turn.prompt
        if prompt is None or not prompt.choices:
            raise InvalidInput("there is no choice to make")
        result = run.ctx.world.choose_move_option(prompt.move, prompt.band, answer.choice)
        _record(
            run.engine,
            run.campaign_id,
            _tool_ran(turn.rounds, request, ToolRun(json.dumps(result), None)),
        )
        for entry in _flush(run):
            yield entry
    elif answer is not None:
        turn = _turn(_state(run.engine, run.campaign_id))
        request = turn.queue[0]
        result = await run_tool(run.tool_calls, request, answer, ctx=run.ctx)
        if isinstance(result, Ask) and result.choices:
            # The roll reached a band the player must choose within. The tool result is recorded
            # when they pick, so the queue and the prompt stay until then.
            _record(run.engine, run.campaign_id, _prompt_shown(result))
            for entry in _flush(run):
                yield entry
            yield Prompt(result.text, result.dice, result.details)
            return
        assert isinstance(result, ToolRun)
        _record(run.engine, run.campaign_id, _tool_ran(turn.rounds, request, result))
        for entry in _flush(run):
            yield entry
        if isinstance(answer, TextAnswer):
            speaker = speaker_name(_state(run.engine, run.campaign_id), run.player_id)
            _record(
                run.engine,
                run.campaign_id,
                ChatPlayer(
                    content=answer.text,
                    speaker=speaker,
                    player_id=run.player_id,
                ),
            )
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


def _character_to_play(state: CampaignState, user_id: int) -> Character:
    """The character this account plays, checked for playability. Raises InvalidInput with the
    reason when the player cannot act, so the page and the turn endpoint give the same answer."""
    player = state.player_character(user_id)
    if player is None:
        raise InvalidInput("create your character before playing")
    if player.out_of_play:
        reason = "is dead" if player.dead else "has left the story"
        raise InvalidInput(f"your character {reason}: choose a new character or a hireling first")
    return player


def _check_encounter_turn(state: CampaignState, user_id: int) -> None:
    """In an encounter a player acts only on their own character's turn. While an NPC's turn is
    running the GM plays it; while another player's character is acting, that player acts."""
    encounter = state.encounter
    if encounter is None or encounter.acting is None:
        return
    acting = state.characters.get(name_key(encounter.acting))
    if acting is None:
        return
    if acting.kind == PC_KIND:
        if not state.owned_by(acting, user_id):
            raise InvalidInput(f"it is {acting.name}'s turn")
        return
    raise InvalidInput(f"{acting.name} is taking their turn; the GM plays it")


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
        player = _character_to_play(current, owner_id)
        _check_encounter_turn(current, owner_id)
        if advancement_due(current, rule_set, player):
            raise InvalidInput("level up or choose your Advancement before sending another message")
        run = _start_run(
            engine,
            providers,
            library,
            rule_set,
            campaign,
            context_tokens,
            rng,
        )
        run.credits.require()
        speaker = speaker_name(current, owner_id)
        _record(
            engine,
            campaign.id,
            ChatPlayer(
                content=text,
                speaker=speaker,
                player_id=owner_id,
            ),
        )
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

    `action` is "roll" to roll the prompt's dice, "choice" to pick one of the move band choices
    the prompt offers, "text" to answer with free-form `text`, or "continue" to retry a reply
    that stopped on an error or was interrupted. The answer is recorded before the reply
    continues, so a failure later does not roll again."""
    campaign = _campaigns().get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _campaigns()._exclusive(campaign.id):
        turn = _state(engine, campaign.id).turn
        if turn is None:
            raise InvalidInput("there is no reply waiting for an answer")
        if turn.player_id is not None and turn.player_id != owner_id:
            who = turn.player_name or "another player"
            raise InvalidInput(f"{who} must answer this reply")
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        answer: RollAnswer | TextAnswer | ChoiceAnswer | None = None
        if action == "continue":
            if turn.prompt is not None:
                raise InvalidInput("answer the GM's prompt first")
        elif turn.prompt is None:
            raise InvalidInput("there is nothing to answer")
        elif action == "roll":
            if turn.prompt.choices:
                raise InvalidInput("choose one of the move's options before rolling again")
            answer = RollAnswer(roll_total(turn.prompt.dice, rng, plus=rule_set.plus))
        elif action == "choice":
            if not turn.prompt.choices:
                raise InvalidInput("there is no choice to make")
            clean = (text or "").strip()
            offered = {
                _choice_fields(choice)["id"]: _choice_fields(choice)["label"]
                for choice in turn.prompt.choices
            }
            if not clean:
                raise InvalidInput("choose one of: " + ", ".join(offered))
            if clean not in offered:
                raise InvalidInput(
                    f"there is no choice named {clean!r}; choose one of " + ", ".join(offered)
                )
            answer = ChoiceAnswer(clean)
        elif action == "text":
            clean = (text or "").strip()
            if not clean:
                raise InvalidInput("the reply cannot be empty")
            if len(clean) > MAX_MESSAGE:
                raise InvalidInput(f"the reply must be at most {MAX_MESSAGE} characters")
            answer = TextAnswer(clean)
        else:
            raise InvalidInput("that action is not available")
        run = _start_run(
            engine,
            providers,
            library,
            rule_set,
            campaign,
            context_tokens,
            rng,
            player_id=owner_id,
        )
        # An opening has not made any model calls yet, so it is a new reply and needs credit. A
        # turn that already started is allowed to finish even if the balance has since fallen.
        if not turn.begun:
            run.credits.require()
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
    streamed like a reply. The engine ends the NPC's turn when the reply finishes, and the next
    player character's turn begins on its own."""
    campaign = _campaigns().get_campaign(engine, owner_id, campaign_id)
    if campaign.model not in allowed_models:
        raise InvalidInput("that model is not available")
    with _campaigns()._exclusive(campaign.id):
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        current = _state(engine, campaign.id)
        if advancement_due(current, rule_set, current.player_character(owner_id)):
            raise InvalidInput("level up or choose your Advancement before starting a turn")
        run = _start_run(engine, providers, library, rule_set, campaign, context_tokens, rng)
        run.credits.require()
        with session_scope(engine) as db:
            character = encounters.begin_turn(db, campaign.id, name)
        if character.kind == PC_KIND:
            raise InvalidInput(
                f"{character.name} is a player character; their turn begins on its own"
            )
        async for event in _guarded(run, _carry_on(run)):
            yield event


# BACKWARDS-COMPATIBILITY: 3 - old pages may still post to the explicit begin route.
def begin_encounter_turn(engine: Engine, owner_id: int, campaign_id: int, name: str) -> None:
    """Start a player character's turn explicitly. The turn normally begins on its own when it
    comes up; this remains for an older page and for a direct retry. The player then acts in chat
    and presses End your turn. Refused while the GM's reply is still open."""
    _campaigns().get_campaign(engine, owner_id, campaign_id)
    with _campaigns()._exclusive(campaign_id):
        state = _state(engine, campaign_id)
        if state.turn is not None:
            raise InvalidInput("the GM's reply is unfinished; finish it before starting a turn")
        with session_scope(engine) as db:
            character = encounters.begin_turn(db, campaign_id, name)
        if character.kind != PC_KIND:
            raise InvalidInput(
                f"{character.name} is not a player character; start their turn from the page"
            )
        if not state.owned_by(character, owner_id):
            raise InvalidInput(f"{character.name} is another player's character")


def _end_turn_as(
    engine: Engine, owner_id: int, campaign_id: int, name: str, by_player: bool
) -> None:
    """End a character's turn for this account. A player ends their own character's turn; the
    campaign owner may also end anyone's, so a table is never stuck on a disconnected player."""
    campaign = _campaigns().get_campaign(engine, owner_id, campaign_id)
    with _campaigns()._exclusive(campaign_id):
        state = _state(engine, campaign_id)
        if state.turn is not None:
            raise InvalidInput("the GM's reply is unfinished; finish it before ending the turn")
        character = state.characters.get(name_key(name))
        if character is None:
            raise InvalidInput(f"there is no character named {name!r}")
        owner_override = campaign.owner_id == owner_id
        if by_player and not owner_override and not state.owned_by(character, owner_id):
            raise InvalidInput(f"{character.name} is another player's character")
        with session_scope(engine) as db:
            encounters.end_turn(db, campaign_id, name, by_player=by_player)


def end_encounter_turn(engine: Engine, owner_id: int, campaign_id: int, name: str) -> None:
    """End the acting character's turn from the encounter controls. The player ends their own
    character's turn and the GM ends an NPC's. The next player character's turn then begins on its
    own; an NPC's still waits for Start. Refused while the GM's reply is still open."""
    _campaigns().get_campaign(engine, owner_id, campaign_id)
    state = _state(engine, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    _end_turn_as(engine, owner_id, campaign_id, name, by_player=character.kind == PC_KIND)


def end_player_turn(engine: Engine, owner_id: int, campaign_id: int, name: str) -> None:
    """End a player character's turn. Refused while the GM's reply is still open."""
    _end_turn_as(engine, owner_id, campaign_id, name, by_player=True)


@dataclass(frozen=True)
class PendingView:
    """An open reply that needs attention: its prompt, or why it stopped. The player's words and
    the rest of the turn are already in the transcript and the Log. `auto_continue` is set for the
    campaign's opening before it has failed, so the page can start it without the player."""

    prompt: Ask | None
    error: str | None = None
    opening: bool = False
    auto_continue: bool = False
    # The player the open reply belongs to, or None when any player may answer it.
    player_id: int | None = None
    player_name: str = ""


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
        return PendingView(
            prompt=turn.prompt,
            opening=turn.opening,
            player_id=turn.player_id,
            player_name=turn.player_name,
        )
    auto = turn.opening and turn.failed is None
    return PendingView(
        prompt=None,
        error=turn.failed or "The GM's reply did not finish.",
        opening=turn.opening,
        auto_continue=auto,
        player_id=turn.player_id,
        player_name=turn.player_name,
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
    including any open reply. Only the owner may rewrite the shared log."""
    campaign = _campaigns().get_campaign(engine, owner_id, campaign_id)
    if campaign.owner_id != owner_id:
        raise InvalidInput("only the campaign owner can roll back the story")
    with _campaigns()._exclusive(campaign_id), session_scope(engine) as db:
        event = db.get(Event, event_id)
        if event is None or event.campaign_id != campaign_id or event.kind != ChatGM.kind:
            raise NotFound("message not found")
        removed = (
            db.exec(
                select(Event.id).where(
                    Event.campaign_id == campaign_id,
                    Event.id > event_id,
                    Event.kind == ChatGM.kind,
                )
            )
            .scalars()
            .all()
        )
        refund_replies(db, removed)
        truncate(db, campaign_id, event_id)
        db.commit()


def discard_pending(engine: Engine, owner_id: int, campaign_id: int) -> None:
    """Remove the open reply and the player's message that started it, so the player can send
    the message again. Used when a reply cannot be continued. The opening cannot be discarded: it
    is recorded with the player character, and it is continued instead."""
    campaign = _campaigns().get_campaign(engine, owner_id, campaign_id)
    with _campaigns()._exclusive(campaign_id), session_scope(engine) as db:
        turn = load_state(db, campaign_id).turn
        if turn is None:
            raise NotFound("there is no open reply to discard")
        if turn.opening:
            raise InvalidInput("the campaign's opening cannot be discarded; continue it instead")
        if (
            turn.player_id is not None
            and turn.player_id != owner_id
            and campaign.owner_id != owner_id
        ):
            raise InvalidInput("only the player whose reply it is can discard it")
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
