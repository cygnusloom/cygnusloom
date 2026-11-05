from __future__ import annotations

import json
import random
import re
import threading
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine
from sqlmodel import select

from cygnusforge.content.character import CampaignSpec, CharacterSpec, roll_stats
from cygnusforge.content.definitions import ContentError
from cygnusforge.content.library import (
    ContentLibrary,
    RuleSet,
)
from cygnusforge.db.models import Campaign, Event
from cygnusforge.llm.base import (
    LLMError,
    Provider,
    complete,
)
from cygnusforge.services import combat
from cygnusforge.services.common import Conflict, InvalidInput, NotFound, session_scope
from cygnusforge.services.events import (
    CHARS_PER_TOKEN,
    CampaignState,
    Channel,
    ChatGM,
    ChatPlayer,
    ContextPart,
    TurnFailed,
    decode,
    estimate_tokens,
    events_since,
    is_hidden,
)
from cygnusforge.services.names import next_campaign_label
from cygnusforge.services.prompts import (
    NAME_CHECK_PROMPT,
    SUMMARY_HEADING,
    summary_messages,
    system_parts,
)
from cygnusforge.services.tools import (
    tool_specs,
)
from cygnusforge.services.views import present_rules_text, world_state_text
from cygnusforge.services.world import (
    create_player_character,
    promote_hireling,
    seed_campaign,
    set_campaign_options,
)

MAX_NAME = 120
MAX_MESSAGE = 8000
# The player's optional setting/story brief at campaign creation. It is prompt text, not state.
MAX_SETTING = 4000
# Spoken words never contain angle brackets or call syntax, so either means a tool call was written.
SPOKEN_MARKUP = re.compile(r"<[^>]*>|\b\w+\(\s*\{")
# An empty speech reply is retried this many times before the step gives up.
SPEECH_ATTEMPTS = 3
# An empty narration is requested again this many times before the turn fails.
NARRATION_ATTEMPTS = 2
JOURNAL_ROUNDS = 3
# Once the prompt reaches this share of the context window, older history is summarized.
COMPRESS_AT = 0.5
# After a summary, the newest messages worth up to this share of the window stay verbatim.
KEEP_RECENT = 0.1
# A summary may take up to this share of the window. Longer ones keep only their newest text.
SUMMARY_SHARE = 0.25
WORDS_PER_TOKEN = 0.75


def create_campaign(
    engine: Engine,
    library: ContentLibrary,
    allowed_models: Iterable[str],
    owner_id: int,
    name: str,
    world_template: str,
    rule_set: str,
    model: str,
    setting: str = "",
    rng: random.Random | None = None,
) -> Campaign:
    name = (name or "").strip()
    if len(name) > MAX_NAME:
        raise InvalidInput(f"label must be 1 to {MAX_NAME} characters")
    setting = (setting or "").strip()
    if len(setting) > MAX_SETTING:
        raise InvalidInput(f"setting and story must be at most {MAX_SETTING} characters")
    if world_template not in library.world_templates:
        raise InvalidInput("that world template is not available")
    if rule_set not in library.rule_sets:
        raise InvalidInput("that rule set is not available")
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    template = library.world_templates[world_template]
    if template.rule_set is not None and rule_set != template.rule_set:
        raise InvalidInput(
            f"that world only works with the {library.rule_sets[template.rule_set].name} rule set"
        )
    try:
        content = library.world_content(world_template, rule_set)
    except ContentError as error:
        raise InvalidInput(f"that world does not work with that rule set: {error}") from error
    with session_scope(engine) as db:
        if not name:
            # A blank label gets the world's next default: "Twin Lakes #1", "Twin Lakes #2"...
            existing = db.exec(
                select(Campaign.name).where(
                    Campaign.owner_id == owner_id,
                    Campaign.world_template == world_template,
                )
            ).all()
            name = next_campaign_label(template.name, existing)
        campaign = Campaign(
            owner_id=owner_id,
            name=name,
            world_template=world_template,
            rule_set=rule_set,
            model=model,
            setting=setting,
        )
        db.add(campaign)
        db.flush()
        marketplace = library.rule_sets[rule_set].marketplace
        # Seeding commits the campaign's world with it. If it fails, nothing is kept.
        seed_campaign(
            db,
            campaign.id,
            template,
            library.core.tables,
            rng if rng is not None else random.Random(),
            library.rule_sets[rule_set].character,
            monsters={**library.rule_sets[rule_set].monsters, **content.monsters},
            plus=library.rule_sets[rule_set].plus,
            npc_hp=library.rule_sets[rule_set].npc_hp,
            npc_hp_stat=library.rule_sets[rule_set].npc_hp_stat,
            combat_rules=library.rule_sets[rule_set].combat,
            inventory=library.rule_sets[rule_set].inventory,
            item_sizes=library.rule_sets[rule_set].item_sizes,
            magic=library.rule_sets[rule_set].magic,
            currency=marketplace.currency if marketplace is not None else "",
            wilderness=library.rule_sets[rule_set].wilderness,
        )
        db.refresh(campaign)
        return campaign


def roll_player_stats(
    engine: Engine,
    library: ContentLibrary,
    owner_id: int,
    campaign_id: int,
    rng: random.Random | None = None,
) -> dict[str, int]:
    """Roll the stats a new player character starts from. The page keeps the roll until the
    player submits; the roll is not stored in the campaign."""
    campaign = get_campaign(engine, owner_id, campaign_id)
    spec = character_spec(library, campaign)
    return roll_stats(
        spec,
        rng if rng is not None else random.Random(),
        plus=library.rule_sets[campaign.rule_set].plus,
    )


@dataclass(frozen=True)
class NewCharacter:
    """The player character a creation form stands for, before its stats are resolved."""

    name: str
    rolled: Mapping[str, int]
    submitted: Mapping[str, Any]
    pronouns: str


def save_creation(
    engine: Engine,
    library: ContentLibrary,
    owner_id: int,
    campaign_id: int,
    *,
    options: Mapping[str, Any] | None = None,
    character: NewCharacter | None = None,
) -> None:
    """Save the campaign options and the player character together, so either both are saved or
    neither is. A part left as None is not saved. The character needs the options first."""
    campaign = get_campaign(engine, owner_id, campaign_id)
    spec = campaign_spec(library, campaign)
    with _exclusive(campaign_id), session_scope(engine) as db:
        if options is not None:
            set_campaign_options(db, campaign_id, spec, options)
        if character is not None:
            rule_set = library.rule_sets[campaign.rule_set]
            create_player_character(
                db,
                campaign_id,
                spec,
                character_spec(library, campaign),
                character.name,
                character.rolled,
                character.submitted,
                random.Random(),
                inventory=rule_set.inventory,
                item_sizes=rule_set.item_sizes,
                magic=rule_set.magic,
                currency=rule_set.marketplace.currency if rule_set.marketplace else "",
                marketplace=rule_set.marketplace,
                armor_max=rule_set.combat.armor_max if rule_set.combat else 0,
                defense_base=rule_set.combat.defense_base if rule_set.combat else 0,
                defense_modifier=rule_set.combat.defense_modifier if rule_set.combat else "",
                pronouns=character.pronouns,
                plus=rule_set.plus,
                combat_rules=rule_set.combat,
            )
        db.commit()


def promote_hireling_to_character(
    engine: Engine, owner_id: int, campaign_id: int, name: str
) -> dict[str, Any]:
    """After the player character died, a living hireling takes over as the player character."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return promote_hireling(db, campaign_id, name)


def character_spec(library: ContentLibrary, campaign: Campaign) -> CharacterSpec:
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        raise InvalidInput("that campaign's rule set is no longer available")
    return rule_set.character


def campaign_spec(library: ContentLibrary, campaign: Campaign) -> CampaignSpec:
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        raise InvalidInput("that campaign's rule set is no longer available")
    return rule_set.campaign


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


def default_campaign_labels(
    engine: Engine, library: ContentLibrary, owner_id: int
) -> dict[str, str]:
    """The label a new campaign of each world would get by default."""
    with session_scope(engine) as db:
        rows = db.exec(
            select(Campaign.world_template, Campaign.name).where(Campaign.owner_id == owner_id)
        ).all()
    existing: dict[str, list[str]] = defaultdict(list)
    for world_template, name in rows:
        existing[world_template].append(name)
    return {
        world_id: next_campaign_label(template.name, existing.get(world_id, ()))
        for world_id, template in library.world_templates.items()
    }


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


def delete_campaign(engine: Engine, owner_id: int, campaign_id: int) -> None:
    """Delete a campaign and its log. Another account's campaign looks the same as a missing
    one. Refused while the GM is still writing a reply."""
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.owner_id != owner_id:
            raise NotFound("campaign not found")
        db.execute(Event.__table__.delete().where(Event.campaign_id == campaign_id))
        db.delete(campaign)
        db.commit()


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


def set_campaign_label(engine: Engine, owner_id: int, campaign_id: int, label: str) -> Campaign:
    """Change the label the campaign list shows. The label is only the player's name for the
    campaign; it is not part of the log."""
    label = (label or "").strip()
    if not label or len(label) > MAX_NAME:
        raise InvalidInput(f"label must be 1 to {MAX_NAME} characters")
    get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.name = label
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


@dataclass(frozen=True)
class CampaignWorld:
    """The folded campaign state."""

    state: CampaignState


def campaign_world(engine: Engine, owner_id: int, campaign_id: int) -> CampaignWorld:
    get_campaign(engine, owner_id, campaign_id)
    return CampaignWorld(state=_state(engine, campaign_id))


def set_debug_invincibility(
    engine: Engine, owner_id: int, campaign_id: int, name: str, active: bool
) -> dict[str, Any]:
    """Turn the debug invincibility buff on or off for a player character. Refused while the GM
    is still writing a reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return combat.set_invincibility(db, campaign_id, name, active)


def set_debug_stat(
    engine: Engine, owner_id: int, campaign_id: int, name: str, stat: str, remaining: int
) -> dict[str, Any]:
    """Set a player character's current score for one stat from the debug panel. Refused while the
    GM is still writing a reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return combat.set_debug_stat(db, campaign_id, name, stat, remaining)


def set_debug_currency(
    engine: Engine, owner_id: int, campaign_id: int, name: str, feature: str, amount: int
) -> dict[str, Any]:
    """Give a player character some of the rule set's currency from the debug panel. Refused while
    the GM is still writing a reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return combat.add_debug_currency(db, campaign_id, name, feature, amount)


@dataclass(frozen=True)
class ChatMessage:
    """A line of the chat: the player's, or the GM's reply with the model that wrote it. `id` is
    the id of the event that recorded it. `speaker` is the character a player's message spoke for."""

    id: int
    role: str
    content: str
    model: str | None = None
    speaker: str | None = None
    reasoning: str = ""


def list_messages(engine: Engine, campaign_id: int) -> list[ChatMessage]:
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, channel=Channel.TRANSCRIPT)
        return [_chat_message(row) for row in rows]


def campaign_transcript(
    engine: Engine, owner_id: int, campaign_id: int
) -> list[ChatMessage | LogLine]:
    """The chat as shown: the messages with the Log's entries among them, in the order they
    happened."""
    get_campaign(engine, owner_id, campaign_id)
    with session_scope(engine) as db:
        # A failure is news only until a later step goes through, as in load_state.
        progress = max(
            (row.id or 0 for row in events_since(db, campaign_id) if row.kind != TurnFailed.kind),
            default=0,
        )
        items: list[ChatMessage | LogLine] = [
            _chat_message(row) for row in events_since(db, campaign_id, channel=Channel.TRANSCRIPT)
        ]
        for row in events_since(db, campaign_id, channel=Channel.LOG):
            event = decode(row)
            if isinstance(event, TurnFailed) and (row.id or 0) < progress:
                continue
            if (text := event.render()) is not None:
                items.append(LogLine(id=row.id or 0, content=text, hidden=is_hidden(row.kind)))
    return sorted(items, key=lambda item: item.id)


def _chat_message(row: Event) -> ChatMessage:
    event = decode(row)
    if isinstance(event, ChatPlayer):
        return ChatMessage(
            id=row.id or 0, role="user", content=event.content, speaker=event.speaker
        )
    if isinstance(event, ChatGM):
        return ChatMessage(
            id=row.id or 0,
            role="assistant",
            content=event.content,
            model=event.model,
            reasoning=event.reasoning,
        )
    raise TypeError(f"{row.kind} is not a chat event")


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
    rule_set: RuleSet | None,
    options: CampaignSpec,
    limit: int,
    world_rules: Mapping[str, str],
    unattached_world_rules: Mapping[str, str] | None = None,
) -> ContextUsage:
    unsummarized = [m for m in history if m.id > state.through]
    all_rules = {
        **(rule_set.rules if rule_set else {}),
        **(rule_set.attached_rules if rule_set else {}),
        **world_rules,
    }
    used = sum(
        part.tokens
        for part in system_parts(
            campaign.rule_set,
            {**(rule_set.rules if rule_set else {}), **(unattached_world_rules or {})},
            rule_set.important if rule_set else frozenset(),
            state.summary,
            state.through,
            world_state_text(
                state,
                options,
                rule_set.grid if rule_set else None,
                rule_set.combat if rule_set else None,
                rule_set.conditions if rule_set else None,
                clock_rules=rule_set.clock if rule_set else None,
                inventory=rule_set.inventory if rule_set else None,
                hooks=rule_set.hooks if rule_set else None,
                counters=rule_set.counters if rule_set else None,
            ),
            present_rules_text(state, all_rules),
            campaign.setting,
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


async def _repeated_place(
    provider: Provider, model_name: str, names: Sequence[str], name: str
) -> str | None:
    """The existing place that `name` repeats, or None. The model judges the wording, so a name
    that repeats a place in other words is caught too."""
    if not names:
        return None
    listing = "\n".join(f"- {existing}" for existing in names)
    messages = [
        {"role": "system", "content": NAME_CHECK_PROMPT},
        {"role": "user", "content": f"Existing places:\n{listing}\n\nNew place: {name}"},
    ]
    reply = (await complete(provider, model_name, messages)).strip()
    if not reply.upper().startswith("REPEAT:"):
        return None
    named = reply.split(":", 1)[1].strip().casefold()
    return next((existing for existing in names if existing.casefold() == named), None)


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
    prompt = summary_messages(previous, transcript, words)
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


from cygnusforge.services.turns import (  # noqa: F401
    Delta,
    Logged,
    LogLine,
    NextMessage,
    PendingView,
    Prompt,
    Saved,
    Status,
    TurnEvent,
    _Run,
    _state,
    _text_message,
    _tool_call_message,
    _tool_context,
    _turn_has_day_end,
    _without_draft,
    begin_encounter_turn,
    campaign_log,
    discard_pending,
    end_encounter_turn,
    end_player_turn,
    get_turn_context,
    get_turn_transcript,
    pending_turn,
    resume_turn,
    rollback_to,
    send_message,
    start_encounter_turn,
    stream_message,
)
