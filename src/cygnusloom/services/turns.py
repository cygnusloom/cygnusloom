from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, select

from cygnusloom.content.library import (
    MAP_AMBIENTS,
    MAP_PALETTES,
    TERRAIN_PATTERNS,
    ContentLibrary,
    ModuleTemplate,
    RuleSet,
    Species,
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
    with_options,
)
from cygnusloom.llm.registry import ProviderFactory
from cygnusloom.rules.context import ToolContext, brief
from cygnusloom.rules.dice import roll_values
from cygnusloom.services import encounters
from cygnusloom.services import modules as modules_service
from cygnusloom.services.common import Conflict, InvalidInput, NotFound, session_scope
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
    ChatGMEdited,
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
    SpeechQueued,
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
from cygnusloom.services.level_up import advancement_due_for_player
from cygnusloom.services.prompts import (
    NARRATION_REVISION,
    OOC_INSTRUCTION,
    OPENING_REPLY_INSTRUCTION,
    TURN_REVIEW_INSTRUCTION,
    journal_instruction,
    narrate_instruction,
    opening_prompt,
    speech_instruction,
    state_instruction,
    system_parts,
    turn_checklist,
)
from cygnusloom.services.tools import (
    FINISH_STATE_STEP,
    LOAD_TOOL,
    TOOL_GROUPS,
    Ask,
    ChoiceAnswer,
    RollAnswer,
    TextAnswer,
    ToolRun,
    checklist_tool_spec,
    run_tool,
    state_tool_groups,
    tool_group_names,
    wire_tool_specs,
)
from cygnusloom.services.views import present_rules_text, world_state_text
from cygnusloom.services.world_support import (
    MAX_JOURNAL_DAY_WORDS,
    journal_day_word_counts,
)

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


# An opening reply is the campaign's introduction. A reply that stops after a sentence or two
# leaves the player with no setting, no character, and nothing to do, so it is asked for again.
OPENING_REPLY_ATTEMPTS = 3
OPENING_MIN_SENTENCES = 4
OPENING_REPLY_RETRY = (
    "Opening reply. That was not the whole introduction. The opening instruction asks for one "
    "sentence for each of these, in this order, and nothing else: introducing yourself as "
    "Cygnus, the game master; each important detail the player must know before they act; each "
    "player character; setting the scene; and prompting the player to begin acting. Bold any "
    "keywords or core concepts, use line breaks to separate the sentences, and write the whole "
    "introduction now."
)
_SENTENCE_END = re.compile(r"[.!?]+")


def _opening_reply_is_complete(text: str) -> bool:
    """Whether an opening reply carries the whole introduction. The instruction asks for an
    introduction sentence, the setting, the player characters, the scene, and a prompt to act,
    so a reply with only a sentence or two has not written the opening."""
    return len(_SENTENCE_END.findall(text)) >= OPENING_MIN_SENTENCES


# An empty speech reply is retried this many times before the step gives up.
SPEECH_ATTEMPTS = 3
SPEECH_RETRY = (
    "Your last reply was empty. Write the line now: only the words the speaker says aloud, "
    "one line, and nothing else."
)
# The speech step needs the immediate exchange, not the whole campaign: the summary, the
# profile, and the intent carry the rest. The full transcript made the call slow and let an
# earlier speaker's line bleed into the new one.
SPEECH_CONTEXT_MESSAGES = 6
# The state step keeps the latest rounds' tool results full and turns older ones into short
# summaries, with a compact record log for the detail it no longer needs. The narration and
# journal read the log, not the raw tool results; the review keeps the record but trims old
# history.
STATE_FULL_ROUNDS = 2
STATE_TOOL_RESULT_CHARS = 400
# Lookups are facts the state step may need again. Compacting them to a few hundred characters
# makes the model look the same thing up again in a later round, which costs far more than the
# extra tokens here. Action results keep the smaller budget above.
LOOKUP_RESULT_CHARS = 2000
STATE_HISTORY_MESSAGES = 4
STATE_LOG_LINES = 30
# Only the newest tool round's thinking is useful. Earlier reasoning is a draft of work
# already done and costs thousands of tokens on every later round.
STATE_REASONING_ROUNDS = 1
NARRATION_HISTORY_MESSAGES = 6
NARRATION_REASONING_ROUNDS = 1
REVIEW_HISTORY_MESSAGES = 4
REVIEW_FULL_ROUNDS = 2
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
HEARTBEAT_SECONDS = 4.0
HEARTBEAT_MESSAGE = "Still working..."
JOURNAL_ROUNDS = 3
# A state-step reply that leaves out part of the turn checklist is asked for again this many
# times. The turn is never blocked on it: after the retries the story goes on.
CHECKLIST_RETRIES = 2
# Once the prompt reaches this share of the context window, older history is summarized.
COMPRESS_AT = 0.7
# After a summary, the newest messages worth up to this share of the window stay verbatim.
KEEP_RECENT = 0.15
# A summary may take up to this share of the window. Longer ones keep only their newest text.
SUMMARY_SHARE = 0.1
# After a summary, the prompt is kept this far below the compress point. The room is what the
# next turns grow into before another summary, so one summary serves several turns.
SUMMARY_HEADROOM = 0.15
# A summary stays at least this long even when the fixed prompt leaves little room, since a
# memory with no room for the summary would forget the campaign entirely.
MIN_SUMMARY_SHARE = 0.02
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


def _checklist_retry(missing: Sequence[str]) -> str:
    return (
        "The state step is not finished: the turn checklist is missing "
        + ", ".join(missing)
        + ". Call finish_state_step now with one short note for every area in the system prompt, "
        'and use "no change" where nothing changed. Make any change you missed with the right '
        "tool before you call it."
    )


def _checklist_active(turn: Turn) -> bool:
    """Whether this turn was begun with the turn checklist in its system prompt. A turn begun
    before the checklist existed keeps playing without one."""
    return any(part.source == "Turn checklist" for part in turn.context)


def _checklist_for(run: _Run, turn: Turn) -> tuple[str, tuple[str, ...]]:
    """The turn's checklist text and keys. An NPC's encounter turn adds the areas that remind
    the GM to consider its attack, movement, and other turn resources."""
    return turn_checklist(
        run.rule_set.checklist,
        tool_group_names(run.rule_set.tool_calls),
        npc_turn=turn.actor is not None,
    )


def _checklist_items(run: _Run, turn: Turn) -> dict[str, str]:
    """The checklist areas this turn requires, in the rule set's order, mapping each key to the
    area's description for the finish tool."""
    rules = run.rule_set.checklist
    if rules is None:
        return {}
    required = set(_checklist_for(run, turn)[1])
    return {item.id: item.text for item in rules.items if item.id in required}


def _read_checklist_report(
    request: ToolRequest, keys: Sequence[str]
) -> tuple[dict[str, str], list[str]]:
    """The finish tool's notes by area, and the required areas with no note. A malformed request
    leaves every area missing, so the engine asks for the whole checklist again."""
    try:
        payload = json.loads(request.arguments or "{}")
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, Mapping):
        return {}, list(keys)
    # A model may wrap the notes one level down; accept either shape.
    nested = payload.get("checks")
    if isinstance(nested, Mapping):
        payload = nested
    report: dict[str, str] = {}
    missing: list[str] = []
    for key in keys:
        value = payload.get(key)
        note = value.strip() if isinstance(value, str) else ""
        if note:
            report[key] = note
        else:
            missing.append(key)
    return report, missing


def _format_checklist_report(report: Mapping[str, str]) -> str:
    return "\n".join(f"- {key}: {note}" for key, note in report.items())


def _is_checklist_retry_message(message: Mapping[str, Any]) -> bool:
    return message.get("role") == "system" and str(message.get("content", "")).startswith(
        "The state step is not finished: the turn checklist is missing"
    )


def _without_checklist_exchange(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The turn's messages without the finish tool's call and result. Its arguments are the
    state step's internal notes; the narration and the review of it never read them."""
    ids = {
        call.get("id")
        for message in messages
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or ())
        if (call.get("function") or {}).get("name") == FINISH_STATE_STEP
    }
    if not ids:
        return list(messages)
    kept: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") in ids:
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            calls = [
                call
                for call in message["tool_calls"]
                if (call.get("function") or {}).get("name") != FINISH_STATE_STEP
            ]
            if not calls:
                # The message held only the finish call, so any prose in it is state-step
                # draft as well. The narration and review read neither.
                continue
            message = {**message, "tool_calls": calls}
        kept.append(message)
    return kept


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


def _tool_call_message(step: ChatReply, *, keep_reasoning: bool = True) -> dict[str, Any]:
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
    # A state round's thinking is a draft. Keeping it would rewrite the prefix every round
    # and lose the provider's prompt-cache hits; the calls and results carry the decisions.
    message["reasoning_content"] = step.reasoning if keep_reasoning else ""
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
    if request.name == FINISH_STATE_STEP:
        return "Finishing the state step..."
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
        optional=list(ask.optional),
        reaction=dict(ask.reaction or {}),
        move=ask.move,
        band=ask.band,
        roll=ask.roll,
        total=ask.total,
        character=ask.character,
        target=ask.target,
        skill=ask.skill,
        weapon=ask.weapon,
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
    campaign: Campaign
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
    # Whether the campaign tracks food, water, and rest privation. Off means rests spend
    # nothing and hunger and thirst are not tracked.
    tracks_survival_needs: bool = True
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
    # NPC lines being written beside the state step. Set once the run exists, because the
    # tool context needs to queue through it.
    speech: _SpeechJobs = field(init=False, repr=False)
    # The same model at lower reasoning effort for the journal's bounded edit and for state
    # rounds after the first, where the plan is already set. Speech, narration, review, and
    # the planning round keep full effort.
    provider_low: Provider = field(init=False, repr=False)

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
    modules: Mapping[str, ModuleTemplate] | None = None,
    speak_handler: Callable[[str, str, str, bool], Awaitable[None]] | None = None,
    species: Sequence[Species] = (),
) -> ToolContext:
    async def speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        if speak_handler is not None:
            await speak_handler(speaker, profile, intent, replace)
            return
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
        items={**world.items},
        species=species,
        checks=rule_set.checks,
        conditions=rule_set.conditions,
        projects=rule_set.projects,
        faction_assets=rule_set.faction_assets,
        campaign_clocks=rule_set.campaign_clocks,
        threats=rule_set.threats,
        vehicles=rule_set.vehicles,
        recipes=rule_set.recipes,
        improvements=rule_set.improvements,
        modules=modules or {},
        table_genres=genres,
        table_tones=rule_set.table_tones,
        moves=rule_set.moves,
        counters=rule_set.counters,
        advancements=rule_set.advancements,
        level_ups=rule_set.level_ups,
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
    schema = library.setting_template(campaign.world_template)
    world = library.world_content_for(schema, campaign.rule_set)
    speech: _SpeechJobs | None = None

    async def queue_speech(speaker: str, profile: str, intent: str, replace: bool) -> None:
        assert speech is not None, "the speech queue is ready before any tool runs"
        await speech.queue(speaker, profile, intent, replace)

    run = _Run(
        engine=engine,
        campaign_id=campaign.id or 0,
        campaign=campaign,
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
            modules=library.modules,
            speak_handler=queue_speech,
            species=schema.species or rule_set.species,
        ),
        cursor=_newest(engine, campaign.id),
        opening=opening_prompt(schema),
        unattached_rules=world.unattached_rules,
        world=schema,
        setting=campaign.setting,
        tracks_inventory=campaign.use_inventory_space,
        tracks_survival_needs=campaign.use_survival_needs,
        player_id=player_id,
        tool_schema_mode=getattr(settings, "tool_schema_mode", "brief"),
        rule_info_mode=getattr(settings, "rule_info_mode", "brief"),
    )
    speech = _SpeechJobs(run)
    run.speech = speech
    # Low effort is for the journal's bounded edit and for state rounds after the first,
    # where the plan is already set; it cuts the deliberation out of those calls. Speech,
    # narration, review, and the planning round keep normal effort, where lower effort cost
    # fidelity.
    run.provider_low = MeteredProvider(with_options(provider, reasoning_effort="low"), meter)
    return run


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
        track_survival_needs=run.tracks_survival_needs,
        currency=rule_set.marketplace.currency if rule_set.marketplace else "",
        housekeeping=rule_set.housekeeping,
        character=rule_set.character,
        monsters=rule_set.monsters,
        species=run.ctx.world.species,
    )


def _narration_messages(
    run: _Run,
    turn: Turn,
    log: list[str] | None = None,
    heading: str = "Turn log, everything in this turn so far, in order:",
    *,
    history_limit: int | None = None,
    tool_exchange: str = "full",
) -> list[dict[str, Any]]:
    """What the narration step reads. The turn's opening system message predates the moves made
    in the turn, so the scene is rebuilt from the state as it is now, which also brings in the
    rules for anything that came into the scene. The turn log follows the conversation, so the
    narration covers each entry. A state step's prose is a draft written before the tools ran, so
    the narration sees the tool calls but not that prose. The turn checklist and its finish tool
    are state-step bookkeeping, so they are left out too. `log` replaces the turn's own Log lines.
    The narration instruction is always last, after the log and any review note, even when a
    review step added its own tool exchange to the turn.

    `history_limit` keeps only the last few pre-turn messages. `tool_exchange` is "full",
    "compact" (summarize old tool results), or "none" (replace the raw results with a
    placeholder). The narration keeps recent results because it must narrate their outcomes;
    the journal reads the log instead."""
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
    conversation = _without_checklist_exchange(turn.messages[1:])
    if history_limit is not None:
        conversation = _trim_conversation(conversation, history_limit)
    if tool_exchange == "none":
        conversation = _without_tool_results(conversation)
    elif tool_exchange == "compact":
        conversation, _ = _compact_old_tool_results(
            conversation,
            REVIEW_FULL_ROUNDS,
            preserve_names=_lookup_tool_names(run),
            preserve_chars=LOOKUP_RESULT_CHARS,
        )
    elif tool_exchange != "full":
        raise ValueError(f"unknown tool exchange mode {tool_exchange!r}")
    conversation = _without_old_reasoning(conversation, NARRATION_REASONING_ROUNDS)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(p.content for p in scene)},
        *(
            _without_draft(m)
            for m in conversation
            if not (
                (m.get("role") == "system" and m.get("content") == instruction)
                or _is_checklist_retry_message(m)
            )
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


def _tool_name_map(messages: Sequence[dict[str, Any]]) -> dict[str, str]:
    """The tool name for each call id in a message list."""
    names: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                names[call.get("id", "")] = call.get("function", {}).get("name", "tool")
    return names


def _lookup_tool_names(run: _Run) -> frozenset[str]:
    """The tools whose results are facts worth keeping for the whole turn."""
    return frozenset(name for name, tool in run.tool_calls.items() if tool.lookup)


def _short_tool_text(name: str, text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= STATE_TOOL_RESULT_CHARS:
        return f"{name}: {flat}"
    return f"{name}: {flat[:STATE_TOOL_RESULT_CHARS]}..."


def _compact_tool_content(name: str, content: str) -> str:
    """A tool result made short enough for an old state round. Errors stay word for word;
    a structured result keeps its useful fields; anything else is clipped."""
    text = content or ""
    if not text:
        return text
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return _short_tool_text(name, text)
    if isinstance(data, dict) and data.get("error"):
        return text
    if isinstance(data, dict):
        wanted = (
            "created",
            "added",
            "placed",
            "moved",
            "removed",
            "updated",
            "deleted",
            "changed",
            "map",
            "name",
            "location",
            "column",
            "row",
            "cost",
            "moves_left",
            "hp",
            "armor",
            "attacks",
            "status",
            "warning",
            "summary",
            "armed",
        )
        parts: list[str] = []
        for key in wanted:
            value = data.get(key)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, dict | list):
                value = json.dumps(value, separators=(",", ":"))
            parts.append(f"{key}={str(value)[:80]}")
        if parts:
            return f"{name}: " + "; ".join(parts)
    return _short_tool_text(name, text)


def _preserve_tool_content(name: str, content: str, limit: int) -> str:
    """A lookup result kept for the turn, with a hard limit for an unusually large answer."""
    text = content or ""
    if len(text) <= limit:
        return text
    return f"{name}: {text[:limit]}... [truncated]"


def _compact_old_tool_results(
    messages: Sequence[dict[str, Any]],
    keep_rounds: int,
    *,
    preserve_names: frozenset[str] = frozenset(),
    preserve_chars: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Keep the latest `keep_rounds` tool-result rounds full and summarize older results.

    A result from a lookup tool is a fact the state step may need again, so it stays readable
    for the whole turn (up to `preserve_chars`) instead of becoming a short action summary."""
    names = _tool_name_map(messages)
    starts = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    if len(starts) <= keep_rounds:
        return list(messages), False
    first_full = starts[-keep_rounds] if keep_rounds else len(messages)
    changed = False
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index < first_full and message.get("role") == "tool":
            name = names.get(message.get("tool_call_id", ""), "tool")
            content = str(message.get("content") or "")
            if name in preserve_names and preserve_chars > 0:
                compact = _preserve_tool_content(name, content, preserve_chars)
            else:
                compact = _compact_tool_content(name, content)
            if compact != content:
                result.append({**message, "content": compact})
                changed = True
                continue
        result.append(message)
    return result, changed


def _without_tool_results(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace raw results with a short placeholder. The assistant tool calls and their
    reasoning stay, and every call still has the result message providers require."""
    return [
        {**message, "content": "[result omitted]"} if message.get("role") == "tool" else message
        for message in messages
    ]


def _without_old_reasoning(
    messages: Sequence[dict[str, Any]], keep_rounds: int
) -> list[dict[str, Any]]:
    """Blank the thinking on tool-call rounds older than the newest `keep_rounds`. The key
    stays because thinking models reject an assistant message without it; the calls and
    results carry what the earlier rounds decided."""
    starts = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    if len(starts) <= keep_rounds:
        return list(messages)
    first_full = starts[-keep_rounds] if keep_rounds else len(messages)
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if (
            index < first_full
            and message.get("role") == "assistant"
            and message.get("tool_calls")
            and message.get("reasoning_content")
        ):
            result.append({**message, "reasoning_content": ""})
        else:
            result.append(message)
    return result


def _trim_conversation(messages: Sequence[dict[str, Any]], keep: int) -> list[dict[str, Any]]:
    """Keep the leading system message, the last `keep` history messages, and the current
    turn from the last player message onward."""
    head = [messages[0]] if messages and messages[0].get("role") == "system" else []
    body = list(messages[len(head) :])
    last_user = next(
        (index for index in range(len(body) - 1, -1, -1) if body[index].get("role") == "user"),
        None,
    )
    if last_user is None:
        return list(messages)
    history = body[:last_user]
    if not keep:
        history = []
    elif len(history) > keep:
        start = len(history) - keep
        # A kept tool result needs the assistant call that asked for it.
        while start > 0 and history[start].get("role") == "tool":
            start -= 1
        history = history[start:]
    return [*head, *history, *body[last_user:]]


def _state_messages(run: _Run, turn: Turn, state_message: dict[str, Any]) -> list[dict[str, Any]]:
    """The state step's messages, with only the last few history messages, old tool results
    compacted, and a short record log when a result was compacted."""
    messages = _trim_conversation([*turn.messages], STATE_HISTORY_MESSAGES)
    messages, compacted = _compact_old_tool_results(
        messages,
        STATE_FULL_ROUNDS,
        preserve_names=_lookup_tool_names(run),
        preserve_chars=LOOKUP_RESULT_CHARS,
    )
    messages = _without_old_reasoning(messages, STATE_REASONING_ROUNDS)
    if compacted:
        log = _turn_log(run.engine, run.campaign_id, turn.opener)
        if log:
            lines = "\n".join(f"- {line}" for line in log[-STATE_LOG_LINES:])
            messages.append(
                {
                    "role": "system",
                    "content": "State so far (record changes; older tool results are "
                    "summarized):\n" + lines,
                }
            )
    messages.extend(
        {"role": "system", "content": part.content}
        for part in turn.context
        if part.source == "Checklist retry" and part.role == "system"
    )
    messages.append(state_message)
    if turn.speech:
        messages.append(_queued_dialogue_note(turn.speech))
    return messages


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


def _speech_messages(
    state: CampaignState,
    turn: Turn,
    speaker: str,
    profile: str,
    intent: str,
    replace: bool,
    sentence_cap: int | None,
    previous: str = "",
) -> tuple[str, str, list[dict[str, Any]]]:
    """The speech step's system prompt, its label, and its messages. It gets no rules or tool
    instructions: it only writes words, and those instructions get copied into dialogue as
    tool-call markup."""
    queued = next(
        (line for line in turn.speech if line["speaker"].casefold() == speaker.casefold()), None
    )
    parts = [speech_instruction(sentence_cap)]
    if state.summary:
        parts.append("Summary of earlier events in this campaign:\n" + state.summary)
    parts.append(profile)
    if replace:
        amended = previous or (queued["dialogue"] if queued else "")
        if amended:
            parts.append(
                f"The line already queued for {speaker}, which this intent replaces: {amended}"
            )
    intent_part = f"Intent: {intent}"
    parts.append(intent_part)
    system = "\n\n".join(parts)
    brief_parts = [profile]
    if replace and (queued or previous):
        brief_parts.append(
            "This intent amends an earlier draft for this speaker. Only the queued dialogue "
            "is the speaker's words; the earlier draft is not."
        )
    brief_parts.append(intent_part)
    brief = "\n\n".join(brief_parts)
    conversation = [
        m for m in turn.messages[1:] if m["role"] in ("user", "assistant") and "tool_calls" not in m
    ][-SPEECH_CONTEXT_MESSAGES:]
    label = f"Speech step (round {turn.rounds})"
    brief_label = f"Speech brief (round {turn.rounds}) for {speaker}"
    return system, brief, brief_label, label, [{"role": "system", "content": system}, *conversation]


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
    """Ask the speech step for an NPC's words and record them for the narration.

    This is the direct path used outside a turn run. A turn queues the intent instead, so the
    state step does not wait for the words (see `_SpeechJobs`)."""
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
    system, brief, brief_label, label, speech = _speech_messages(
        state, turn, speaker, profile, intent, replace, sentence_cap
    )
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
                    ContextPart("system", brief_label, brief),
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


@dataclass
class _SpeechJob:
    """One NPC line being written while the state step continues. `fallback` is the line this
    replacement amends, kept until the new line is known to be usable."""

    speaker: str
    replace: bool
    system: str
    brief: str
    brief_label: str
    label: str
    request: list[dict[str, Any]]
    fallback: tuple[str, bool] | None = None
    task: asyncio.Task[tuple[str, bool]] | None = None


class _SpeechJobs:
    """The NPC lines waiting for the speech step in one reply.

    The state step's `speak` call records the intent and returns at once; the speech step runs as
    its own task beside the next state round. Lines are written in parallel, then recorded in the
    order the GM asked for them, before narration. Each speaker still gets its own speech prompt,
    so one NPC's voice cannot bleed into another's.
    """

    def __init__(self, run: _Run) -> None:
        self._run = run
        self._jobs: dict[str, _SpeechJob] = {}
        self._order: list[str] = []

    async def queue(self, speaker: str, profile: str, intent: str, replace: bool) -> None:
        state = _state(self._run.engine, self._run.campaign_id)
        turn = _turn(state)
        key = name_key(speaker)
        queued = next(
            (
                entry
                for entry in turn.speech_queue
                if name_key(str(entry.get("speaker", ""))) == key
            ),
            None,
        ) or next(
            (line for line in turn.speech if line["speaker"].casefold() == speaker.casefold()), None
        )
        if queued and not replace:
            raise InvalidInput(
                f"{speaker} already has dialogue for this reply; put everything they say in one "
                "intent, or pass replace: true to rewrite that line"
            )
        fallback = await self._settle_before_replace(key) if replace else None
        amended = (
            (
                fallback[0]
                if fallback is not None
                else str(queued.get("intent", ""))
                if isinstance(queued, Mapping)
                else ""
            )
            if replace
            else ""
        )
        _record(
            self._run.engine,
            self._run.campaign_id,
            SpeechQueued(speaker=speaker, profile=profile, intent=intent, replace=replace),
        )
        # The record above changes the queued line the new speech prompt describes, so read
        # the turn again before it is built.
        state = _state(self._run.engine, self._run.campaign_id)
        self._start(
            state,
            _turn(state),
            {"speaker": speaker, "profile": profile, "intent": intent, "replace": replace},
            fallback=fallback,
            previous=amended,
        )

    async def _settle_before_replace(self, key: str) -> tuple[str, bool] | None:
        """Wait for the line this replacement amends, so a failed rewrite keeps a line to fall
        back on, as the direct speech step did. The line is not recorded until the replacement
        fails: a recorded line would also reach the narration as a second line."""
        old = self._jobs.get(key)
        if old is None or old.task is None:
            self._cancel(key)
            return None
        try:
            reply, malformed = await old.task
        except asyncio.CancelledError:
            self._cancel(key)
            raise
        except Exception as exc:
            self._cancel(key)
            if isinstance(exc, LLMError):
                raise
            raise LLMError("the speech step failed") from exc
        self._cancel(key)
        return (reply, malformed) if reply and not malformed else None

    def ensure_started(self, state: CampaignState, turn: Turn) -> None:
        """Start a task for every queued line, such as after a resumed reply, and forget one
        whose line was already written or replaced."""
        wanted = {name_key(str(entry.get("speaker", ""))): entry for entry in turn.speech_queue}
        for key in list(self._jobs):
            if key not in wanted:
                self._cancel(key)
        for key, entry in wanted.items():
            if key not in self._jobs:
                self._start(state, turn, entry)

    def _start(
        self,
        state: CampaignState,
        turn: Turn,
        entry: Mapping[str, Any],
        fallback: tuple[str, bool] | None = None,
        previous: str = "",
    ) -> None:
        speaker = str(entry.get("speaker", ""))
        replace = bool(entry.get("replace", False))
        system, brief, brief_label, label, request = _speech_messages(
            state,
            turn,
            speaker,
            str(entry.get("profile", "")),
            str(entry.get("intent", "")),
            replace,
            self._run.rule_set.sentence_cap,
            previous=previous,
        )
        job = _SpeechJob(
            speaker=speaker,
            replace=replace,
            system=system,
            brief=brief,
            brief_label=brief_label,
            label=label,
            request=request,
            fallback=fallback,
        )
        job.task = asyncio.create_task(self._write(job), name=f"speech-{self._run.campaign_id}")
        key = name_key(speaker)
        self._jobs[key] = job
        if key not in self._order:
            self._order.append(key)

    async def _write(self, job: _SpeechJob) -> tuple[str, bool]:
        # Speech keeps the turn model's full effort. With reasoning disabled it once wrote
        # another speaker's earlier line as scene prose, which the review then rewrote twice.
        reply = ""
        for attempt in range(SPEECH_ATTEMPTS):
            request = (
                job.request
                if attempt == 0
                else [*job.request, {"role": "system", "content": SPEECH_RETRY}]
            )
            reply = (await complete(self._run.provider, self._run.model_name, request)).strip()
            if reply:
                break
        malformed = _is_tool_markup(reply, (tool.name for tool in self._run.tool_calls.values()))
        return reply, malformed

    async def collect(self, turn: Turn) -> Turn:
        """Wait for the queued lines, record them in request order, and return the turn with
        them. A provider failure aborts the reply before any line is recorded, so Continue
        retries the queue cleanly."""
        jobs = [self._jobs[key] for key in self._order if key in self._jobs]
        if not jobs:
            return turn
        results = await asyncio.gather(
            *(job.task for job in jobs if job.task is not None), return_exceptions=True
        )
        error = next((result for result in results if isinstance(result, BaseException)), None)
        if error is not None:
            self.cancel()
            if isinstance(error, asyncio.CancelledError):
                raise error
            if isinstance(error, LLMError):
                raise error
            raise LLMError("the speech step failed") from error
        events: list[GameEvent] = []
        for job, result in zip(jobs, results, strict=True):
            assert not isinstance(result, BaseException)
            reply, malformed = result
            if (malformed or not reply) and job.fallback is not None:
                # A failed rewrite keeps the line it was amending.
                reply, malformed = job.fallback
            if malformed or not reply:
                logger.warning("speech step wrote no usable line for %s", job.speaker)
            events.append(self._line_event(job, reply, malformed))
        _record(self._run.engine, self._run.campaign_id, *events)
        self._jobs.clear()
        self._order.clear()
        return _turn(_state(self._run.engine, self._run.campaign_id))

    def _line_event(self, job: _SpeechJob, reply: str, malformed: bool) -> SpeechWritten:
        return SpeechWritten(
            speaker=job.speaker,
            dialogue="" if malformed else reply,
            replace=job.replace,
            context=_dicts(
                [
                    ContextPart("system", job.label, job.system),
                    ContextPart("system", job.brief_label, job.brief),
                    ContextPart("assistant", f"{job.label} reply", reply),
                ]
            ),
        )

    def cancel(self) -> None:
        for key in list(self._jobs):
            self._cancel(key)

    def _cancel(self, key: str) -> None:
        job = self._jobs.pop(key, None)
        if job is not None and job.task is not None and not job.task.done():
            job.task.cancel()
        if key in self._order:
            self._order.remove(key)


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
    modules_text = modules_service.module_state_text(
        getattr(run.ctx.world, "modules", {}),
        run.campaign,
        state,
        default_genres=run.rule_set.table_genres,
        default_tones=run.rule_set.table_tones,
    )
    groups = state_tool_groups(state, message) | run.extra_groups
    checklist, _ = _checklist_for(run, turn)
    tool_parts = _campaigns().tool_definition_parts(
        rule_set,
        run.tool_schema_mode,
        include_groups=groups,
        load_tools=True,
        checklist=_checklist_items(run, turn) or None,
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
            checklist=checklist,
        )
    )
    prompt_tokens += estimate_tokens(state_text)
    if modules_text:
        prompt_tokens += estimate_tokens(modules_text)
    prompt_tokens += sum(part.tokens for part in tool_parts)
    history_tokens = sum(estimate_tokens(m.content) for m in history)
    prompt_tokens += estimate_tokens(message) + history_tokens
    summary_record: list[ContextPart] = []
    events: list[GameEvent] = []
    if prompt_tokens >= run.context_tokens * COMPRESS_AT:
        # Keep the recent tail verbatim, but only as much as leaves the summary and its
        # headroom below the compress point. Without that room, the next turns push the prompt
        # back over the point and summarize again after a turn or two.
        head_tokens = prompt_tokens - history_tokens
        target = int(run.context_tokens * (COMPRESS_AT - SUMMARY_HEADROOM))
        summary_budget = int(run.context_tokens * SUMMARY_SHARE)
        if head_tokens + summary_budget > target:
            summary_budget = max(int(run.context_tokens * MIN_SUMMARY_SHARE), target - head_tokens)
        keep_budget = max(
            0,
            min(
                int(run.context_tokens * KEEP_RECENT),
                target - head_tokens - summary_budget,
            ),
        )
        start = _campaigns()._verbatim_start(history, keep_budget)
        if start > 0:
            folded = history[:start]
            yield Status("Summarizing older messages...")
            summary = await _campaigns()._summarize(
                run.provider,
                run.model_name,
                summary,
                folded,
                summary_budget,
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
        checklist=checklist,
    )
    if modules_text:
        system.append(ContextPart("system", "Modules", modules_text))
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
                run,
                turn,
                log=log,
                heading="Log since the journal was last written, in order:",
                history_limit=0,
                tool_exchange="none",
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


# Context the narration never sees: the speech step's brief (profile and intent). The review is
# shown it so it can judge whether the narration invented dialogue. The full speech prompt stays
# in the turn for debugging, but it can contain a superseded draft that is not the record. The
# state step's prose is a draft written before its tools ran, not part of the record, so neither
# the narration nor the review reads it.
_REVIEW_ONLY_SOURCES = ("Speech brief (round",)


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
    """The parts of the turn's context that the narration is not given. The speech step's brief is
    included. Its reply repeats dialogue the review already has from the queued lines, so only
    the system part is added. When a speaker's line was replaced, only the newest brief is kept;
    an older brief describes a draft that is no longer queued."""
    newest: dict[str, ContextPart] = {}
    for part in turn.context:
        if not part.source.startswith(_REVIEW_ONLY_SOURCES) or part.role != "system":
            continue
        key = part.source.split(" for ", 1)[1].casefold() if " for " in part.source else part.source
        newest[key] = part
    return list(newest.values())


def _review_view(
    run: _Run,
    turn: Turn,
    log: list[str] | None = None,
    heading: str = "Turn log, everything in this turn so far, in order:",
    *,
    history_limit: int | None = REVIEW_HISTORY_MESSAGES,
    tool_exchange: str = "compact",
) -> list[dict[str, Any]]:
    """What the review reads before the narration. It leaves out the narration instruction and
    the narrator's rewrite note, and replaces the latter with a review note naming the fault the
    rewrite was meant to fix. Earlier reviews' prose is left out: the review judges this narration
    against the record, not a review of an earlier version."""
    instruction = narrate_instruction(run.rule_set, run.world)
    view = [
        message
        for message in _narration_messages(
            run,
            turn,
            log=log,
            heading=heading,
            history_limit=history_limit,
            tool_exchange=tool_exchange,
        )
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
    actor = turn.actor
    # The GM may have adjusted whose turn is running during the reply, so end the NPC actually
    # taking it, not only the one the reply opened with.
    with session_scope(run.engine) as db:
        state = load_state(db, run.campaign_id)
        encounter = state.encounter
        if encounter is not None and encounter.acting is not None:
            acting = state.characters.get(name_key(encounter.acting))
            if acting is not None and acting.kind == NPC_KIND:
                actor = acting.name
    if actor is not None:
        _end_npc_encounter_turn(run, actor)
    # A fight the GM set up without calling roll_initiative still gets its side order before the
    # reply closes and a turn can begin.
    run.ctx.world.settle_side_initiative()
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
    messages = _narration_messages(
        run,
        turn,
        history_limit=NARRATION_HISTORY_MESSAGES,
        tool_exchange="compact",
    )
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


async def _journal_step(run: _Run, turn: Turn) -> None:
    """Bring the journal up to date while the narration is being written.

    The journal reads only the record, never the narration, so it can run beside the narration
    instead of in the review's call. A journal step that fails leaves the journal as it was; the
    next turn writes the missing days."""
    tools = wire_tool_specs(run.tool_calls, journal_step=True, mode=run.tool_schema_mode)
    if tools is None:
        return
    # The journal covers the log since the last journal step, which can reach back past this
    # turn after an out-of-character reply or a failed reply. Read that log before this step's
    # JournalStarted marks it covered.
    state = _state(run.engine, run.campaign_id)
    log = _turn_log(run.engine, run.campaign_id, state.journal_through or turn.opener)
    view = _review_view(
        run,
        turn,
        log=log,
        heading="Log since the journal was last written, in order:",
        history_limit=0,
        tool_exchange="none",
    )
    counts = journal_day_word_counts(state.journal)
    budget = "; ".join(
        f"Day {day}: {words}/{MAX_JOURNAL_DAY_WORDS}" for day, words in sorted(counts.items())
    )
    view.append(
        {
            "role": "system",
            "content": journal_instruction(state.clock.day, state.journal)
            + (f"\n\nWords already used by day: {budget}." if budget else ""),
        }
    )
    # One round: the step is written beside the narration, so every edit belongs in the same
    # request. A model that asks for another round would only delay the player's reply.
    step = await run.provider_low.chat(run.model_name, view, tools)
    failed, succeeded, exchange = await _run_journal_calls(run, step)
    if failed and not succeeded:
        # The journal is unchanged, so a correction cannot duplicate an edit that worked. Give
        # the model one chance to see the exact tool error and fix it in the same reply.
        view.extend(exchange)
        errors = "\n".join(failed)
        view.append(
            {
                "role": "system",
                "content": "The journal was not changed because these calls failed:\n"
                + errors
                + "\nFix them once now. Use only the offered journal tools, quote the journal "
                "exactly, and keep one day's passage under the word limit.",
            }
        )
        step = await run.provider_low.chat(run.model_name, view, tools)
        retry_failed, _, _ = await _run_journal_calls(run, step)
        failed = retry_failed
    if failed:
        # Leave the log for the next turn so the journal can try the day again.
        return
    # Mark the covered log only after the step succeeded. A failed or cancelled journal step
    # must leave `journal_through` where it was, so the next turn writes the same days again
    # instead of losing them.
    _record(run.engine, run.campaign_id, JournalStarted())


async def _run_journal_calls(
    run: _Run, step: ChatReply
) -> tuple[list[str], bool, list[dict[str, Any]]]:
    """Run a journal step's requests and build the exchange for a correction round."""
    failed: list[str] = []
    succeeded = False
    exchange: list[dict[str, Any]] = [_tool_call_message(step)] if step.tool_requests else []
    for request in step.tool_requests:
        result = await run_tool(run.tool_calls, request, ctx=run.ctx)
        if isinstance(result, Ask):
            raise LLMError("the journal step asked the player a question")
        if _tool_failed(result):
            failed.append(f"{request.name}: {result.output}")
            logger.warning("journal tool %s failed: %s", request.name, result.output)
        else:
            succeeded = True
        exchange.append({"role": "tool", "tool_call_id": request.id, "content": result.output})
    return failed, succeeded, exchange


_REVIEW_REPAIR_EXCLUDED = frozenset({"speak", "ooc", "set_music"})


def _review_repair_tools(run: _Run) -> list[dict[str, Any]] | None:
    """The record tools the review may use after an undo, to fix the fault it named. Speech and
    the out-of-character step are left out, as they do not repair a record."""
    tools = wire_tool_specs(run.tool_calls, mode=run.tool_schema_mode)
    if tools is None:
        return None
    return [spec for spec in tools if spec["function"]["name"] not in _REVIEW_REPAIR_EXCLUDED]


async def _review_narration(
    run: _Run,
    turn: Turn,
    narration: str,
    reasoning: str,
    result: list[_ReviewResult],
) -> AsyncIterator[TurnEvent]:
    """Check the narration just written against the record. The review passes with no tools,
    or sets the narration aside and then uses the record tools to fix the fault it named. A
    passing narration is never sent back for a record tweak. The caller writes the story again
    when `result` says the narration was undone."""
    tools = wire_tool_specs(run.tool_calls, review_step=True, mode=run.tool_schema_mode)
    if tools is None:
        result.append(_ReviewResult(undone=False))
        return
    allowed = {spec["function"]["name"] for spec in tools}
    read_only = {tool.name for tool in run.tool_calls.values() if tool.lookup or tool.review_step}
    yield Status("Reviewing the turn...")
    view = _review_view(run, turn)
    view.append(_text_message(narration, reasoning))
    hidden = _review_only_context(turn)
    if hidden:
        lines = "\n\n".join(f"{part.source}:\n{part.content}" for part in hidden)
        view.append(
            {
                "role": "system",
                "content": "The speech step's briefs: what each queued line was asked to "
                "convey, not the wording. The queued dialogue above is the only wording the "
                "narration may give an NPC.\n\n" + lines,
            }
        )
    view.append({"role": "system", "content": TURN_REVIEW_INSTRUCTION})
    undone = False
    repairing = False
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
                            "error": "that tool is not available yet; call undo_narration "
                            "if the record is wrong, or pass"
                        }
                    ),
                    None,
                )
            elif request.name not in read_only and not undone:
                # A record-changing review tool would leave the player reading a story written
                # from the old state. Refuse it instead of guessing.
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
                    json.dumps(
                        {
                            "error": "the narration is already set aside; fix the record "
                            "fault or stop"
                        }
                    ),
                    None,
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
        if undone and not repairing:
            # The undo opens the record tools. A second round lets the review fix the fault it
            # named, then the story is written again from the corrected record.
            repair = _review_repair_tools(run)
            if not repair:
                break
            repairing = True
            tools = repair
            allowed = {spec["function"]["name"] for spec in tools}
            read_only = {
                tool.name for tool in run.tool_calls.values() if tool.lookup or tool.review_step
            }
            view.append(
                {
                    "role": "system",
                    "content": "The narration is set aside. Use the offered tools only to "
                    "correct the record fault you named, then stop.",
                }
            )
            continue
        if undone:
            break
    result.append(_ReviewResult(undone=undone))


def _record_state_finished(
    run: _Run,
    turn: Turn,
    messages: list[dict[str, Any]],
    context: list[ContextPart],
) -> None:
    """Record the state step's last message, with the dialogue it queued and the narration
    instruction the narration step reads last."""
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


async def _finish_checklist(
    run: _Run, turn: Turn, request: ToolRequest
) -> AsyncIterator[TurnEvent]:
    """Accept the state step's checklist report or ask for the missing areas again. The report
    arrives as a tool call so its notes stay out of the text and out of the narration."""
    # The state step that requested the finish call is the latest round already recorded.
    round_no = turn.rounds
    if turn.ooc:
        # The checklist does not apply to an out-of-character reply. Answer the call so the
        # conversation stays valid, then let the out-of-character step run.
        _record(
            run.engine,
            run.campaign_id,
            StateStepMade(
                requests=[],
                messages=[
                    {
                        "role": "tool",
                        "tool_call_id": request.id,
                        "content": json.dumps(
                            {
                                "error": "the message is out of character; the turn checklist "
                                "does not apply"
                            }
                        ),
                    }
                ],
                context=[],
                final=False,
            ),
        )
        for entry in _flush(run):
            yield entry
        return
    keys = _checklist_for(run, turn)[1]
    report, missing = _read_checklist_report(request, keys)
    retry = _checklist_retry(missing) if missing else ""
    messages: list[dict[str, Any]] = [
        {
            "role": "tool",
            "tool_call_id": request.id,
            "content": json.dumps({"error": retry}) if missing else json.dumps({"finished": True}),
        }
    ]
    context = [
        ContextPart(
            "assistant",
            f"GM state step (round {round_no}), "
            + ("incomplete checklist" if missing else "turn checklist"),
            _format_checklist_report(report),
        )
    ]
    if not missing:
        _record_state_finished(run, turn, messages, context)
        for entry in _flush(run):
            yield entry
        return
    retries = sum(1 for part in turn.context if part.source == "Checklist retry")
    if retries < CHECKLIST_RETRIES:
        context.append(ContextPart("system", "Checklist retry", retry))
        _record(
            run.engine,
            run.campaign_id,
            StateStepMade(requests=[], messages=messages, context=_dicts(context), final=False),
        )
    else:
        logger.warning("state step ended without checklist items: %s", ", ".join(missing))
        context.append(
            ContextPart(
                "system",
                "Checklist incomplete",
                "Missing: " + ", ".join(missing) + ".",
            )
        )
        _record_state_finished(run, turn, messages, context)
    for entry in _flush(run):
        yield entry


def _id_list(values: Iterable[str], limit: int = 60) -> str:
    ids = sorted(str(value) for value in values)
    shown = ", ".join(ids[:limit])
    return shown + (f", and {len(ids) - limit} more" if len(ids) > limit else "")


def _map_option_line(run: _Run) -> str:
    """The compact map ids the state step needs, so map_options is not a whole round."""
    rule_set = run.rule_set
    if not (rule_set.terrain or rule_set.encounter_objects or rule_set.encounter_templates):
        return ""
    parts = [
        f"terrains {_id_list(rule_set.terrain)}",
        f"objects {_id_list(rule_set.encounter_objects)}",
        f"templates {_id_list(rule_set.encounter_templates)}",
        f"patterns {_id_list(TERRAIN_PATTERNS)}",
        f"palettes {_id_list(MAP_PALETTES)}",
        f"ambients {_id_list(MAP_AMBIENTS)}",
    ]
    return (
        "Map ids (already given; do not call map_options just to get them): "
        + "; ".join(parts)
        + "."
    )


async def _drive(run: _Run) -> AsyncIterator[TurnEvent]:
    """Run the reply until it is recorded or paused. Log entries are yielded as the steps that
    recorded them finish. A pause is recorded before the player hears of it."""
    map_line = _map_option_line(run)
    state_message = {
        "role": "system",
        "content": state_instruction(run.rule_set, run.world)
        + (f"\n\n{map_line}" if map_line else ""),
    }
    while True:
        state = _state(run.engine, run.campaign_id)
        turn = _turn(state)
        # A queued line starts as soon as its intent is recorded, and a resumed reply starts
        # whatever the log still has waiting.
        run.speech.ensure_started(state, turn)
        text = run.opening if turn.opening else turn.text
        groups = state_tool_groups(state, text) | run.extra_groups
        tools = wire_tool_specs(
            run.tool_calls,
            mode=run.tool_schema_mode,
            include_groups=groups,
            load_tools=True,
        )
        checklist_required = _checklist_active(turn) and bool(_checklist_for(run, turn)[1])
        if checklist_required:
            tools = [*(tools or []), checklist_tool_spec(_checklist_items(run, turn))]
        ooc_tools = (
            [
                spec
                for spec in tools
                if spec["function"]["name"] not in (OOC_TOOL, FINISH_STATE_STEP)
            ]
            if tools is not None
            else None
        )
        if turn.queue:
            request = turn.queue[0]
            yield Status(_status_for(request))
            if request.name == FINISH_STATE_STEP:
                async for event in _finish_checklist(run, turn, request):
                    yield event
                continue
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
            if turn.speech_queue:
                # The out-of-character reply still hands any queued line to the narration that
                # follows it.
                turn = await run.speech.collect(turn)
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
            attempts = OPENING_REPLY_ATTEMPTS if turn.opening else 1
            reply = ""
            reasoning = ""
            requests: list[ToolRequest] = []
            displayed = False
            cleared = False
            for attempt in range(attempts):
                # A short opening is asked for again with the items it left out spelled out.
                request = (
                    view
                    if attempt == 0
                    else [*view, {"role": "system", "content": OPENING_REPLY_RETRY}]
                )
                step: ChatReply | None = None
                streamed = False
                async for arrived, item in _stream_with_heartbeat(
                    run.provider.stream(run.model_name, request, ooc_tools)
                ):
                    if not arrived:
                        yield Status(HEARTBEAT_MESSAGE)
                    elif isinstance(item, TextDelta):
                        streamed = True
                        yield Delta(item.text)
                    else:
                        step = item
                if step is None:
                    raise LLMError("the model returned no reply")
                # The ooc tool is not offered here. A model that calls it anyway must not restart
                # the out-of-character step; the state step already decided whether the story
                # continues.
                requests = [request for request in step.tool_requests if request.name != OOC_TOOL]
                if requests:
                    break
                text = step.text.strip()
                if text:
                    reply, reasoning = text, step.reasoning
                displayed = streamed
                if reply and (not turn.opening or _opening_reply_is_complete(reply)):
                    break
                if streamed and attempt + 1 < attempts:
                    # The player saw the short introduction; clear it before the retry writes in
                    # its place. The last attempt is left on screen if it is still short.
                    yield Retract()
                    displayed = False
                    cleared = True
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
            if not reply:
                raise LLMError("the model returned an empty reply")
            if cleared and not displayed:
                # A retry that came back empty cleared the earlier text from the page; show the
                # reply that is being kept.
                yield Delta(reply)
            if turn.ooc_narrate:
                messages: list[dict[str, Any]] = [_text_message(reply, reasoning)]
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
                        reasoning=reasoning,
                    ),
                )
                _after_reply(run, event_id)
                yield NextMessage()
                continue
            if turn.opening:
                async for event in _write_journal(run, turn, reply, reasoning):
                    yield event
            # An out-of-character reply can still be the reply that plays out an NPC's turn, so
            # it closes the turn exactly as a narrated reply does. Skipping this left the
            # encounter acting forever, with the table unable to send another message.
            event_id = _finish_reply(run, turn, reply, reasoning)
            yield Saved(event_id)
            return
        yield Status("Managing state...")
        round_no = turn.rounds + 1
        view = _state_messages(run, turn, state_message)
        # The first round plans the turn, and keeps full effort. Later rounds carry out that
        # plan, apply tool results, and write the checklist; low effort halves their output
        # there without changing the tool calls. The prompt still requires any move and the
        # speak call a beat turns on, so fewer rounds never means a thinner turn.
        state_provider = run.provider if turn.rounds == 0 or turn.opening else run.provider_low
        replied: list[ChatReply] = []
        async for event in _await_with_heartbeat(
            state_provider.chat(run.model_name, view, tools), replied
        ):
            yield event
        step = replied[0]
        if not step.tool_requests:
            retries = sum(1 for part in turn.context if part.source == "Checklist retry")
            if checklist_required and retries < CHECKLIST_RETRIES:
                retry = _checklist_retry(_checklist_for(run, turn)[1])
                context: list[ContextPart] = []
                if step.text.strip():
                    context.append(
                        ContextPart(
                            "assistant",
                            f"GM state step (round {round_no}), incomplete checklist",
                            step.text,
                        )
                    )
                context.append(ContextPart("system", "Checklist retry", retry))
                _record(
                    run.engine,
                    run.campaign_id,
                    StateStepMade(
                        requests=[],
                        messages=[],
                        context=_dicts(context),
                        final=False,
                    ),
                )
                continue
            messages: list[dict[str, Any]] = []
            context = []
            if step.text.strip():
                context.append(
                    ContextPart(
                        "assistant",
                        f"GM state step (round {round_no}), not shown to the narration",
                        step.text,
                    )
                )
            if checklist_required:
                logger.warning("state step ended without calling %s", FINISH_STATE_STEP)
                context.append(
                    ContextPart(
                        "system",
                        "Checklist incomplete",
                        f"The state step did not call {FINISH_STATE_STEP}.",
                    )
                )
            _record_state_finished(run, turn, messages, context)
            continue
        context = []
        if step.text:
            context.append(ContextPart("assistant", f"GM text (round {round_no})", step.text))
        # The finish tool must run after every other tool, so its notes describe the state the
        # other calls left. A repeated finish call would only add unanswered tool calls.
        requests = [request for request in step.tool_requests if request.name != FINISH_STATE_STEP]
        finishes = [request for request in step.tool_requests if request.name == FINISH_STATE_STEP]
        if finishes:
            requests.append(finishes[-1])
        recorded = (
            step
            if len(requests) == len(step.tool_requests)
            else ChatReply(text=step.text, tool_requests=tuple(requests), reasoning=step.reasoning)
        )
        _record(
            run.engine,
            run.campaign_id,
            StateStepMade(
                requests=[asdict(request) for request in requests],
                messages=[_tool_call_message(recorded, keep_reasoning=False)],
                context=_dicts(context),
            ),
        )

    if turn.speech_queue:
        # The state step is over; wait for the lines it queued before the narration needs them.
        turn = await run.speech.collect(turn)
    injection = _take_debug_injection(run)
    forced_narration = ""
    if injection is not None and injection.mode == "tool":
        async for event in _inject_debug_tool(run, turn, injection):
            yield event
        turn = _turn(_state(run.engine, run.campaign_id))
    elif injection is not None and injection.mode == "narration":
        forced_narration = injection.text
    # The journal reads only the record, so it is written beside the narration instead of in
    # the review's call. Only the campaign opening writes it before the narration exists.
    journal_tools = wire_tool_specs(run.tool_calls, journal_step=True, mode=run.tool_schema_mode)
    journal_task: asyncio.Task[None] | None = (
        asyncio.create_task(_journal_step(run, turn), name=f"journal-{run.campaign_id}")
        if journal_tools is not None
        else None
    )
    reply = ""
    reasoning = ""
    try:
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
        if journal_task is not None:
            # The journal reads only the record, so the review does not wait for it. It
            # finishes beside the review instead of adding its time before review starts.
            try:
                await journal_task
            except asyncio.CancelledError:
                raise
            except Exception:
                # The journal is a convenience, not part of the story. The next turn
                # covers whatever this one missed.
                logger.exception("the journal step for campaign %s failed", run.campaign_id)
            journal_task = None
    finally:
        if journal_task is not None and not journal_task.done():
            journal_task.cancel()
    event_id = _finish_reply(run, turn, reply, reasoning)
    yield Saved(event_id)


async def _carry_on(run: _Run) -> AsyncIterator[TurnEvent]:
    turn = _turn(_state(run.engine, run.campaign_id))
    if not turn.begun:
        async for event in _begin(run, turn):
            yield event
    try:
        async for event in _drive(run):
            yield event
    finally:
        # A reply that pauses at a prompt or is abandoned must not leave speech tasks writing
        # into a turn nobody is following. The intent stays in the log, so Continue writes it.
        run.speech.cancel()


async def _answer(
    run: _Run, answer: RollAnswer | TextAnswer | ChoiceAnswer | None
) -> AsyncIterator[TurnEvent]:
    """Give the waiting tool the player's answer, record its result, then carry the reply on. A
    choice runs the move band's chosen effects instead of the tool again, since the roll already
    happened. A tool that answers with a band's choices pauses here for the player to pick one."""
    turn = _turn(_state(run.engine, run.campaign_id))
    if turn.prompt is not None and turn.prompt.reaction:
        async for event in _answer_reaction(run, turn, turn.prompt, answer):
            yield event
        return
    if isinstance(answer, ChoiceAnswer):
        request = turn.queue[0]
        prompt = turn.prompt
        if prompt is None or not prompt.choices:
            raise InvalidInput("there is no choice to make")
        result = run.ctx.world.choose_move_option(
            prompt.move,
            prompt.band,
            answer.choice,
            target=prompt.target,
            skill=prompt.skill,
            weapon=prompt.weapon,
        )
        _record(
            run.engine,
            run.campaign_id,
            _tool_ran(turn.rounds, request, ToolRun(json.dumps(result), None)),
        )
        for entry in _flush(run):
            yield entry
    elif answer is not None:
        request = turn.queue[0]
        result = await run_tool(run.tool_calls, request, answer, ctx=run.ctx)
        if isinstance(result, Ask):
            # The roll reached a band or a reaction that needs the player. The tool result is
            # recorded when they answer, so the queue and the prompt stay until then.
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


async def _answer_reaction(
    run: _Run, turn: Turn, prompt: Ask, answer: RollAnswer | TextAnswer | ChoiceAnswer | None
) -> AsyncIterator[TurnEvent]:
    """Finish a check or save reaction with the player's own decision and roll."""
    pending = dict(prompt.reaction or {})
    request = turn.queue[0]
    if isinstance(answer, ChoiceAnswer):
        if answer.choice == "decline":
            initial = dict(pending.get("initial") or {})
            _record(
                run.engine,
                run.campaign_id,
                _tool_ran(turn.rounds, request, ToolRun(json.dumps(initial), None)),
            )
            for entry in _flush(run):
                yield entry
        elif answer.choice == "use":
            dice = str(pending.get("dice", ""))
            original = pending.get("original_total", pending.get("original_roll", ""))
            details = f"First result: {original}." if original != "" else ""
            ask = Ask(
                text=f"{pending.get('feature_name', 'The ability')}: roll {dice} for the reroll.",
                dice=dice,
                details=details or None,
                reaction=pending,
                character=str(pending.get("name", "") or ""),
            )
            _record(run.engine, run.campaign_id, _prompt_shown(ask))
            for entry in _flush(run):
                yield entry
            yield Prompt(ask.text, ask.dice, ask.details)
            return
        else:
            raise InvalidInput("choose whether to use the reaction")
    elif isinstance(answer, RollAnswer):
        final = run.ctx.world.resolve_reaction(pending, answer.total, answer.dice)
        _record(
            run.engine,
            run.campaign_id,
            _tool_ran(turn.rounds, request, ToolRun(json.dumps(final), None)),
        )
        for entry in _flush(run):
            yield entry
    else:
        raise InvalidInput("roll the reaction dice")
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


def _guarded_start(engine: Engine, campaign_id: int, build: Callable[[], _Run]) -> _Run:
    """Build a run, recording a failed reply when it cannot start.

    `_guarded` protects the steps after the run exists. Starting one can fail as well: the
    provider may have no configuration, an address this machine cannot reach, or a TLS setup
    it cannot create. Recording that as the open reply's failure lets the page show the reason
    and a Continue button, exactly like a failure inside the reply."""
    try:
        return build()
    except (Conflict, InvalidInput, NotFound):
        # A refusal, such as a credit shortfall, records nothing: the caller reports it and
        # the player fixes the cause before trying again.
        raise
    except Exception as exc:
        message = str(exc) if isinstance(exc, LLMError) else "an unexpected error"
        _record(engine, campaign_id, TurnFailed(error=message))
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
        _character_to_play(current, owner_id)
        if rule_set.combat is None or rule_set.combat.mode != "moves":
            _check_encounter_turn(current, owner_id)
        if advancement_due_for_player(current, rule_set, owner_id):
            raise InvalidInput("level up or choose your Advancement before sending another message")

        def build_run() -> _Run:
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
            return run

        run = _guarded_start(engine, campaign.id, build_run)
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
    use_optional: bool = False,
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
        state = _state(engine, campaign.id)
        turn = state.turn
        if turn is None:
            raise InvalidInput("there is no reply waiting for an answer")
        prompt_owner, prompt_name = state.prompt_owner(turn)
        if prompt_owner is not None and prompt_owner != owner_id:
            who = prompt_name or "another player"
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
            values, total = roll_values(turn.prompt.dice, rng, plus=rule_set.plus)
            answer = RollAnswer(total=total, dice=tuple(values), use_optional=use_optional)
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

        def build_run() -> _Run:
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
            # An opening has not made any model calls yet, so it is a new reply and needs
            # credit. A turn that already started is allowed to finish even if the balance
            # has since fallen.
            if not turn.begun:
                run.credits.require()
            return run

        run = _guarded_start(engine, campaign.id, build_run)
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
        if advancement_due_for_player(current, rule_set, owner_id):
            raise InvalidInput("level up or choose your Advancement before starting a turn")

        def build_run() -> _Run:
            run = _start_run(engine, providers, library, rule_set, campaign, context_tokens, rng)
            run.credits.require()
            return run

        run = _guarded_start(engine, campaign.id, build_run)
        with session_scope(engine) as db:
            character = encounters.begin_turn(db, campaign.id, name)
        if character.kind == PC_KIND:
            raise InvalidInput(
                f"{character.name} is a player character; their turn begins on its own"
            )
        async for event in _guarded(run, _carry_on(run)):
            yield event


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
    state = _state(engine, campaign_id)
    turn = state.turn
    if turn is None:
        return None
    if turn.prompt is not None:
        player_id, player_name = state.prompt_owner(turn)
        return PendingView(
            prompt=turn.prompt,
            opening=turn.opening,
            player_id=player_id,
            player_name=player_name,
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


def edit_last_reply(
    engine: Engine, user_id: int, campaign_id: int, event_id: int, text: str
) -> None:
    """Replace the text of the campaign's most recent GM reply. The reply's turn, its tool
    calls, and the rest of the Log stay as they were, and the next turn reads the edited text
    as history. Only the newest reply may be edited, so a correction cannot rewrite settled
    story."""
    _campaigns().get_campaign(engine, user_id, campaign_id)
    content = text.strip()
    if not content:
        raise InvalidInput("the edited reply cannot be empty")
    if len(content) > MAX_MESSAGE:
        raise InvalidInput(f"the edited reply must be at most {MAX_MESSAGE} characters")
    with _campaigns()._exclusive(campaign_id), session_scope(engine) as db:
        event = db.get(Event, event_id)
        if event is None or event.campaign_id != campaign_id or event.kind != ChatGM.kind:
            raise NotFound("message not found")
        latest = (
            db.exec(
                select(Event.id)
                .where(Event.campaign_id == campaign_id, Event.kind == ChatGM.kind)
                .order_by(Event.seq.desc())
                .limit(1)
            )
            .scalars()
            .one_or_none()
        )
        if latest != event_id:
            raise InvalidInput("only the most recent GM reply can be edited")
        record(db, campaign_id, ChatGMEdited(reply_id=event_id, content=content))
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
