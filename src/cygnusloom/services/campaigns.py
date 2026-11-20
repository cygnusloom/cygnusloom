from __future__ import annotations

import difflib
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
from cygnusloom.content.character import (
    CampaignSpec,
    CharacterError,
    CharacterSpec,
    check_choices,
    check_swaps,
    resolve,
    roll_stats,
)
from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import (
    ContentLibrary,
    RuleSet,
    WorldTemplate,
    read_table_genres,
    read_table_tones,
)
from cygnusloom.db.models import Campaign, CampaignInvite, CampaignMember, Event, User
from cygnusloom.llm.base import (
    LLMError,
    Provider,
    complete,
)
from cygnusloom.services import character_fields as character_field_service
from cygnusloom.services import combat, encounters
from cygnusloom.services import credits as credit_service
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.accounts import public_name
from cygnusloom.services.common import Conflict, InvalidInput, NotFound, session_scope
from cygnusloom.services.events import (
    CHARS_PER_TOKEN,
    PC_KIND,
    CampaignState,
    Channel,
    CharacterRespecced,
    ChatGM,
    ChatGMEdited,
    ChatPlayer,
    ContextPart,
    DebugInjectionCleared,
    DebugInjectionSet,
    NpcRemoved,
    OpeningStarted,
    TurnFailed,
    decode,
    estimate_tokens,
    events_since,
    is_hidden,
    load_state,
    name_key,
    record,
    truncate,
)
from cygnusloom.services.names import next_campaign_label
from cygnusloom.services.prompts import (
    NAME_CHECK_PROMPT,
    SUMMARY_HEADING,
    checklist_areas,
    state_instruction,
    summary_messages,
    summary_shorten_messages,
    system_parts,
    turn_checklist,
)
from cygnusloom.services.tools import (
    FINISH_STATE_STEP,
    LOAD_TOOL,
    checklist_tool_spec,
    state_tool_groups,
    tool_group_names,
    wire_tool_specs,
)
from cygnusloom.services.views import present_rules_text, world_state_text
from cygnusloom.services.world import (
    create_party_hireling,
    create_player_character,
    promote_hireling,
    seed_campaign,
    set_campaign_options,
)
from cygnusloom.services.world_support import _check_sheet_text

MAX_NAME = 120
MAX_MESSAGE = 8000
# A model id is `provider/model`, matching the campaign column.
MAX_MODEL = 120


class NotPlayable(Conflict):
    """A campaign from another major version. Its row and log stay intact and exportable."""


# The campaign-wide settings the AI GM may change during a turn, and only on the owner's
# explicit request.
TURN_SETTINGS = {"inventory_space", "survival_needs"}
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


def _campaign_table_genres(
    table_genres: Sequence[str] | None, rule_set: RuleSet
) -> tuple[str, ...]:
    """The categories a new campaign's generation tables and modules are filtered by. Leaving
    the choice empty keeps the rule set's own selection; every named category must be known."""
    if not table_genres:
        return rule_set.table_genres
    try:
        return read_table_genres(list(table_genres), "creative category")
    except ContentError as error:
        raise InvalidInput(str(error)) from error


def _campaign_table_tones(table_tones: Sequence[str] | None, rule_set: RuleSet) -> tuple[str, ...]:
    """The tones a new campaign's mini modules are filtered by. Leaving the choice empty keeps
    the rule set's own selection; every named tone must be one the engine knows."""
    if not table_tones:
        return rule_set.table_tones
    try:
        return read_table_tones(list(table_tones), "module tone")
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
    table_tones: Sequence[str] | None = None,
    rng: random.Random | None = None,
) -> Campaign:
    name = (name or "").strip()
    if len(name) > MAX_NAME:
        raise InvalidInput(f"label must be 1 to {MAX_NAME} characters")
    if rule_set not in library.rule_sets:
        raise InvalidInput("that rule set is not available")
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    module = library.modules.get(world_template)
    if module is not None:
        if module.kind != "campaign" or module.world_template is None:
            raise InvalidInput("that is a mini module, not a campaign module")
        if module.rule_set is not None and module.rule_set != rule_set:
            raise InvalidInput(
                f"that module only works with the "
                f"{library.rule_sets[module.rule_set].name} rule set"
            )
        if module.recommended_rule_sets and rule_set not in module.recommended_rule_sets:
            raise InvalidInput("that module is not built for that rule set")
        template = module.world_template
    else:
        if world_template not in library.world_templates:
            raise InvalidInput("that world template is not available")
        template = library.world_templates[world_template]
    if template.rule_set is not None and rule_set != template.rule_set:
        raise InvalidInput(
            f"that world only works with the {library.rule_sets[template.rule_set].name} rule set"
        )
    genres = _campaign_table_genres(table_genres, library.rule_sets[rule_set])
    tones = _campaign_table_tones(table_tones, library.rule_sets[rule_set])
    try:
        content = library.world_content_for(template, rule_set)
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
            table_tones=json.dumps(list(tones)),
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
        if not characters:
            continue
        # Spread the NPCs across the map instead of filling the first row. Each character gets
        # its own slice of the row-major cell order, starting in the middle of that slice.
        cells = [(column, row) for row in range(entry.rows) for column in range(entry.columns)]
        for index, character in enumerate(characters):
            start = (2 * index + 1) * len(cells) // (2 * len(characters))
            for offset in range(len(cells)):
                column, row = cells[(start + offset) % len(cells)]
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
                        quiet=True,
                    )
                except InvalidInput:
                    continue
                occupants.add((column, row))
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
    table_tones: Sequence[str] | None = None,
    use_inventory_space: bool | None = None,
    use_survival_needs: bool | None = None,
    start_level: int | None = None,
    follow_owner_level: bool | None = None,
    allow_respec: bool | None = None,
    defer_opening: bool = False,
) -> None:
    """Save the campaign options, the player character, the setting and story, the table
    genres, and the starting level together, so either all of them are saved or none is. A part
    left as None is not saved. The character needs the options first. The setting, genres,
    starting level, follow switch, and respec option are captured on the campaign-options
    creation page, not when the campaign is first made. `defer_opening` leaves the first
    character's opening turn unrecorded, so the table can still build its starting party and
    start play with the party in place."""
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
    tones = None
    if table_tones is not None:
        rule_set = library.rule_sets.get(campaign.rule_set)
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        tones = _campaign_table_tones(table_tones, rule_set)
    with _exclusive(campaign_id), session_scope(engine) as db:
        state = load_state(db, campaign_id)
        # Only the campaign's first character sets the shared options, setting, and genres. A
        # player joining a campaign already under way makes their character and nothing else.
        first_character = not state.pcs()
        stored = db.get(Campaign, campaign_id)
        if first_character and stored is not None:
            if brief is not None:
                stored.setting = brief
            if genres is not None:
                stored.table_genres = json.dumps(list(genres))
            if tones is not None:
                stored.table_tones = json.dumps(list(tones))
            if use_inventory_space is not None:
                stored.use_inventory_space = use_inventory_space
            if use_survival_needs is not None:
                stored.use_survival_needs = use_survival_needs
            if start_level is not None:
                stored.start_level = start_level
            if follow_owner_level is not None:
                stored.follow_owner_level = follow_owner_level
            if allow_respec is not None:
                stored.allow_respec = allow_respec
        if options is not None:
            set_campaign_options(db, campaign_id, spec, options)
        if character is not None:
            rule_set = library.rule_sets[campaign.rule_set]
            # The starting level is a campaign option: every new character begins there, not
            # only the campaign's first one. While the campaign follows the owner's level, a
            # character joining later starts at the level the owner's own character has reached.
            starting_level = campaign.start_level
            if first_character:
                if start_level is not None:
                    starting_level = start_level
            elif campaign.follow_owner_level:
                owner_level = level_up_service.owner_pc_level(state, rule_set)
                if owner_level is not None:
                    starting_level = owner_level
                    if stored is not None:
                        stored.start_level = owner_level
            level_rules = level_up_service.start_level_rules(rule_set)
            if level_rules is not None:
                level_up_service.check_start_level(rule_set, starting_level)
            previous = state.player_character(owner_id)
            # A replacement for this player's out-of-play character gets an opening turn again.
            # A player joining a campaign already under way does not; the GM meets them on the
            # next turn through the world state. The first character's opening waits while the
            # player builds the rest of the starting party. The opening is recorded after the
            # character is complete, so everything before it is the built starting party that a
            # campaign reset returns to.
            opening = first_character or previous is not None
            if first_character and defer_opening:
                opening = False
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
                opening=False,
            )
            if starting_level > 1 and level_rules is not None:
                level_up_service.grant_start_level(
                    db, campaign_id, rule_set, character.name, starting_level
                )
            if opening:
                record(db, campaign_id, OpeningStarted(player_id=owner_id))
        db.commit()


def _require_party_setup(state: CampaignState) -> None:
    """Party members may only be added or removed before play begins and while nothing is
    running. A session that has started uses the game's own hireling tools instead."""
    if state.opened:
        raise InvalidInput("the game has already begun; change the party through play")
    if state.turn is not None:
        raise InvalidInput("the party waits until the current turn is finished")
    if state.encounter is not None:
        raise InvalidInput("the party waits until the encounter is over")


def save_party_hireling(
    engine: Engine,
    library: ContentLibrary,
    player_id: int,
    campaign_id: int,
    *,
    character: NewCharacter,
    wage: int = 0,
) -> int:
    """Add a hireling to the player's starting party from the creation form. The hireling is
    built from the same options as the player character and receives its chosen starting kit.
    Returns the level the hireling begins at, which the advancement pages take up from there."""
    campaign = get_campaign(engine, player_id, campaign_id)
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        raise InvalidInput("that campaign's rule set is no longer available")
    spec = character_spec(library, campaign)
    with _exclusive(campaign_id), session_scope(engine) as db:
        state = load_state(db, campaign_id)
        _require_party_setup(state)
        player_character = state.player_character(player_id)
        if player_character is None or player_character.out_of_play:
            raise InvalidInput("create your player character before adding a hireling")
        # A starting hireling joins at the campaign's starting level, which follows the owner's
        # level while the campaign says so. Using the stored value also covers a party setting
        # out at level 2 before the player has spent the owner's first level-up picks.
        starting_level = campaign.start_level
        level_rules = level_up_service.start_level_rules(rule_set)
        if level_rules is not None:
            level_up_service.check_start_level(rule_set, starting_level)
        created = create_party_hireling(
            db,
            campaign_id,
            library.core.tables,
            random.Random(),
            spec,
            character.name,
            player_character.name,
            character.rolled,
            character.submitted,
            pronouns=character.pronouns,
            plus=rule_set.plus,
            level_ups=rule_set.level_ups,
            inventory=rule_set.inventory,
            item_sizes=rule_set.item_sizes,
            magic=rule_set.magic,
            currency=rule_set.marketplace.currency if rule_set.marketplace else "",
            marketplace=rule_set.marketplace,
            armor_max=rule_set.combat.armor_max if rule_set.combat else 0,
            defense_base=rule_set.combat.defense_base if rule_set.combat else 0,
            defense_modifier=rule_set.combat.defense_modifier if rule_set.combat else "",
            combat_rules=rule_set.combat,
            wage=wage,
        )
        if starting_level > 1 and level_rules is not None:
            level_up_service.grant_start_level(
                db, campaign_id, rule_set, created["name"], starting_level
            )
        db.commit()
        return starting_level


def remove_party_hireling(engine: Engine, player_id: int, campaign_id: int, name: str) -> str:
    """Remove a hireling from the starting party before play begins. The character is deleted,
    along with the gear they carry, so the slot can be built again."""
    get_campaign(engine, player_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        state = load_state(db, campaign_id)
        _require_party_setup(state)
        character = state.characters.get(name_key(name))
        if character is None:
            raise InvalidInput(f"there is no character named {name!r}")
        if not character.hireling:
            raise InvalidInput("only a hireling can be removed from the party")
        player_character = state.player_character(player_id)
        if player_character is None or name_key(character.employer) != name_key(
            player_character.name
        ):
            raise InvalidInput("that hireling is not in your party")
        record(db, campaign_id, NpcRemoved(name=character.name))
        db.commit()
        return character.name


def start_campaign(engine: Engine, player_id: int, campaign_id: int) -> None:
    """Begin play: record the opening turn for this player once the starting party is ready.
    Repeating the request while the campaign has not opened is harmless."""
    get_campaign(engine, player_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        state = load_state(db, campaign_id)
        if state.opened:
            return
        if state.turn is not None:
            raise InvalidInput("a turn is already running")
        player_character = state.player_character(player_id)
        if player_character is None or player_character.out_of_play:
            raise InvalidInput("create your player character before starting the game")
        record(db, campaign_id, OpeningStarted(player_id=player_id))
        db.commit()


def respec_character(
    engine: Engine,
    library: ContentLibrary,
    player_id: int,
    campaign_id: int,
    *,
    name: str,
    rolled: Mapping[str, int],
    submitted: Mapping[str, Any],
) -> int:
    """Rebuild a player character's or hireling's sheet from scratch, under the campaign's respec
    option. The character keeps their name, pronouns, description, flavor details, gear, and place
    in the story. Stats, templates, skills, features, counters, and mechanical fields are replaced,
    so levels, experience, and advancement picks are refunded and the player chooses them again.
    The starting kit is not handed over a second time. Returns the level the character begins at,
    which the normal advancement pages take up from there."""
    campaign = get_campaign(engine, player_id, campaign_id)
    if not campaign.allow_respec:
        raise InvalidInput("this campaign does not allow respeccing")
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        raise InvalidInput("that campaign's rule set is no longer available")
    spec = rule_set.character
    with _exclusive(campaign_id), session_scope(engine) as db:
        state = load_state(db, campaign_id)
        target = state.characters.get(name_key(name))
        if target is None or not character_field_service.controls_character(
            state, player_id, target
        ):
            raise InvalidInput("only a character you play can be respecced")
        if target.kind != PC_KIND or target.out_of_play:
            raise InvalidInput(f"{target.name} is not in play")
        if state.turn is not None:
            raise InvalidInput("a respec waits until the GM's reply is finished")
        if state.encounter is not None:
            raise InvalidInput("a respec waits until the encounter is over")
        chosen = submitted.get("stats", {})
        rolled_stats = {
            stat.id for stat in spec.stats.values() if stat.derived is None and not stat.hidden
        }
        if set(chosen) != rolled_stats:
            raise InvalidInput("every stat needs a value")
        # The form showed only the mechanical fields. The character's own words stay as they
        # were, and so does a mechanical text field the player left alone, such as a College the
        # GM recorded during play.
        given_fields = dict(submitted.get("fields", {}))
        for field in spec.fields.values():
            old = target.fields.get(field.id)
            if old is None:
                continue
            if field.flavor or (field.table is None and not given_fields.get(field.id)):
                given_fields[field.id] = old
        rebuilt_submission = dict(submitted)
        rebuilt_submission["fields"] = given_fields
        try:
            check_swaps(spec, rolled, chosen)
            check_choices(spec, rebuilt_submission)
            sheet = resolve(spec, rebuilt_submission, random.Random(), plus=rule_set.plus)
        except CharacterError as error:
            raise InvalidInput(str(error)) from error
        _check_sheet_text(sheet)
        # A field the rule set no longer defines is kept too: it may be a story label.
        fields = dict(sheet.fields)
        for field_id, value in target.fields.items():
            if field_id not in spec.fields and value:
                fields[field_id] = value
        # The character keeps the gear they carry, and no starting kit is handed out again. Gear
        # carried as inventory items is untouched; coin and any item a rule set keeps as a
        # feature count are carried over here.
        features = {
            feature_id: count
            for feature_id, count in sheet.features.items()
            if not _carried_feature(spec, rule_set.magic, feature_id)
        }
        for feature_id, count in target.features.items():
            if feature_id not in spec.features or _carried_feature(
                spec, rule_set.magic, feature_id
            ):
                features[feature_id] = features.get(feature_id, 0) + count
        # The level the player rebuilds to is the table's level now, before the refund resets
        # the owner's own character. A campaign that follows its owner keeps them at the party's
        # level while they take each level-up again.
        starting_level = campaign.start_level
        if campaign.follow_owner_level:
            owner_level = level_up_service.owner_pc_level(state, rule_set)
            if owner_level is not None:
                starting_level = owner_level
        record(
            db,
            campaign_id,
            CharacterRespecced(
                name=target.name,
                stats=dict(sheet.stats),
                templates=dict(sheet.templates),
                features=features,
                items=dict(target.template_items),
                fields=fields,
                counters=dict(sheet.counters),
                skills=dict(sheet.skills),
                class_levels=dict(sheet.class_levels),
                hit_dice_pools={},
                feature_choices={
                    key: list(values) for key, values in sheet.feature_choices.items()
                },
                stat_modifiers=dict(sheet.stat_modifiers),
            ),
        )
        rebuilt = load_state(db, campaign_id).characters[name_key(target.name)]
        if rule_set.combat is not None and rule_set.combat.defense_base:
            # The new sheet's own armor effects replace the old ones; the gear stays equipped.
            combat.set_defense_equipment(
                db,
                campaign_id,
                rebuilt,
                spec,
                rule_set.combat,
                adjustment=sheet.armor,
            )
        if level_up_service.start_level_rules(rule_set) is None:
            starting_level = 1
        else:
            level_up_service.check_start_level(rule_set, starting_level)
            if starting_level > 1:
                level_up_service.grant_start_level(
                    db, campaign_id, rule_set, target.name, starting_level
                )
        db.commit()
        return starting_level


def _carried_feature(spec: CharacterSpec, magic: Any, feature_id: str) -> bool:
    """Whether the rule set carries this feature as gear rather than keeping it on the sheet: an
    inventory item, or a spell the rule set gives as an item. Those belong to the character, not
    to the build being refunded."""
    feature = spec.features.get(feature_id)
    if feature is None:
        return False
    return feature.feature_type == "spell" and magic is not None and magic.item is not None


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


def reset_campaign(
    engine: Engine, library: ContentLibrary, owner_id: int, campaign_id: int
) -> None:
    """Start the campaign over from just before its opening. Everything recorded while the
    table was being set up stays, including the world, the campaign options, and the starting
    party with their sheets and gear. The opening turn and everything played since are removed,
    so the party can be changed and the game started again from a fresh opening. Credits charged
    for the removed replies are returned. Another account's campaign looks the same as a missing
    one, and the reset is refused while the GM is still writing a reply."""
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.owner_id != owner_id:
            raise NotFound("campaign not found")
        # The campaign's own opening marks the end of setup: a replacement character has its own
        # opening later, so the first one is the beginning the table returns to.
        opening = db.exec(
            select(Event.id)
            .where(Event.campaign_id == campaign_id, Event.kind == OpeningStarted.kind)
            .order_by(Event.seq)
            .limit(1)
        ).first()
        if opening is None:
            raise InvalidInput("this campaign has not started yet; there is nothing to reset")
        removed = db.exec(
            select(Event.id).where(
                Event.campaign_id == campaign_id,
                Event.id >= opening,
                Event.kind == ChatGM.kind,
            )
        ).all()
        credit_service.refund_replies(db, removed)
        truncate(db, campaign_id, opening - 1)
        _restore_starting_level(db, campaign_id, campaign.start_level, campaign.rule_set, library)
        db.commit()


def _restore_starting_level(
    db: Any, campaign_id: int, start_level: int, rule_set_id: str, library: ContentLibrary
) -> None:
    """Give the starting party back the experience a starting level above 1 implies. An older
    log recorded that grant inside the opening turn, so the reset removed it. Granting it again
    is a no-op when the characters kept it. A rule set that has been removed, or a level it can
    no longer grant, leaves the party as the log reads."""
    if start_level <= 1:
        return
    rule_set = library.rule_sets.get(rule_set_id)
    if rule_set is None or level_up_service.start_level_rules(rule_set) is None:
        return
    for character in load_state(db, campaign_id).characters.values():
        if character.kind != PC_KIND:
            continue
        try:
            level_up_service.grant_start_level(
                db, campaign_id, rule_set, character.name, start_level
            )
        except InvalidInput:
            continue


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


def set_campaign_survival_needs(
    engine: Engine, owner_id: int, campaign_id: int, enabled: bool
) -> Campaign:
    """Turn food, water, and rest privation on or off for the campaign. Off means a rest spends
    no rations or water and records no privation. Existing marks end at the next rest."""
    get_owned_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.use_survival_needs = enabled
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
    survival_needs: bool,
    label: str,
    start_level: int | None = None,
    follow_owner_level: bool | None = None,
    setting: str | None = None,
    table_genres: Sequence[str] | None = None,
    table_tones: Sequence[str] | None = None,
    allow_respec: bool | None = None,
    options: Mapping[str, Any] | None = None,
    campaign_options: CampaignSpec | None = None,
    rule_set: RuleSet | None = None,
) -> Campaign:
    """Save the owner's campaign settings in one write: the model, the campaign's options and
    brief, the inventory, survival-needs, and respec switches, the starting level for new
    characters and its follow switch, and the label. The model and switches are configuration,
    not part of the log, so they may change mid-game. The rule set's own options are recorded
    as an event, so the GM reads the newest values from the next reply on."""
    if model not in allowed_models:
        raise InvalidInput("that model is not available")
    label = (label or "").strip()
    if not label or len(label) > MAX_NAME:
        raise InvalidInput(f"label must be 1 to {MAX_NAME} characters")
    brief = None
    if setting is not None:
        brief = (setting or "").strip()
        if len(brief) > MAX_SETTING:
            raise InvalidInput(f"setting and story must be at most {MAX_SETTING} characters")
    genres: tuple[str, ...] | None = None
    if table_genres is not None:
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        genres = _campaign_table_genres(table_genres, rule_set)
    tones: tuple[str, ...] | None = None
    if table_tones is not None:
        if rule_set is None:
            raise InvalidInput("that campaign's rule set is no longer available")
        tones = _campaign_table_tones(table_tones, rule_set)
    get_owned_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        if genres is not None:
            # An empty choice means the rule set's own genres, and storing nothing keeps the
            # settings page showing those as checked.
            same_as_rule_set = rule_set is not None and genres == rule_set.table_genres
            campaign.table_genres = (
                "" if not genres or same_as_rule_set else json.dumps(list(genres))
            )
        if tones is not None:
            same_as_rule_set = rule_set is not None and tones == rule_set.table_tones
            campaign.table_tones = "" if not tones or same_as_rule_set else json.dumps(list(tones))
        campaign.model = model
        campaign.use_inventory_space = inventory_space
        campaign.use_survival_needs = survival_needs
        if allow_respec is not None:
            campaign.allow_respec = allow_respec
        if start_level is not None:
            campaign.start_level = start_level
        if follow_owner_level is not None:
            campaign.follow_owner_level = follow_owner_level
        if brief is not None:
            campaign.setting = brief
        campaign.name = label
        if options is not None:
            if campaign_options is None:
                raise InvalidInput("that campaign's rule set is no longer available")
            set_campaign_options(db, campaign_id, campaign_options, options, replace=True)
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
        if setting == "survival_needs":
            campaign.use_survival_needs = value
            db.add(campaign)
            db.commit()
            detail = (
                "Food, water, and rest needs are tracked again."
                if value
                else "Food, water, and rest needs are no longer tracked."
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
    engine: Engine, owner_id: int, campaign_id: int, name: str, item: str, amount: int
) -> dict[str, Any]:
    """Give a player character some of the rule set's currency from the debug panel. Refused while
    the GM is still writing a reply."""
    get_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        return combat.add_debug_currency(db, campaign_id, name, item, amount)


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
        edits = _reply_edits(rows)
        return [
            _chat_message(row, names, owner_name, edits)
            for row in rows
            if row.kind != ChatGMEdited.kind
        ]


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
        edits = _reply_edits(transcript_rows)
        items: list[ChatMessage | LogLine] = [
            _chat_message(row, names, owner_name, edits)
            for row in transcript_rows
            if row.kind != ChatGMEdited.kind
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


def _reply_edits(rows: Sequence[Event]) -> dict[int, str]:
    """The current text of every edited GM reply, keyed by the reply's event id. The rows are in
    order, so the newest edit of a reply wins."""
    edits: dict[int, str] = {}
    for row in rows:
        if row.kind != ChatGMEdited.kind:
            continue
        event = decode(row)
        assert isinstance(event, ChatGMEdited)
        edits[event.reply_id] = event.content
    return edits


def _chat_message(
    row: Event,
    names: dict[int, str],
    owner_name: str,
    edits: Mapping[int, str],
) -> ChatMessage:
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
        edited = (row.id or 0) in edits
        return ChatMessage(
            id=row.id or 0,
            role="assistant",
            content=edits[row.id or 0] if edited else event.content,
            model=event.model,
            # Stale reasoning explains the original text, so an edited reply drops it.
            reasoning="" if edited else event.reasoning,
        )
    raise TypeError(f"{row.kind} is not a chat event")


def tool_definition_parts(
    rule_set: RuleSet,
    mode: str = "brief",
    include_groups: set[str] | None = None,
    load_tools: bool = False,
    checklist: Mapping[str, str] | None = None,
) -> list[ContextPart]:
    """Tool definitions are sent beside the messages, so they are recorded as their own parts.
    The parts use the same wire projection the provider receives. `checklist` adds the engine's
    finish tool for a turn that has a checklist, with one argument per area."""
    specs = (
        wire_tool_specs(
            rule_set.tool_calls,
            mode=mode,
            include_groups=include_groups,
            load_tools=load_tools,
        )
        or []
    )
    if checklist:
        specs = [*specs, checklist_tool_spec(checklist)]
    return [
        ContextPart(
            "tool definition",
            f"Tool '{spec['function']['name']}' from "
            + (
                "the engine"
                if spec["function"]["name"] in (LOAD_TOOL, FINISH_STATE_STEP)
                else f"rule set '{rule_set.id}'"
            ),
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
    checklist = (
        turn_checklist(rule_set.checklist, tool_group_names(rule_set.tool_calls))[0]
        if rule_set is not None
        else ""
    )
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
            track_survival_needs=campaign.use_survival_needs,
            currency=(rule_set.marketplace.currency if rule_set and rule_set.marketplace else ""),
            housekeeping=rule_set.housekeeping if rule_set else None,
            character=rule_set.character if rule_set else None,
            monsters=rule_set.monsters if rule_set else None,
        ),
        present_rules_text(state, all_rules),
        campaign.setting,
        rule_info_mode=rule_info_mode,
        checklist=checklist,
    )
    system_tokens = sum(part.tokens for part in system)
    instruction_tokens = (
        estimate_tokens(state_instruction(rule_set, world)) if rule_set is not None else 0
    )
    groups = state_tool_groups(state, "") if rule_set is not None else set()
    checklist_labels = (
        {
            area.id: area.text
            for area in checklist_areas(rule_set.checklist, tool_group_names(rule_set.tool_calls))
        }
        if rule_set is not None
        else {}
    )
    specs = (
        wire_tool_specs(
            rule_set.tool_calls,
            mode=tool_schema_mode,
            include_groups=groups,
            load_tools=True,
        )
        or []
        if rule_set is not None
        else []
    )
    if checklist_labels:
        specs = [*specs, checklist_tool_spec(checklist_labels)]
    tool_tokens = sum(estimate_tokens(json.dumps(spec, separators=(",", ":"))) for spec in specs)
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
    """Keep the newest text that fits the budget, starting at a paragraph, sentence, or word
    boundary, so the kept memory never begins in the middle of a sentence."""
    limit = budget_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    kept = text[-limit:]
    window = max(120, limit // 3)
    for marker, width in (("\n\n", 2), (". ", 2), ("! ", 2), ("? ", 2), ("\n", 1), (" ", 1)):
        index = kept.find(marker)
        if index != -1 and index <= window:
            return kept[index + width :]
    index = kept.find(" ")
    return kept[index + 1 :] if index != -1 else kept


# Words that carry no identity in a place name. Leaving them out lets "The Old Mill" and
# "Mill" match without asking the model.
_PLACE_NOISE = frozenset(
    {"the", "a", "an", "of", "at", "in", "on", "by", "to", "and", "old", "new", "little", "great"}
)


def _place_words(name: str) -> tuple[str, ...]:
    return tuple(
        word for word in re.findall(r"[a-z0-9]+", name.casefold()) if word not in _PLACE_NOISE
    )


def _similar_place_word(first: str, second: str) -> bool:
    """Whether two name words could be the same word: equal, one a stem of the other, or a
    spelling that differs by a letter or two."""
    if first == second:
        return True
    if (
        len(first) >= 4
        and len(second) >= 4
        and (first.startswith(second) or second.startswith(first))
    ):
        return True
    return difflib.SequenceMatcher(None, first, second).ratio() >= 0.8


def _place_may_repeat(name: str, existing: str) -> bool:
    """Whether two place names are close enough that the model should judge them. A similar
    word or one name inside the other is the only way two ordinary names are the same place."""
    words = _place_words(name)
    other = _place_words(existing)
    if not words or not other:
        return True
    if any(_similar_place_word(word, candidate) for word in words for candidate in other):
        return True
    return " ".join(words) in " ".join(other) or " ".join(other) in " ".join(words)


async def _repeated_place(
    provider: Provider, model_name: str, names: Sequence[str], name: str
) -> str | None:
    """The existing place that `name` repeats, or None. A name with no words in common with any
    existing place is clearly new, so it is answered without a model call. Otherwise the model
    judges the wording, so a name that repeats a place in other words is caught too."""
    if not names:
        return None
    folded = " ".join(_place_words(name))
    for existing in names:
        if folded and folded == " ".join(_place_words(existing)):
            return existing
    candidates = [existing for existing in names if _place_may_repeat(name, existing)]
    if not candidates:
        return None
    listing = "\n".join(f"- {existing}" for existing in candidates)
    messages = [
        {"role": "system", "content": NAME_CHECK_PROMPT},
        {"role": "user", "content": f"Existing places:\n{listing}\n\nNew place: {name}"},
    ]
    reply = (await complete(provider, model_name, messages)).strip()
    if not reply.upper().startswith("REPEAT:"):
        return None
    named = reply.split(":", 1)[1].strip().casefold()
    return next((existing for existing in candidates if existing.casefold() == named), None)


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
    limit = budget_tokens * CHARS_PER_TOKEN
    for _attempt in range(2):
        if len(summary) <= limit:
            break
        # Cutting the text would leave a fragment of the memory. Ask for a whole compact
        # summary instead, and keep the cut only as a last resort.
        shorten = summary_shorten_messages(summary, max(1, int(limit * 0.8)))
        shortened = (await complete(provider, model_name, shorten)).strip()
        parts.extend(
            [
                ContextPart(
                    "user", "Summary shortening request", "\n\n".join(m["content"] for m in shorten)
                ),
                ContextPart("assistant", "Summary shortening reply", shortened),
            ]
        )
        if shortened.startswith(SUMMARY_HEADING):
            shortened = shortened[len(SUMMARY_HEADING) :].strip()
        if not shortened:
            break
        summary = shortened
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
    campaign_log,
    discard_pending,
    edit_last_reply,
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
