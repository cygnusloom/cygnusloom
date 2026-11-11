from __future__ import annotations

import json
import random
import re
import threading
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, func
from sqlmodel import select

from cygnusloom import CAMPAIGN_MAJOR_VERSION
from cygnusloom.content.character import CampaignSpec, CharacterSpec, roll_stats
from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import (
    ContentLibrary,
    RuleSet,
    WorldTemplate,
    read_table_genres,
)
from cygnusloom.db.models import Campaign, CampaignInvite, CampaignMember, Event, User
from cygnusloom.llm.base import (
    LLMError,
    Provider,
    complete,
)
from cygnusloom.services import combat, encounters
from cygnusloom.services import credits as credit_service
from cygnusloom.services.accounts import public_name
from cygnusloom.services.common import Conflict, InvalidInput, NotFound, session_scope
from cygnusloom.services.events import (
    CHARS_PER_TOKEN,
    CampaignState,
    Channel,
    ChatGM,
    ChatPlayer,
    ContextPart,
    DebugInjectionCleared,
    DebugInjectionSet,
    TurnFailed,
    decode,
    estimate_tokens,
    events_since,
    is_hidden,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.names import next_campaign_label
from cygnusloom.services.prompts import (
    NAME_CHECK_PROMPT,
    SUMMARY_HEADING,
    state_instruction,
    summary_messages,
    system_parts,
)
from cygnusloom.services.tools import (
    state_tool_groups,
    wire_tool_specs,
)
from cygnusloom.services.views import present_rules_text, world_state_text
from cygnusloom.services.world import (
    create_player_character,
    promote_hireling,
    seed_campaign,
    set_campaign_options,
)

MAX_NAME = 120
MAX_MESSAGE = 8000
# A model id is `provider/model`, matching the campaign column.
MAX_MODEL = 120


class NotPlayable(Conflict):
    """A campaign from another major version. Its row and log stay intact and exportable."""


# The campaign-wide settings the AI GM may change during a turn, and only on the owner's
# explicit request.
TURN_SETTINGS = {"inventory_space"}
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


def _campaign_table_genres(
    table_genres: Sequence[str] | None, rule_set: RuleSet
) -> tuple[str, ...]:
    """The genres a new campaign's generation tables are filtered by. Leaving the choice empty
    keeps the rule set's own selection; every named genre must be one the engine knows."""
    if not table_genres:
        return rule_set.table_genres
    try:
        return read_table_genres(list(table_genres), "table genre")
    except ContentError as error:
        raise InvalidInput(str(error)) from error


def create_campaign(
    engine: Engine,
    library: ContentLibrary,
    allowed_models: Iterable[str],
    owner_id: int,
    name: str,
    world_template: str,
    rule_set: str,
    model: str,
    table_genres: Sequence[str] | None = None,
    rng: random.Random | None = None,
) -> Campaign:
    name = (name or "").strip()
    if len(name) > MAX_NAME:
        raise InvalidInput(f"label must be 1 to {MAX_NAME} characters")
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
    genres = _campaign_table_genres(table_genres, library.rule_sets[rule_set])
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
            major_version=CAMPAIGN_MAJOR_VERSION,
            model=model,
            table_genres=json.dumps(list(genres)),
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
        _seed_local_maps(db, campaign.id, template, library.rule_sets[rule_set])
        db.refresh(campaign)
        return campaign


def _seed_local_maps(db: Any, campaign_id: int, template: Any, rule_set: RuleSet) -> None:
    """Create the authored local map of every place that names one, and put the NPCs already
    there on its free cells. A map is created without an encounter, so the player can walk its
    rooms before a fight and the same map becomes the fight's board."""
    # Loading the state once keeps the seeding linear: replaying the growing log for every map
    # and every token made a world with authored maps slow to start.
    state = load_state(db, campaign_id)
    for spot in template.locations:
        if not spot.map:
            continue
        name = f"{spot.name} local map"
        encounters.create_map(
            db,
            campaign_id,
            name,
            spot.name,
            description=f"A local map of {spot.name}.",
            template=spot.map,
            templates=rule_set.encounter_templates,
            state=state,
        )
    for spot in template.locations:
        if not spot.map:
            continue
        name = f"{spot.name} local map"
        entry = state.maps.get(name_key(name))
        if entry is None:
            continue
        occupants = set(entry.tokens.values())
        characters = [
            character
            for character in state.characters.values()
            if character.location is not None
            and name_key(character.location) == name_key(spot.name)
        ]
        for character in characters:
            for row in range(entry.rows):
                for column in range(entry.columns):
                    if (column, row) in occupants:
                        continue
                    try:
                        encounters.place_map_token(
                            db,
                            campaign_id,
                            name,
                            character.name,
                            column,
                            row,
                            rule_set.encounter_objects,
                            state=state,
                        )
                    except InvalidInput:
                        continue
                    occupants.add((column, row))
                    break
                else:
                    continue
                break


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
    setting: str | None = None,
    table_genres: Sequence[str] | None = None,
) -> None:
    """Save the campaign options, the player character, the setting and story, and the table
    genres together, so either all of them are saved or none is. A part left as None is not
    saved. The character needs the options first. The setting and genres are captured on the
    campaign-options creation page, not when the campaign is first made."""
    campaign = get_campaign(engine, owner_id, campaign_id)
    spec = campaign_spec(library, campaign)
    brief = None
    if setting is not None:
        brief = (setting or "").strip()
        if len(brief) > MAX_SETTING:
            raise InvalidInput(f"setting and story must be at most {MAX_SETTING} characters")
    genres = None
    if table_genres is not None:
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        genres = _campaign_table_genres(table_genres, rule_set)
    with _exclusive(campaign_id), session_scope(engine) as db:
        state = load_state(db, campaign_id)
        # Only the campaign's first character sets the shared options, setting, and genres. A
        # player joining a campaign already under way makes their character and nothing else.
        first_character = not state.pcs()
        if (brief is not None or genres is not None) and first_character:
            stored = db.get(Campaign, campaign_id)
            if stored is not None:
                if brief is not None:
                    stored.setting = brief
                if genres is not None:
                    stored.table_genres = json.dumps(list(genres))
        if options is not None:
            set_campaign_options(db, campaign_id, spec, options)
        if character is not None:
            rule_set = library.rule_sets[campaign.rule_set]
            previous = state.player_character(owner_id)
            # A replacement for this player's out-of-play character gets an opening turn again.
            # A player joining a campaign already under way does not; the GM meets them on the
            # next turn through the world state.
            opening = first_character or previous is not None
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
                player_id=owner_id,
                opening=opening,
            )
        db.commit()


def promote_hireling_to_character(
    engine: Engine, owner_id: int, campaign_id: int, name: str
) -> dict[str, Any]:
    """After the player character died, a living hireling takes over as the player character."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return promote_hireling(
            db,
            campaign_id,
            name,
            player_id=owner_id,
        )


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
    """The campaigns this account owns. Shared campaigns are listed by `visible_campaigns`."""
    with session_scope(engine) as db:
        query = (
            select(Campaign)
            .where(Campaign.owner_id == owner_id)
            .order_by(Campaign.created_at.desc(), Campaign.id.desc())
        )
        return list(db.exec(query).all())


@dataclass(frozen=True)
class CampaignCard:
    """One campaign on the list, with the account that owns it and the viewer's part in it."""

    campaign: Campaign
    owner_name: str = ""
    shared: bool = False
    # When the campaign's newest event happened, and that time as the list shows it.
    last_event_at: datetime | None = None
    last_activity: str = ""
    # False for a campaign from another major version: export stays available, play does not.
    playable: bool = True


def last_activity_text(when: datetime | None, now: datetime | None = None) -> str:
    """A relative time with its date, such as '3 hours ago (2026-11-07)'. A database time
    without a timezone is taken as UTC."""
    if when is None:
        return ""
    when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    now = now or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:
        relative = "just now"
    elif seconds < 3600:
        minutes = seconds // 60
        relative = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif seconds < 86400:
        hours = seconds // 3600
        relative = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = seconds // 86400
        relative = f"{days} day{'s' if days != 1 else ''} ago"
    return f"{relative} ({when.strftime('%Y-%m-%d')})"


def visible_campaigns(engine: Engine, user_id: int) -> list[CampaignCard]:
    """Every campaign the account can open: its own first, then the ones shared with it."""
    with session_scope(engine) as db:
        owned = db.exec(
            select(Campaign)
            .where(Campaign.owner_id == user_id)
            .order_by(Campaign.created_at.desc(), Campaign.id.desc())
        ).all()
        shared = db.exec(
            select(Campaign)
            .join(CampaignMember, CampaignMember.campaign_id == Campaign.id)
            .where(
                CampaignMember.user_id == user_id,
                CampaignMember.removed_at.is_(None),
            )
            .order_by(Campaign.created_at.desc(), Campaign.id.desc())
        ).all()
        owners = {
            row.id: row
            for row in db.exec(
                select(User).where(User.id.in_({c.owner_id for c in [*owned, *shared]}))
            ).all()
        }
        campaign_ids = [c.id for c in [*owned, *shared] if c.id is not None]
        last_events: dict[int, datetime] = {}
        if campaign_ids:
            last_events = {
                campaign_id: last_event
                for campaign_id, last_event in db.exec(
                    select(Event.campaign_id, func.max(Event.created_at))
                    .where(Event.campaign_id.in_(campaign_ids))
                    .group_by(Event.campaign_id)
                ).all()
            }
        cards: list[CampaignCard] = []
        for campaign in owned:
            last_event = last_events.get(campaign.id or 0) or campaign.created_at
            cards.append(
                CampaignCard(
                    campaign=campaign,
                    last_event_at=last_event,
                    last_activity=last_activity_text(last_event),
                    playable=is_playable(campaign),
                )
            )
        for campaign in shared:
            owner = owners.get(campaign.owner_id)
            last_event = last_events.get(campaign.id or 0) or campaign.created_at
            cards.append(
                CampaignCard(
                    campaign=campaign,
                    owner_name=public_name(owner),
                    shared=True,
                    last_event_at=last_event,
                    last_activity=last_activity_text(last_event),
                    playable=is_playable(campaign),
                )
            )
        return cards


def is_playable(campaign: Campaign) -> bool:
    """Whether this engine can play the campaign. Other-major campaigns stay exportable."""
    return campaign.major_version == CAMPAIGN_MAJOR_VERSION


def get_campaign(
    engine: Engine, user_id: int, campaign_id: int, *, playable: bool = True
) -> Campaign:
    """A campaign the account may play: its own, or one shared with it. Anything else looks
    exactly like a missing campaign, so nothing leaks. `playable=False` is for the export and
    blocked-page paths, which must still reach another major's campaign."""
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFound("campaign not found")
        allowed = campaign.owner_id == user_id
        if not allowed:
            member = db.exec(
                select(CampaignMember.id).where(
                    CampaignMember.campaign_id == campaign_id,
                    CampaignMember.user_id == user_id,
                    CampaignMember.removed_at.is_(None),
                )
            ).first()
            allowed = member is not None
        if not allowed:
            raise NotFound("campaign not found")
        if playable and not is_playable(campaign):
            raise NotPlayable(
                f"this campaign was made for Cygnus Loom {campaign.major_version}; "
                f"Cygnus Loom {CAMPAIGN_MAJOR_VERSION} can export it but not play it"
            )
        return campaign


def get_owned_campaign(engine: Engine, user_id: int, campaign_id: int) -> Campaign:
    """A campaign the account owns. Configuration and destructive controls use this, so a
    member who can play cannot rename, switch the model, export, or delete it."""
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.owner_id != user_id:
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
TURN_BUSY_MESSAGE = "the GM is still working on this campaign; try again when it finishes"
_busy_lock = threading.Lock()
_busy_campaigns: set[int] = set()


@dataclass
class _LiveTurn:
    """What the running turn is doing, for every other page at the table.

    It is a hint, like a typing indicator: it changes while the GM works and disappears when the
    turn ends. The saved reply is the source of truth; a page replaces this with the stored
    message when the live snapshot reaches it."""

    activity: str = ""
    reply: str = ""


_live_turns: dict[int, _LiveTurn] = {}


def turn_busy(campaign_id: int) -> bool:
    """Whether this process is writing a turn for the campaign right now."""
    with _busy_lock:
        return campaign_id in _busy_campaigns


def turn_progress(campaign_id: int) -> tuple[str, str]:
    """The running turn's latest status line and the narration streamed so far."""
    with _busy_lock:
        live = _live_turns.get(campaign_id)
        return (live.activity, live.reply) if live is not None else ("", "")


def set_turn_activity(campaign_id: int, text: str) -> None:
    """Record what the GM is doing, so another player's page can show the same status."""
    with _busy_lock:
        live = _live_turns.get(campaign_id)
        if live is not None:
            live.activity = text


def append_turn_reply(campaign_id: int, text: str) -> None:
    """Add streamed narration, so another player's page can follow the reply as it is written."""
    with _busy_lock:
        live = _live_turns.get(campaign_id)
        if live is not None:
            live.reply += text


def clear_turn_reply(campaign_id: int) -> None:
    """Drop the streamed narration, such as when the GM sets it aside to write it again."""
    with _busy_lock:
        live = _live_turns.get(campaign_id)
        if live is not None:
            live.reply = ""


@contextmanager
def _exclusive(campaign_id: int) -> Iterator[None]:
    with _busy_lock:
        if campaign_id in _busy_campaigns:
            raise Conflict(TURN_BUSY_MESSAGE)
        _busy_campaigns.add(campaign_id)
        _live_turns[campaign_id] = _LiveTurn()
    try:
        yield
    finally:
        with _busy_lock:
            _busy_campaigns.discard(campaign_id)
            _live_turns.pop(campaign_id, None)


def delete_campaign(engine: Engine, owner_id: int, campaign_id: int) -> None:
    """Delete a campaign and its log. Another account's campaign looks the same as a missing
    one. Refused while the GM is still writing a reply."""
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.owner_id != owner_id:
            raise NotFound("campaign not found")
        db.execute(Event.__table__.delete().where(Event.campaign_id == campaign_id))
        db.execute(
            CampaignMember.__table__.delete().where(CampaignMember.campaign_id == campaign_id)
        )
        db.execute(
            CampaignInvite.__table__.delete().where(CampaignInvite.campaign_id == campaign_id)
        )
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
    get_owned_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.model = model
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


def set_campaign_inventory_space(
    engine: Engine, owner_id: int, campaign_id: int, enabled: bool
) -> Campaign:
    """Turn the rule set's inventory space on or off for the campaign. Off means capacity is not
    checked: characters may carry anything reasonable, no penalty item is added, and a full pack
    drains nothing. Existing items are left as they are."""
    get_owned_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.use_inventory_space = enabled
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


def update_campaign_settings(
    engine: Engine,
    allowed_models: Iterable[str],
    owner_id: int,
    campaign_id: int,
    *,
    model: str,
    inventory_space: bool,
    label: str,
) -> Campaign:
    """Save the owner's manual campaign settings in one write: the model, the inventory switch,
    and the label. They are configuration, not part of the log, so they may change mid-game."""
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    label = (label or "").strip()
    if not label or len(label) > MAX_NAME:
        raise InvalidInput(f"label must be 1 to {MAX_NAME} characters")
    get_owned_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.model = model
        campaign.use_inventory_space = inventory_space
        campaign.name = label
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


def set_campaign_setting_from_turn(
    engine: Engine, campaign_id: int, setting: str, value: bool
) -> dict[str, Any]:
    """Change a campaign-wide setting during a turn, at the owner's explicit request.

    Only the settings in `TURN_SETTINGS` may change this way, and only while the campaign owner's
    own turn is open. The turn already holds the campaign's turn lock, so this must not take it.
    """
    if setting not in TURN_SETTINGS:
        raise InvalidInput(f"{setting!r} is not a campaign setting the GM can change")
    if not isinstance(value, bool):
        raise InvalidInput("the setting's value must be true or false")
    with session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFound("campaign not found")
        turn = load_state(db, campaign_id).turn
        if turn is None or turn.player_id != campaign.owner_id:
            raise InvalidInput("only the campaign's owner can change its settings")
        if setting == "inventory_space":
            campaign.use_inventory_space = value
            db.add(campaign)
            db.commit()
            detail = (
                "Inventory space is tracked again."
                if value
                else "Inventory space is no longer tracked; anything reasonable can be carried."
            )
            return {"setting": setting, "value": value, "detail": detail}
    raise InvalidInput(f"{setting!r} is not a campaign setting the GM can change")


def set_campaign_label(engine: Engine, owner_id: int, campaign_id: int, label: str) -> Campaign:
    """Change the label the campaign list shows. The label is only the player's name for the
    campaign; it is not part of the log."""
    label = (label or "").strip()
    if not label or len(label) > MAX_NAME:
        raise InvalidInput(f"label must be 1 to {MAX_NAME} characters")
    get_owned_campaign(engine, owner_id, campaign_id)
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


def set_debug_credits(
    engine: Engine, owner_id: int, campaign_id: int, balance: int, *, force: bool
) -> None:
    """Set the campaign owner's credit balance and choose whether an administrator is charged in
    this campaign. Debug-only; the normal admin page handles real accounts."""
    campaign = get_owned_campaign(engine, owner_id, campaign_id)
    credit_service.set_balance(
        engine,
        campaign.owner_id,
        balance,
        reason=credit_service.DEBUG_REASON,
        note=f"debug panel, campaign {campaign.id}",
        campaign_id=campaign.id,
        actor_id=owner_id,
    )
    with session_scope(engine) as db:
        row = db.get(Campaign, campaign.id)
        assert row is not None
        row.force_credit_charges = force
        db.add(row)
        db.commit()


def set_debug_injection(
    engine: Engine,
    owner_id: int,
    campaign_id: int,
    mode: str,
    tool: str = "",
    arguments: str = "{}",
    text: str = "",
) -> dict[str, Any]:
    """Set the one-shot fault the next GM turn must carry, for testing the review step. `tool`
    names a tool call run just before the narration; `text` replaces the narration. Refused while
    the GM is still writing a reply."""
    get_campaign(engine, owner_id, campaign_id)
    if mode == "tool":
        tool = (tool or "").strip()
        if not tool:
            raise InvalidInput("choose a tool to inject")
        if len(tool) > MAX_NAME:
            raise InvalidInput(f"tool must be at most {MAX_NAME} characters")
        arguments = (arguments or "{}").strip() or "{}"
        if len(arguments) > MAX_MESSAGE:
            raise InvalidInput(f"arguments must be at most {MAX_MESSAGE} characters")
        text = ""
    elif mode == "narration":
        text = (text or "").strip()
        if not text:
            raise InvalidInput("text must hold the narration to inject")
        if len(text) > MAX_MESSAGE:
            raise InvalidInput(f"text must be at most {MAX_MESSAGE} characters")
        tool = ""
        arguments = "{}"
    else:
        raise InvalidInput("mode must be tool or narration")
    with _exclusive(campaign_id), session_scope(engine) as db:
        record(
            db,
            campaign_id,
            DebugInjectionSet(mode=mode, tool=tool, arguments=arguments, text=text),
        )
        db.commit()
    return {"injection": mode, "tool": tool}


def clear_debug_injection(engine: Engine, owner_id: int, campaign_id: int) -> None:
    """Remove a pending debug injection. Refused while the GM is still writing a reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        record(db, campaign_id, DebugInjectionCleared())
        db.commit()


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
    player_id: int | None = None
    player_name: str = ""


def list_messages(engine: Engine, campaign_id: int) -> list[ChatMessage]:
    with session_scope(engine) as db:
        rows = events_since(db, campaign_id, channel=Channel.TRANSCRIPT)
        names, owner_name = _chat_names(db, campaign_id, rows)
        return [_chat_message(row, names, owner_name) for row in rows]


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
        transcript_rows = events_since(db, campaign_id, channel=Channel.TRANSCRIPT)
        names, owner_name = _chat_names(db, campaign_id, transcript_rows)
        items: list[ChatMessage | LogLine] = [
            _chat_message(row, names, owner_name) for row in transcript_rows
        ]
        for row in events_since(db, campaign_id, channel=Channel.LOG):
            event = decode(row)
            if isinstance(event, TurnFailed) and (row.id or 0) < progress:
                continue
            if (text := event.render()) is not None:
                items.append(LogLine(id=row.id or 0, content=text, hidden=is_hidden(row.kind)))
    return sorted(items, key=lambda item: item.id)


def _chat_names(db: Any, campaign_id: int, rows: Sequence[Event]) -> tuple[dict[int, str], str]:
    """Current display names for the accounts that wrote the chat, keyed by account id. The
    event payload stores only the id, so a name change reaches old messages too."""
    owner_id = db.exec(select(Campaign.owner_id).where(Campaign.id == campaign_id)).one_or_none()
    player_ids = {owner_id} if owner_id is not None else set()
    for row in rows:
        event = decode(row)
        if isinstance(event, ChatPlayer) and event.player_id is not None:
            player_ids.add(event.player_id)
    if not player_ids:
        return {}, ""
    users = db.exec(select(User).where(User.id.in_(player_ids))).all()
    names = {user.id or 0: public_name(user) for user in users}
    return names, names.get(owner_id or 0, "")


def _chat_message(row: Event, names: dict[int, str], owner_name: str) -> ChatMessage:
    event = decode(row)
    if isinstance(event, ChatPlayer):
        player_name = owner_name if event.player_id is None else names.get(event.player_id, "")
        return ChatMessage(
            id=row.id or 0,
            role="user",
            content=event.content,
            speaker=event.speaker,
            player_id=event.player_id,
            player_name=player_name,
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


def tool_definition_parts(
    rule_set: RuleSet,
    mode: str = "brief",
    include_groups: set[str] | None = None,
    load_tools: bool = False,
) -> list[ContextPart]:
    """Tool definitions are sent beside the messages, so they are recorded as their own parts.
    The parts use the same wire projection the provider receives."""
    specs = (
        wire_tool_specs(
            rule_set.tool_calls,
            mode=mode,
            include_groups=include_groups,
            load_tools=load_tools,
        )
        or []
    )
    return [
        ContextPart(
            "tool definition",
            f"Tool '{spec['function']['name']}' from rule set '{rule_set.id}'",
            json.dumps(spec["function"], separators=(",", ":")),
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
    # `used` is the sum. The split lets the UI explain a large fixed overhead, especially the
    # tool definitions that are sent beside the messages.
    fixed: int = 0
    tools: int = 0
    messages: int = 0

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
    world: WorldTemplate | None = None,
    tool_schema_mode: str = "brief",
    rule_info_mode: str = "brief",
) -> ContextUsage:
    unsummarized = [m for m in history if m.id > state.through]
    all_rules = {
        **(rule_set.rules if rule_set else {}),
        **(rule_set.attached_rules if rule_set else {}),
        **world_rules,
    }
    system = system_parts(
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
            tallies=rule_set.tallies if rule_set else None,
            track_inventory=campaign.use_inventory_space,
            currency=(rule_set.marketplace.currency if rule_set and rule_set.marketplace else ""),
        ),
        present_rules_text(state, all_rules),
        campaign.setting,
        rule_info_mode=rule_info_mode,
    )
    system_tokens = sum(part.tokens for part in system)
    instruction_tokens = (
        estimate_tokens(state_instruction(rule_set, world)) if rule_set is not None else 0
    )
    groups = state_tool_groups(state, "") if rule_set is not None else set()
    tool_tokens = (
        sum(
            estimate_tokens(json.dumps(spec, separators=(",", ":")))
            for spec in wire_tool_specs(
                rule_set.tool_calls,
                mode=tool_schema_mode,
                include_groups=groups,
                load_tools=True,
            )
            or []
        )
        if rule_set is not None
        else 0
    )
    message_tokens = sum(estimate_tokens(m.content) for m in unsummarized)
    fixed = system_tokens + instruction_tokens
    used = fixed + tool_tokens + message_tokens
    return ContextUsage(
        used=used,
        limit=limit,
        fixed=fixed,
        tools=tool_tokens,
        messages=message_tokens,
    )


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


from cygnusloom.services.turns import (  # noqa: F401
    Delta,
    Logged,
    LogLine,
    NextMessage,
    PendingView,
    Prompt,
    Retract,
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
