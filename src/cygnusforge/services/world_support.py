"""Places, characters, and notes: the world that a campaign's GM edits.

Writes check the campaign's current state, record events, and commit. Reads work on a state that
is already folded from the log. Problems the model can fix are raised as `InvalidInput`, and their
messages are returned to the model as they are."""

from __future__ import annotations

import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from difflib import get_close_matches
from typing import Any, TypeVar

from sqlmodel import Session

from cygnusforge.content.character import (
    CampaignSpec,
    CharacterError,
    CharacterSpec,
    Feature,
    Sheet,
    TableOption,
    campaign_schema,
    check_choices,
    check_swaps,
    resolve,
)
from cygnusforge.content.library import (
    ClockRules,
    CombatRules,
    Hooks,
    InventoryRules,
    MagicRules,
    Marketplace,
    MonsterTemplate,
    Travel,
    Wilderness,
    WorldTemplate,
    WorldTerrain,
)
from cygnusforge.rules.context import brief
from cygnusforge.rules.dice import is_formula
from cygnusforge.services import clock as clock_service
from cygnusforge.services import combat
from cygnusforge.services import inventory as inventory_service
from cygnusforge.services import market as market_service
from cygnusforge.services import wilderness as wilderness_service
from cygnusforge.services.attacks import ATTACK_FLAGS
from cygnusforge.services.common import (
    InvalidInput,
    bounded_text,
    whole_number,
)
from cygnusforge.services.events import (
    CAMPAIGN_WIDE,
    NPC_KIND,
    PC_KIND,
    ActorMoved,
    ArmorSet,
    CampaignOptionsChosen,
    CampaignState,
    Character,
    HirelingAdded,
    HirelingDismissed,
    HirelingPromoted,
    Item,
    JournalAppended,
    JournalReplaced,
    KnownNpc,
    LocationAdded,
    LocationDiscovered,
    LocationRemoved,
    LocationSummarized,
    LocationUpdated,
    Note,
    NoteAdded,
    NoteRemoved,
    NoteUpdated,
    NpcAdded,
    NpcKnown,
    NpcRemoved,
    NpcStatted,
    NpcUpdated,
    OpeningStarted,
    PcAdded,
    Place,
    RegionAdded,
    SeasonSet,
    StartingItemsTransferred,
    TravelCharged,
    load_state,
    name_key,
    record,
)
from cygnusforge.services.geometry import clearance, overlaps, polygon_problem, uncovered
from cygnusforge.services.tools import check_arguments
from cygnusforge.services.views import (
    _character_named,
    _children_of,
    _flag_text,
    _location_entry,
    _npc_summary,
    _npc_words,
    _npcs_at,
    _place_named,
)

T = TypeVar("T")
MAX_NAME = 120
MAX_STAT = 999
MAX_TEXT = 2000
# A player-facing summary of a place: a sentence or two, not the place's full description.
MAX_SUMMARY = 300
MAX_QUIRKS = 10
QUIRKS_PER_NPC = 2
# A region outline may have at most this many corners.
MAX_REGION_CORNERS = 200
# A place given a map point must lie this far inside a region's edge, so the ground around it shows.
REGION_BUFFER_KM = 0.5
# Two places count as sharing a point when their positions agree to this many kilometers.
SAME_POINT_KM = 0.01


# How a player character moves: overland takes wilderness time, in a settlement takes minutes at a
# speed, and in a dungeon takes no clock time.
OVERLAND = "overland"
IN_SETTLEMENT = "in_settlement"
IN_DUNGEON = "in_dungeon"
MOVE_MODES = (OVERLAND, IN_SETTLEMENT, IN_DUNGEON)


def seed_campaign(
    db: Session,
    campaign_id: int,
    world: WorldTemplate,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    *,
    monsters: Mapping[str, MonsterTemplate],
    plus: str,
    npc_hp: Mapping[str, int],
    npc_hp_stat: str | None,
    wilderness: Wilderness | None = None,
    combat_rules: CombatRules | None = None,
    inventory: InventoryRules | None = None,
    item_sizes: Mapping[str, int] | None = None,
    magic: MagicRules | None = None,
    currency: str = "",
) -> None:
    """Record a campaign's places and the world's NPCs as events. The player character is made
    later, by `create_player_character`. An NPC that names a statblock takes its stats, and an
    NPC with an HP tier and no statblock takes that tier's HP. A rule set with a wilderness
    calendar also records the season the campaign starts in."""
    if wilderness is not None:
        requested = world.start.season if world.start is not None else None
        season = wilderness_service.start_season_for(wilderness, requested)
        record(db, campaign_id, SeasonSet(season=season))
    # A place can be declared before the place it is inside, so every name is known first.
    names = {spot.id: spot.name for spot in world.locations}
    for spot in world.locations:
        record(
            db,
            campaign_id,
            LocationAdded(
                name=spot.name,
                description=spot.description,
                discovered=spot.discovered,
                x=spot.x,
                y=spot.y,
                parent=None if spot.parent is None else names[spot.parent],
                icon=spot.icon,
                rules=list(spot.rules),
                routes={key: dict(route) for key, route in spot.routes.items()},
            ),
        )
    for region in world.regions:
        record(
            db,
            campaign_id,
            RegionAdded(
                name=region.name,
                terrain=region.terrain,
                points=[[x, y] for x, y in region.points],
            ),
        )
    for npc in world.npcs:
        base = None if npc.monster is None else monsters[npc.monster]
        if base is not None:
            character: dict[str, Any] | None = {"stats": dict(base.stats)}
        elif npc.hp_tier is not None and npc_hp_stat is not None:
            character = {"stats": {npc_hp_stat: npc_hp[npc.hp_tier]}}
        else:
            character = None
        event = _npc_event(
            tables,
            rng,
            spec,
            name=npc.name,
            location=names[npc.location],
            description=npc.description,
            personality=npc.personality,
            accent=npc.accent,
            quirks=npc.quirks,
            character=character,
            plus=plus,
            story_only=npc.story_only,
            rules=npc.rules,
        )
        if base is not None:
            event = _with_statblock(event, base)
        record(db, campaign_id, event)
        if base is None and not event.story_only:
            _initialize_generated_defense(
                db,
                campaign_id,
                event.name,
                spec,
                combat_rules,
                inventory,
                item_sizes,
                magic,
                currency,
            )
    db.commit()


def set_campaign_options(
    db: Session, campaign_id: int, campaign: CampaignSpec, submitted: Mapping[str, Any]
) -> dict[str, Any]:
    """Record the campaign's shared options. They are chosen once, so the campaign's omen and
    the like cannot change after the characters are made."""
    if not campaign.fields:
        raise InvalidInput("this campaign has no options to choose")
    state = load_state(db, campaign_id)
    if state.options:
        raise InvalidInput("the campaign options are already chosen")
    problem = check_arguments(campaign_schema(campaign), dict(submitted))
    if problem is not None:
        raise InvalidInput(problem)
    record(db, campaign_id, CampaignOptionsChosen(options=dict(submitted)))
    return {"chosen": "campaign"}


def _starting_item(
    feature: Feature, magic: MagicRules | None, item_sizes: Mapping[str, int]
) -> Item:
    """The inventory item a starting feature becomes: a magic item for a spell, else the item."""
    if feature.feature_type == "spell":
        assert magic is not None
        return Item(
            feature.name,
            magic.reusable.size,
            item_sizes[magic.reusable.size],
            kind=magic.reusable.kind,
            text=feature.description,
        )
    flags = list(feature.flags)
    if feature.armor > 0 and "shield" not in flags and "armor" not in flags:
        flags.append("armor")
    return Item(
        feature.name,
        feature.size,
        item_sizes[feature.size],
        damage=feature.damage,
        rules=feature.rules,
        flags=tuple(flags),
        uses=feature.uses,
        max_uses=feature.uses,
    )


def _take_starting_items(
    spec: CharacterSpec,
    starting: dict[str, int],
    inventory: InventoryRules | None,
    item_sizes: Mapping[str, int] | None,
    magic: MagicRules | None,
    currency: str,
) -> list[tuple[Feature, int]]:
    carried: list[tuple[Feature, int]] = []
    if inventory is not None and item_sizes is not None:
        for feature_id in list(starting):
            feature = spec.features[feature_id]
            if feature_id != currency and (
                feature.feature_type == "item"
                or (feature.feature_type == "spell" and magic is not None)
            ):
                carried.append((feature, starting.pop(feature_id)))
    return carried


def _carry_starting_items(
    db: Session,
    campaign_id: int,
    name: str,
    carried: Sequence[tuple[Feature, int]],
    inventory: InventoryRules | None,
    item_sizes: Mapping[str, int] | None,
    magic: MagicRules | None,
    *,
    defense: bool,
) -> tuple[tuple[str, int], tuple[str, int], int]:
    armor = ("", 0)
    shield = ("", 0)
    armor_bonus = 0
    if carried:
        starting_inventory = _required(inventory, "inventory")
        starting_sizes = _required(item_sizes, "item sizes")
        for feature, count in carried:
            for _ in range(count):
                inventory_service.carry_item(
                    db,
                    campaign_id,
                    starting_inventory,
                    name,
                    _starting_item(feature, magic, starting_sizes),
                    cause="starting gear",
                )
            if defense:
                if "shield" in feature.flags and feature.armor > shield[1]:
                    shield = (feature.name, feature.armor)
                elif "shield" not in feature.flags and feature.armor > armor[1]:
                    armor = (feature.name, feature.armor)
            else:
                armor_bonus += feature.armor * count
    return armor, shield, armor_bonus


def _initialize_generated_defense(
    db: Session,
    campaign_id: int,
    name: str,
    spec: CharacterSpec,
    rules: CombatRules | None,
    inventory: InventoryRules | None,
    item_sizes: Mapping[str, int] | None,
    magic: MagicRules | None,
    currency: str,
) -> None:
    if rules is None or not rules.defense_base:
        return
    character = _character_named(load_state(db, campaign_id), name)
    if character.story_only or character.defense_tracked:
        return
    starting = dict(character.features)
    carried = _take_starting_items(spec, starting, inventory, item_sizes, magic, currency)
    if carried:
        record(
            db,
            campaign_id,
            StartingItemsTransferred(name=character.name, features=[f.id for f, _ in carried]),
        )
    armor, shield, _ = _carry_starting_items(
        db, campaign_id, name, carried, inventory, item_sizes, magic, defense=True
    )
    combat.set_defense_equipment(
        db,
        campaign_id,
        character,
        spec,
        rules,
        armor=armor,
        shield=shield,
        adjustment=character.armor,
    )


def _carry_granted_kit(
    db: Session,
    campaign_id: int,
    name: str,
    sheet: Sheet,
    spec: CharacterSpec,
    marketplace: Marketplace | None,
    inventory: InventoryRules | None,
    item_sizes: Mapping[str, int] | None,
) -> tuple[tuple[str, int], tuple[str, int], int]:
    """Carry what the chosen templates grant: catalogue items, starting money, and the armor and
    shield already worn. Returns the equipped armor and shield the kit gives, and the silver."""
    armor: tuple[str, int] = ("", 0)
    shield: tuple[str, int] = ("", 0)
    silver = 0
    for template_id in sheet.templates.values():
        grant = spec.templates[template_id].grant
        if not (grant.items or grant.equip or grant.silver):
            continue
        if marketplace is None:
            raise InvalidInput("this rule set has no marketplace to grant starting items from")
        rules = _required(inventory, "inventory")
        carried: list[market_service.ShopItem] = []
        for item_name in grant.items:
            listed = market_service.listed_item(marketplace, item_name)
            if listed is None:
                raise InvalidInput(f"there is no catalogue item named {item_name!r}")
            carried.append(listed)
        # Weapons go into the pack first, so a character with few Readied slots still has one at
        # the ready. Worn armor and a wielded shield protect whether or not there was room to
        # ready them.
        for listed in sorted(carried, key=lambda item: 0 if item.damage else 1):
            inventory_service.carry_listed(
                db,
                campaign_id,
                rules,
                _required(item_sizes, "item sizes"),
                name,
                listed,
                cause="starting gear",
                overflow=True,
            )
        for item_name in grant.equip:
            listed = market_service.listed_item(marketplace, item_name)
            if listed is None:
                raise InvalidInput(f"there is no catalogue item named {item_name!r}")
            if listed.armor_mode == "shield":
                shield = (listed.name, listed.armor)
            else:
                armor = (listed.name, listed.armor)
        if grant.silver:
            market_service.change_currency(
                db, campaign_id, marketplace, name, grant.silver, "starting silver"
            )
            silver += grant.silver
    return armor, shield, silver


def create_player_character(
    db: Session,
    campaign_id: int,
    campaign: CampaignSpec,
    spec: CharacterSpec,
    name: str,
    rolled: Mapping[str, int],
    submitted: Mapping[str, Any],
    rng: random.Random,
    inventory: InventoryRules | None = None,
    item_sizes: Mapping[str, int] | None = None,
    magic: MagicRules | None = None,
    currency: str = "",
    armor_max: int = 0,
    defense_base: int = 0,
    defense_modifier: str = "",
    marketplace: Marketplace | None = None,
    *,
    pronouns: str,
    plus: str,
    combat_rules: CombatRules | None = None,
) -> dict[str, Any]:
    """Make the player character from the player's choices. `rolled` are the stats rolled for
    this character; the player may only keep them or swap within a swap group. Starting item
    features are carried as inventory items, spell features as the rule set's magic items, and the armor they
    give is worn. A template that grants a starting kit hands over its items, money, and equipment
    the same way. The currency stays a count. The pronouns are how the GM refers to the character."""
    state = load_state(db, campaign_id)
    current = state.pc()
    if current is not None and not current.dead:
        raise InvalidInput("the player character already exists")
    if campaign.fields and not state.options:
        raise InvalidInput("the campaign options must be chosen before the player character")
    name = bounded_text(name, "name", MAX_NAME)
    pronouns = bounded_text(pronouns, "pronouns", MAX_NAME)
    _ensure_free_name(state, name)
    chosen = submitted.get("stats", {})
    rolled_stats = {s.id for s in spec.stats.values() if s.derived is None}
    if set(chosen) != rolled_stats:
        raise InvalidInput("every stat needs a value")
    try:
        check_swaps(spec, rolled, chosen)
        check_choices(spec, submitted)
        sheet = resolve(spec, submitted, rng, plus=plus)
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    _check_sheet_text(sheet)
    fields = _sheet_fields(sheet)
    starting = fields["features"]
    carried = _take_starting_items(spec, starting, inventory, item_sizes, magic, currency)
    record(db, campaign_id, PcAdded(name=name, pronouns=pronouns, **fields))
    # A rule set whose armor is its defense gives an unarmored character a base AC, such as
    # 10 + Dexterity. Focus and other feature effects add to the armor the carried features give.
    armor = defense_base
    if defense_modifier:
        armor += spec.modifier(sheet.stats, defense_modifier)
    armor += sheet.armor
    equipped_armor, equipped_shield, item_armor = _carry_starting_items(
        db,
        campaign_id,
        name,
        carried,
        inventory,
        item_sizes,
        magic,
        defense=bool(combat_rules is not None and combat_rules.defense_base),
    )
    granted_armor, granted_shield, _ = _carry_granted_kit(
        db, campaign_id, name, sheet, spec, marketplace, inventory, item_sizes
    )
    if granted_armor[0]:
        equipped_armor = granted_armor
    if granted_shield[0]:
        equipped_shield = granted_shield
    armor += item_armor
    if combat_rules is not None and combat_rules.defense_base:
        created = _character_named(load_state(db, campaign_id), name)
        combat.set_defense_equipment(
            db,
            campaign_id,
            created,
            spec,
            combat_rules,
            armor=equipped_armor,
            shield=equipped_shield,
            adjustment=sheet.armor,
        )
    elif armor:
        cap = armor_max or combat.UNCAPPED_ARMOR
        record(db, campaign_id, ArmorSet(name=name, armor=min(armor, cap)))
    record(db, campaign_id, OpeningStarted())
    return {"created": "pc", "name": name, "location": None}


def create_location(
    db: Session,
    campaign_id: int,
    name: str,
    description: str,
    discovered: bool = False,
    x: float | None = None,
    y: float | None = None,
    force: bool = False,
    parent: str | None = None,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    _ensure_no_same_place(state, name)
    text = bounded_text(description, "description", MAX_TEXT)
    if not isinstance(discovered, bool):
        raise InvalidInput("discovered must be true or false")
    px, py = _coordinates(x, y)
    inside = None
    if parent is not None:
        inside = _parent_for_new_place(state, parent, px, py)
        px, py = None, None
    else:
        _ensure_on_terrain(state, name, px, py)
        _ensure_open_point(state, name, px, py, force)
    record(
        db,
        campaign_id,
        LocationAdded(
            name=name, description=text, discovered=discovered, x=px, y=py, parent=inside
        ),
    )
    db.commit()
    return {"created": "location", "name": name, "discovered": discovered}


def _ensure_on_terrain(state: CampaignState, name: str, x: float | None, y: float | None) -> None:
    """A place with a map point must sit inside a region with ground around it, so the map shows
    the terrain under the place rather than blank space."""
    if x is None or y is None:
        return
    ground = [region.points for region in state.regions.values()]
    if clearance((x, y), ground) < REGION_BUFFER_KM:
        raise InvalidInput(
            f"{name!r} is not on the map's terrain. Create a region that covers the place and "
            f"extends at least {REGION_BUFFER_KM} km beyond it, using create_region, then add "
            "the location."
        )


def create_region(
    db: Session,
    campaign_id: int,
    kinds: Mapping[str, WorldTerrain],
    name: str,
    terrain: str,
    points: Sequence[Sequence[float]],
    force: bool = False,
    discovered: bool = False,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    if name_key(name) in state.regions:
        raise InvalidInput(f"there is already a region named {name!r}")
    if terrain not in kinds:
        choices = ", ".join(sorted(kinds)) or "none"
        raise InvalidInput(f"{terrain!r} is not a terrain kind. The terrain kinds are: {choices}.")
    corners = _region_corners(points)
    problem = polygon_problem(corners)
    if problem is not None:
        raise InvalidInput(
            f"the region {problem}. Give its corners in order around the edge, in kilometers "
            "from the map's center."
        )
    if not isinstance(force, bool):
        raise InvalidInput("force must be true or false")
    if not isinstance(discovered, bool):
        raise InvalidInput("discovered must be true or false")
    result: dict[str, Any] = {
        "created": "region",
        "name": name,
        "terrain": terrain,
        "discovered": discovered,
    }
    if not force:
        covering = [region for region in state.regions.values() if overlaps(corners, region.points)]
        if covering:
            # Ground already there is taken away, so the new region covers only open ground.
            cut = uncovered(corners, [region.points for region in state.regions.values()])
            if not cut.parts:
                names = ", ".join(repr(region.name) for region in covering)
                if cut.holed:
                    raise InvalidInput(
                        f"cutting {name!r} to fit the ground already there would leave a hole in "
                        f"it, because {names} lies wholly inside the outline. Draw around that "
                        "ground, or pass force=true to overlap it."
                    )
                raise InvalidInput(
                    f"{name!r} lies entirely on ground that is already covered, by {names}. Give "
                    "an outline that reaches open ground, or pass force=true to overlap it."
                )
            corners = cut.parts[0]
            result["points"] = [[x, y] for x, y in corners]
            left_out = len(cut.parts) - 1 + cut.holed
            if left_out:
                result["left_out"] = left_out
    record(
        db,
        campaign_id,
        RegionAdded(
            name=name,
            terrain=terrain,
            points=[[x, y] for x, y in corners],
            discovered=discovered,
        ),
    )
    db.commit()
    return result


def _region_corners(points: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    if not isinstance(points, list | tuple) or len(points) > MAX_REGION_CORNERS:
        raise InvalidInput(f"points must be a list of at most {MAX_REGION_CORNERS} [x, y] corners")
    corners: list[tuple[float, float]] = []
    for point in points:
        if (
            not isinstance(point, list | tuple)
            or len(point) != 2
            or any(isinstance(value, bool) or not isinstance(value, int | float) for value in point)
        ):
            raise InvalidInput(
                "each point must be an [x, y] pair of numbers, in kilometers from the map's center"
            )
        corners.append((float(point[0]), float(point[1])))
    return corners


def create_npc(
    db: Session,
    campaign_id: int,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    name: str,
    location: str,
    description: str,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    *,
    monsters: Mapping[str, MonsterTemplate],
    force: bool = False,
    plus: str,
    npc_hp: Mapping[str, int],
    npc_hp_stat: str | None,
    hp_tier: str | None = None,
    pronouns: str | None = None,
    important: bool = False,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    where = _place_named(state, location)
    try:
        check_choices(spec, character or {})
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    base = None if force else _statblock_named(monsters, name)
    if base is not None:
        if (character or {}).get("stats"):
            raise InvalidInput(_statblock_conflict(base, spec))
        character = {**(character or {}), "stats": dict(base.stats)}
    if hp_tier is not None:
        character = _with_hp_tier(
            character, hp_tier, npc_hp, npc_hp_stat, statblock=base is not None
        )
    event = _npc_event(
        tables,
        rng,
        spec,
        name=name,
        location=where.name,
        description=bounded_text(description, "description", MAX_TEXT),
        personality=_optional_text(personality, "personality"),
        accent=_optional_text(accent, "accent"),
        quirks=_quirks(quirks),
        character=character,
        plus=plus,
        pronouns=_optional_text(pronouns, "pronouns"),
        important=important,
    )
    if base is not None:
        event = _with_statblock(event, base)
    record(db, campaign_id, event)
    return {
        "created": "npc",
        "name": event.name,
        "location": event.location,
        "personality": event.personality,
        "accent": event.accent,
        "quirks": list(event.quirks),
        "pronouns": event.pronouns,
        "statblock": base.id if base else None,
    }


def hire_hireling(
    db: Session,
    campaign_id: int,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    name: str,
    employer: str,
    wage: int,
    description: str,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    pronouns: str | None = None,
    *,
    plus: str,
) -> dict[str, Any]:
    """Hire a character into the employer's company, at the employer's location. The hireling is
    controlled by the player and its wage is paid each day from the employer's currency. Its sheet is
    rolled the way an NPC's is."""
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    boss = state.characters.get(name_key(employer))
    if boss is None or boss.kind != PC_KIND or boss.hireling:
        raise InvalidInput(f"{employer} is not a player character who can hire")
    if boss.location is None:
        raise InvalidInput(f"{boss.name} has no location yet, so cannot hire anyone")
    if not isinstance(wage, int) or isinstance(wage, bool) or wage < 0:
        raise InvalidInput("the wage must be a whole number of currency, zero or more")
    try:
        check_choices(spec, character or {})
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    event = _npc_event(
        tables,
        rng,
        spec,
        name=name,
        location=boss.location,
        description=bounded_text(description, "description", MAX_TEXT),
        personality=_optional_text(personality, "personality"),
        accent=_optional_text(accent, "accent"),
        quirks=_quirks(quirks),
        character=character,
        plus=plus,
        pronouns=_optional_text(pronouns, "pronouns"),
    )
    record(
        db,
        campaign_id,
        HirelingAdded(
            name=event.name,
            employer=boss.name,
            wage=wage,
            location=event.location,
            description=event.description,
            personality=event.personality,
            accent=event.accent,
            quirks=list(event.quirks),
            pronouns=event.pronouns,
            stats=dict(event.stats),
            templates=dict(event.templates),
            features=dict(event.features),
            fields=dict(event.fields),
            counters=dict(event.counters),
            armor=event.armor,
            skills=dict(event.skills),
        ),
    )
    return {
        "hired": event.name,
        "employer": boss.name,
        "wage": wage,
        "location": event.location,
        "personality": event.personality,
        "accent": event.accent,
        "quirks": list(event.quirks),
    }


def dismiss_hireling(db: Session, campaign_id: int, name: str, cause: str) -> dict[str, Any]:
    """The hireling leaves its employer's company and stays where it is, as an NPC."""
    state = load_state(db, campaign_id)
    hireling = state.characters.get(name_key(name))
    if hireling is None or not hireling.hireling:
        raise InvalidInput(f"{name} is not a hireling")
    cause = bounded_text(cause, "cause", MAX_TEXT)
    employer = hireling.employer
    record(db, campaign_id, HirelingDismissed(name=hireling.name, cause=cause))
    db.commit()
    return {"dismissed": hireling.name, "employer": employer}


def promote_hireling(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    """After the player character died, a living hireling takes over as the player character."""
    state = load_state(db, campaign_id)
    current = state.pc()
    if current is not None and not current.dead:
        raise InvalidInput("the player character is still alive")
    hireling = state.characters.get(name_key(name))
    if hireling is None or not hireling.hireling:
        raise InvalidInput(f"{name} is not a hireling")
    if hireling.dead or hireling.dying_hours is not None or hireling.dying_rounds is not None:
        raise InvalidInput(f"{hireling.name} cannot take up the part: they are not able to act")
    employer = hireling.employer
    record(db, campaign_id, HirelingPromoted(name=hireling.name, employer=employer))
    db.commit()
    return {"promoted": hireling.name, "employer": employer}


def create_monster(
    db: Session,
    campaign_id: int,
    monsters: Mapping[str, MonsterTemplate],
    spec: CharacterSpec,
    name: str,
    location: str,
    description: str | None,
    template: str | None,
    stats: Mapping[str, int | None],
    armor: int | None,
    attacks: Sequence[Mapping[str, Any]] | None,
    armor_max: int = 0,
    force: bool = False,
    detachment: bool | None = None,
    rules: Sequence[str] | None = None,
) -> dict[str, Any]:
    """An NPC built from a statblock. A template gives the starting values, and any value given
    replaces it. Without a template, a name that matches a statblock uses it and takes no values
    of its own, unless force is set. A detachment flag overrides the statblock's group setting;
    without it, the statblock decides."""
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    where = _place_named(state, location)
    base: MonsterTemplate | None = None
    given = {key: value for key, value in stats.items() if value is not None}
    if template is not None:
        base = monsters.get(template.strip())
        if base is None:
            raise InvalidInput(
                f"there is no monster template {template!r}; search_monster lists them"
            )
    elif not force:
        base = _statblock_named(monsters, name)
        if base is not None and (given or armor is not None or attacks is not None):
            raise InvalidInput(_statblock_conflict(base, spec))
    merged = {**(base.stats if base else {}), **given}
    unknown = [key for key in merged if key not in spec.stats]
    if unknown:
        raise InvalidInput("unknown stats: " + ", ".join(unknown))
    missing = [
        spec.stats[key].name
        for key in spec.stats
        if key not in merged and spec.stats[key].derived is None
    ]
    if missing:
        raise InvalidInput("missing stats: " + ", ".join(missing))
    for key, value in merged.items():
        whole_number(value, f"{spec.stats[key].name}", 0, MAX_STAT)
    if description is not None:
        text = description
    elif base is not None:
        text = base.description
    else:
        raise InvalidInput("a description is needed when there is no template")
    armor_value = whole_number(
        armor if armor is not None else (base.armor if base else 0),
        "armor",
        0,
        armor_max or combat.UNCAPPED_ARMOR,
    )
    attack_map, flag_map, shock_map = _attack_table(attacks, base)
    event = NpcAdded(
        name=name,
        location=where.name,
        description=bounded_text(text, "description", MAX_TEXT),
        personality="",
        accent="",
        quirks=[],
        stats=dict(merged),
        armor=armor_value,
        attacks=attack_map,
        attack_flags=flag_map,
        attack_shock=shock_map,
        creature=base.name if base else "",
        rules=list(rules) if rules is not None else (list(base.rules) if base else []),
        large_group=(detachment if detachment is not None else bool(base and base.large_group)),
    )
    record(db, campaign_id, event)
    db.commit()
    return {
        "created": "monster",
        "name": event.name,
        "location": event.location,
        "template": base.id if base else None,
        "stats": dict(merged),
        "armor": armor_value,
        "attacks": attack_map,
        "attack_flags": flag_map,
        "attack_shock": shock_map,
        "detachment": event.large_group,
    }


def _attack_table(
    attacks: Sequence[Mapping[str, Any]] | None, base: MonsterTemplate | None
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[int]]]:
    """The attacks an NPC gets: the given ones, else the statblock's, else none. Returns the dice by
    attack name, the flags of the attacks that have any, and the Shock and the armor it shocks
    through, by attack name."""
    if attacks is not None:
        return _attacks(attacks)
    if base is None:
        return {}, {}, {}
    return (
        dict(base.attacks),
        {name: list(flags) for name, flags in base.attack_flags.items()},
        {name: list(pair) for name, pair in base.attack_shock.items()},
    )


def _attacks(
    attacks: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[int]]]:
    """Each attack is {"name", "dice", "flags", "shock", "shock_ac"}. Its dice must be a formula
    such as '1d8' or '1d6+1d6'; flags is an optional list of names from ATTACK_FLAGS, each listed
    once; and shock and shock_ac say what it does to a target it misses whose armor is low enough.
    A Shock value needs a shock_ac. Returns the dice, the flags, and the shock of the attacks that
    have any."""
    checked: dict[str, str] = {}
    flags_by_attack: dict[str, list[str]] = {}
    shock_by_attack: dict[str, list[int]] = {}
    seen: set[str] = set()
    for attack in attacks:
        label = bounded_text(attack.get("name", ""), "attack name", MAX_NAME)
        dice = attack.get("dice", "")
        flags = attack.get("flags", [])
        shock = attack.get("shock", 0)
        shock_ac = attack.get("shock_ac", 0)
        if label.casefold() in seen:
            raise InvalidInput(f"attack {label!r} is listed twice")
        if not isinstance(dice, str) or not is_formula(dice):
            raise InvalidInput(f"attack {label!r} needs a dice formula such as '1d8' or '1d6+1d6'")
        if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
            raise InvalidInput(f"attack {label!r} flags must be a list of names")
        unknown = [flag for flag in flags if flag not in ATTACK_FLAGS]
        if unknown or len(set(flags)) != len(flags):
            options = ", ".join(ATTACK_FLAGS)
            raise InvalidInput(f"attack {label!r} flags must each be listed once, from: {options}")
        shock_value = whole_number(shock, f"attack {label!r} shock", 0, MAX_STAT)
        shock_ac_value = whole_number(shock_ac, f"attack {label!r} shock_ac", 0, MAX_STAT)
        if shock_value and not shock_ac_value:
            raise InvalidInput(
                f"attack {label!r} gives Shock, so it needs the AC it shocks through"
            )
        seen.add(label.casefold())
        checked[label] = dice
        if flags:
            flags_by_attack[label] = list(flags)
        if shock_value:
            shock_by_attack[label] = [shock_value, shock_ac_value]
    return checked, flags_by_attack, shock_by_attack


def _statblock_conflict(base: MonsterTemplate, spec: CharacterSpec) -> str:
    """The refusal when a named statblock creature is given values of its own."""
    stats = ", ".join(
        f"{spec.stats[key].name} {value}" for key, value in base.stats.items() if key in spec.stats
    )
    attacks = "".join(
        f"; {label} {dice}{_flag_text(base.attack_flags.get(label, ()))}"
        for label, dice in base.attacks.items()
    )
    return (
        f"{base.name} has a published statblock ({stats}{attacks}). "
        "Leave out the values you would give to use it, or set force to create a custom "
        "creature with this name."
    )


def _with_statblock(event: NpcAdded, base: MonsterTemplate) -> NpcAdded:
    """The NPC event with a statblock's armor, attacks, creature, large-group flag, and rules.
    Its stats are set by the caller, before the event is made, so they are not rolled."""
    return replace(
        event,
        armor=base.armor,
        attacks=dict(base.attacks),
        attack_flags={name: list(flags) for name, flags in base.attack_flags.items()},
        attack_shock={name: list(pair) for name, pair in base.attack_shock.items()},
        creature=base.name,
        large_group=base.large_group,
        rules=list(dict.fromkeys([*event.rules, *base.rules])),
    )


def _with_hp_tier(
    character: Mapping[str, Any] | None,
    hp_tier: str,
    npc_hp: Mapping[str, int],
    stat: str | None,
    *,
    statblock: bool,
) -> dict[str, Any]:
    """The character with the HP of a tier. A tier is for an NPC with no statblock, and it sets
    the rule set's HP stat outright, so it cannot be combined with that stat given another way."""
    if stat is None or hp_tier not in npc_hp:
        choices = ", ".join(sorted(npc_hp)) or "none"
        raise InvalidInput(f"Unknown HP tier {hp_tier!r}; choose one of: {choices}")
    if statblock:
        raise InvalidInput(
            "A creature with a statblock takes its HP from the statblock; leave out hp_tier"
        )
    stats = dict((character or {}).get("stats") or {})
    if stat in stats:
        raise InvalidInput(f"Give either hp_tier or {stat}, not both")
    return {**(character or {}), "stats": {**stats, stat: npc_hp[hp_tier]}}


def _statblock_named(monsters: Mapping[str, MonsterTemplate], name: str) -> MonsterTemplate | None:
    """The statblock whose name or id is `name`, ignoring case, so a named module or bestiary
    creature gets its published stats without the caller supplying them."""
    wanted = name.strip().lower()
    slug = wanted.replace(" ", "-")
    for monster in monsters.values():
        if monster.id.lower() in (wanted, slug) or monster.name.lower() == wanted:
            return monster
    return None


def search_monster(
    monsters: Mapping[str, MonsterTemplate], query: str
) -> dict[str, list[dict[str, Any]]]:
    """Statblocks whose id, name, or description contain every word of `query`."""
    words = _words(query)
    matches = [
        {
            "id": monster.id,
            "name": monster.name,
            "description": monster.description,
            "stats": dict(monster.stats),
            "armor": monster.armor,
            "attacks": dict(monster.attacks),
            "attack_flags": {name: list(flags) for name, flags in monster.attack_flags.items()},
            "attack_shock": {name: list(pair) for name, pair in monster.attack_shock.items()},
        }
        for monster in monsters.values()
        if _has_words(words, monster.id, monster.name, monster.description)
    ]
    return {"monsters": matches}


def search_marketplace(marketplace: Marketplace, query: str) -> dict[str, Any]:
    """Catalogue items and services whose id, name, category, or description contain every word
    of `query`. Items come back in full, up to a limit, and `more` counts the rest."""
    words = _words(query)
    items = [
        {
            "item": item.name,
            "category": item.category,
            "price": item.price,
            "size": item.size,
            "description": item.description,
        }
        for item in marketplace.items
        if _has_words(words, item.id, item.name, item.category, item.description)
    ]
    services = [
        {"service": service.id, "name": service.name, "price": service.price}
        for service in marketplace.services
        if _has_words(words, service.id, service.name)
    ]
    return {
        "currency": marketplace.currency,
        "items": items[:MAX_MARKET_RESULTS],
        "more": max(0, len(items) - MAX_MARKET_RESULTS),
        "services": services,
    }


def update_location(
    db: Session,
    campaign_id: int,
    name: str,
    description: str,
    x: float | None = None,
    y: float | None = None,
    off_map: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Change a location's description and its map position. Giving x and y moves it. Giving
    off_map takes it off the map. Without any of these the position stays as it is."""
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    text = bounded_text(description, "description", MAX_TEXT)
    if not isinstance(off_map, bool):
        raise InvalidInput("off_map must be true or false")
    px, py = _coordinates(x, y)
    if where.parent is not None:
        if px is not None or py is not None:
            raise InvalidInput(f"{where.name} is inside {where.parent}, so it has no map position")
        off_map = False
    if off_map:
        if px is not None or py is not None:
            raise InvalidInput("give x and y to move the place, or off_map to take it off the map")
    elif px is None and py is None and where.position is not None:
        px, py = where.position.x, where.position.y
    _ensure_open_point(state, where.name, px, py, force)
    record(db, campaign_id, LocationUpdated(name=where.name, description=text, x=px, y=py))
    db.commit()
    return {"updated": "location", "name": where.name}


def discover_location(
    db: Session, campaign_id: int, name: str, summary: str | None = None
) -> dict[str, Any]:
    """Discover a place. A given summary is what the player sees of it. Giving one for a place that
    is already discovered replaces its summary. A summary is never required."""
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    text = _optional_text(summary, "summary", MAX_SUMMARY)
    if not where.discovered:
        record(db, campaign_id, LocationDiscovered(name=where.name, summary=text))
        db.commit()
    elif text is not None and text != where.summary:
        record(db, campaign_id, LocationSummarized(name=where.name, summary=text))
        db.commit()
    return {"discovered": "location", "name": where.name}


def delete_location(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    occupants = [c.name for c in state.characters.values() if c.location == where.name]
    if occupants:
        raise InvalidInput(
            f"{where.name} still has {', '.join(occupants)} there; move them elsewhere first"
        )
    inside = _children_of(state, where.name)
    if inside:
        raise InvalidInput(
            f"{where.name} still has {', '.join(inside)} inside it; delete those places first"
        )
    mapped = [m.name for m in state.maps.values() if name_key(m.location) == name_key(where.name)]
    if mapped:
        raise InvalidInput(
            f"{where.name} still has the encounter map {', '.join(mapped)}; delete the map first"
        )
    record(db, campaign_id, LocationRemoved(name=where.name))
    db.commit()
    return {"deleted": "location", "name": where.name}


def update_npc(
    db: Session,
    campaign_id: int,
    spec: CharacterSpec,
    rng: random.Random,
    name: str,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    armor: int | None = None,
    attacks: Sequence[Mapping[str, Any]] | None = None,
    *,
    armor_max: int = 0,
    defense_base: int = 0,
    plus: str,
) -> dict[str, Any]:
    """Change the NPC's details. Only the given fields change; quirks, if given, replace the list.
    A story-only NPC can be given its stats once, with character, armor, and attacks."""
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    text_given = any(value is not None for value in (description, personality, accent, quirks))
    stats_given = any(value is not None for value in (character, armor, attacks))
    if not text_given and not stats_given:
        raise InvalidInput(
            "give at least one of description, personality, accent, quirks, or stats"
        )
    statted: NpcStatted | None = None
    if stats_given:
        if not npc.story_only:
            raise InvalidInput(
                f"{npc.name} already has stats; only a story-only NPC can be given them"
            )
        try:
            check_choices(spec, character or {})
            sheet = resolve(spec, character or {}, rng, plus=plus)
        except CharacterError as error:
            raise InvalidInput(str(error)) from error
        _check_sheet_text(sheet)
        attack_map, attack_flags, attack_shock = _attacks(attacks or [])
        statted = NpcStatted(
            name=npc.name,
            armor=(sheet.armor if defense_base else 0)
            if armor is None
            else whole_number(armor, "armor", 0, armor_max or combat.UNCAPPED_ARMOR),
            attacks=attack_map,
            attack_flags=attack_flags,
            attack_shock=attack_shock,
            **_sheet_fields(sheet),
        )
    if text_given:
        record(
            db,
            campaign_id,
            NpcUpdated(
                name=npc.name,
                description=npc.description
                if description is None
                else bounded_text(description, "description", MAX_TEXT),
                personality=npc.personality
                if personality is None
                else bounded_text(personality, "personality", MAX_NAME),
                accent=npc.accent if accent is None else bounded_text(accent, "accent", MAX_NAME),
                quirks=list(npc.quirks) if quirks is None else _quirks(quirks) or [],
            ),
        )
    if statted is not None:
        record(db, campaign_id, statted)
    return {"updated": "npc", "name": npc.name, "statted": statted is not None}


def update_npc_knowledge(
    db: Session,
    campaign_id: int,
    name: str,
    *,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    pronouns: str | None = None,
    relationship: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Record what the player character has learned of an NPC. Only the given fields change. The
    NPC need not have been met yet: a rumor can put someone in the player's mind, with whatever
    was told. `location` is the place the player now knows the NPC to be."""
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    if all(
        value is None
        for value in (description, personality, accent, quirks, pronouns, relationship, location)
    ):
        raise InvalidInput(
            "give at least one of description, personality, accent, quirks, pronouns, "
            "relationship, or location"
        )
    known = state.known.get(name_key(npc.name), KnownNpc(name=npc.name))
    where = known.location
    if location is not None:
        where = _place_named(state, location).name
    record(
        db,
        campaign_id,
        NpcKnown(
            name=npc.name,
            description=known.description
            if description is None
            else bounded_text(description, "description", MAX_TEXT),
            personality=known.personality
            if personality is None
            else bounded_text(personality, "personality", MAX_NAME),
            accent=known.accent if accent is None else bounded_text(accent, "accent", MAX_NAME),
            quirks=list(known.quirks) if quirks is None else _quirks(quirks) or [],
            pronouns=known.pronouns if pronouns is None else _optional_text(pronouns, "pronouns"),
            relationship=known.relationship
            if relationship is None
            else bounded_text(relationship, "relationship", MAX_NAME),
            location=where,
        ),
    )
    db.commit()
    return {"updated": "npc knowledge", "name": npc.name}


def delete_npc(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    if state.encounter is not None and npc.name in state.encounter.order:
        raise InvalidInput(f"{npc.name} is in the encounter; remove them from the encounter first")
    record(db, campaign_id, NpcRemoved(name=npc.name))
    db.commit()
    return {"deleted": "npc", "name": npc.name}


def move_actor(
    db: Session,
    campaign_id: int,
    name: str,
    location: str | None = None,
    *,
    travel: Travel | None = None,
    speed: str | None = None,
    mode: str = IN_SETTLEMENT,
    clock_rules: ClockRules | None = None,
    hooks: Hooks | None = None,
    inventory: InventoryRules | None = None,
) -> dict[str, Any]:
    """Move a character to a place. With no place, an NPC goes into the wind: it is nowhere
    in particular until a later move brings it back. A player character's move in a settlement
    takes time from the travel table at the given speed, when the rule set has one. Moves in a
    dungeon, and overland moves placed after their watches, take no clock time here. The clock
    advances by whole hours; the rest of the minutes carry to the next move."""
    state = load_state(db, campaign_id)
    actor = _character_named(state, name)
    if location is None:
        if actor.kind != NPC_KIND:
            raise InvalidInput(f"{actor.name} is the player character and cannot go into the wind")
        record(db, campaign_id, ActorMoved(name=actor.name, location=None))
        db.commit()
        return {"moved": actor.name, "location": "in the wind"}
    if speed is None:
        speed = travel.default_mode if travel is not None else ""
    if travel is not None and speed not in travel.modes:
        raise InvalidInput(
            f"there is no travel mode named {speed!r}; use one of {', '.join(sorted(travel.modes))}"
        )
    where = _place_named(state, location)
    minutes = None
    if travel is not None and actor.kind == PC_KIND and mode == IN_SETTLEMENT:
        minutes = _travel_minutes(state, actor, where, travel, speed)
    record(db, campaign_id, ActorMoved(name=actor.name, location=where.name))
    # Going somewhere in person means the player character knows the place in detail, and every
    # place that holds it: anyone inside the great hall is inside the castle too.
    if actor.kind == PC_KIND:
        for place in _enclosing_places(state, where):
            if not place.discovered:
                record(db, campaign_id, LocationDiscovered(name=place.name))
    # Hirelings travel with the player character who employs them.
    if actor.kind == PC_KIND and not actor.hireling:
        for hireling in state.characters.values():
            if hireling.hireling and hireling.employer == actor.name:
                record(db, campaign_id, ActorMoved(name=hireling.name, location=where.name))
    result: dict[str, Any] = {"moved": actor.name, "location": where.name}
    if minutes is not None and clock_rules is not None:
        result.update(
            _charge_travel(db, campaign_id, state, minutes, clock_rules, hooks, inventory, speed)
        )
    db.commit()
    return result


def _travelling_party(state: CampaignState, actor: Character) -> list[Character]:
    """The character and the hirelings who go with them, in that order."""
    if actor.hireling:
        return [actor]
    hirelings = [c for c in state.characters.values() if c.hireling and c.employer == actor.name]
    return [actor, *hirelings]


def _travel_minutes(
    state: CampaignState, actor: Character, where: Place, travel: Travel, mode: str
) -> int:
    """The whole minutes a move takes: the straight-line distance from where the actor is, at the
    mode's speed. A move with no distance to work out takes the default leg."""
    origin = _place_position(state, actor.location)
    destination = _place_position(state, where.name)
    if origin is None or destination is None:
        return travel.default_leg_minutes
    distance = math.hypot(destination[0] - origin[0], destination[1] - origin[1])
    return round(distance / travel.modes[mode].km_per_hour * 60)


def _place_position(state: CampaignState, name: str | None) -> tuple[float, float] | None:
    """The map position of a place, as kilometers (x, y). A place inside another has none of its
    own, so it is at its parent's position."""
    place = state.places.get(name_key(name)) if name else None
    if place is None:
        return None
    if place.position is None and place.parent is not None:
        place = state.places.get(name_key(place.parent))
        if place is None:
            return None
    if place.position is None:
        return None
    return (place.position.x, place.position.y)


def _slug(name: str) -> str:
    """A place name as a location id writes it: "Village Gate" becomes "village-gate"."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def _authored_route(
    state: CampaignState, origin: Place | None, dest: Place
) -> Mapping[str, Any] | None:
    """The route the world authors from origin to dest, or None. A place inside another place
    uses its parent's routes, and a route to a place inside another leads to that parent."""
    origins = [origin]
    if origin is not None and origin.parent is not None:
        origins.append(state.places.get(name_key(origin.parent)))
    destinations = {_slug(dest.name), name_key(dest.name)}
    if dest.parent is not None:
        dest_parent = state.places.get(name_key(dest.parent))
        if dest_parent is not None:
            destinations |= {_slug(dest_parent.name), name_key(dest_parent.name)}
    for place in origins:
        if place is None:
            continue
        for key, route in place.routes.items():
            if key in destinations:
                return route
    return None


def _charge_travel(
    db: Session,
    campaign_id: int,
    state: CampaignState,
    minutes: int,
    clock_rules: ClockRules,
    hooks: Hooks | None,
    inventory: InventoryRules | None,
    mode: str,
) -> dict[str, Any]:
    total = state.travel_minutes + minutes
    hours, carried = divmod(total, 60)
    record(db, campaign_id, TravelCharged(minutes=carried))
    result: dict[str, Any] = {"minutes": minutes, "speed": mode}
    if hours:
        start = clock_service.absolute(
            state.clock, len(clock_rules.parts), clock_rules.hours_per_part
        )
        clock = clock_service.advance_to(
            db, campaign_id, clock_rules, start + hours, hooks, inventory
        )
        result["clock"] = clock
    return result


def add_note(
    db: Session, campaign_id: int, text: str, subject: str | None = None
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    text = bounded_text(text, "text", MAX_TEXT)
    subject_name: str | None = None
    label = _optional_text(subject, "subject")
    if label is not None and label.casefold() != CAMPAIGN_WIDE:
        entry = state.named(label)
        if entry is None:
            raise InvalidInput(f"there is no location or character named {label!r}")
        subject_name = entry.name
    note_id = record(db, campaign_id, NoteAdded(text=text, subject=subject_name))
    db.commit()
    return {
        "noted": True,
        "id": note_id,
        "subject": subject_name if subject_name is not None else CAMPAIGN_WIDE,
    }


def update_note(db: Session, campaign_id: int, note_id: int, text: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    _note_with_id(state, note_id)
    record(db, campaign_id, NoteUpdated(note_id=note_id, text=bounded_text(text, "text", MAX_TEXT)))
    db.commit()
    return {"updated": "note", "id": note_id}


def delete_note(db: Session, campaign_id: int, note_id: int) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    note = _note_with_id(state, note_id)
    record(db, campaign_id, NoteRemoved(note_id=note_id, text=note.text))
    db.commit()
    return {"deleted": "note", "id": note_id}


def search_notes(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """Notes whose text or subject name contains every word of `query`, newest first."""
    words = _words(query)
    matches: list[dict[str, Any]] = []
    for note in reversed(state.notes):
        if _has_words(words, note.subject, note.text):
            matches.append({"id": note.id, "subject": note.subject, "text": note.text})
    return {"notes": matches}


def append_journal(db: Session, campaign_id: int, text: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    added = bounded_text(text, "text", MAX_TEXT).strip()
    if not added:
        raise InvalidInput("text must hold what to add to the journal")
    record(db, campaign_id, JournalAppended(text=added))
    db.commit()
    return {"added": "journal", "day": state.clock.day}


def replace_in_journal(
    db: Session, campaign_id: int, old_text: str, new_text: str
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    old = bounded_text(old_text, "old_text", MAX_TEXT)
    if not old.strip():
        raise InvalidInput("old_text must hold the passage to replace")
    new = (
        bounded_text(new_text, "new_text", MAX_TEXT).strip() if str(new_text or "").strip() else ""
    )
    count = state.journal.count(old)
    if count == 0:
        raise InvalidInput(
            "the journal has no passage matching old_text; view or search it for the exact text"
        )
    if count > 1:
        raise InvalidInput(f"old_text matches {count} places; quote more of it so it matches once")
    record(db, campaign_id, JournalReplaced(old_text=old, new_text=new))
    db.commit()
    return {"replaced": "journal", "deleted": new == ""}


def view_journal(
    state: CampaignState, start_line: int = 1, end_line: int | None = None
) -> dict[str, Any]:
    """Lines `start_line` to `end_line` of the journal, numbered from 1, at most JOURNAL_VIEW_LINES."""
    lines = _journal_lines(state)
    if end_line is not None and end_line < start_line:
        raise InvalidInput("end_line must be at least start_line")
    first = max(1, start_line)
    last = min(first if end_line is None else end_line, len(lines))
    last = min(last, first + JOURNAL_VIEW_LINES - 1)
    return {
        "total_lines": len(lines),
        "lines": [
            {"number": number, "text": lines[number - 1]} for number in range(first, last + 1)
        ],
    }


def search_journal(
    state: CampaignState, query: str, context: int = 1
) -> dict[str, list[dict[str, Any]]]:
    """The passages of the journal that best match `query`, in journal order. A passage is a
    matching line with `context` lines on either side, numbered as `from_line` to `to_line`.
    Matching is fuzzy: a query word matches a journal word that contains it or is spelled nearly
    the same."""
    words = re.findall(r"\w+", bounded_text(query, "query", MAX_TEXT).casefold())
    lines = _journal_lines(state)
    context = max(0, min(context, MAX_JOURNAL_CONTEXT))
    scored = [
        (score, index)
        for index, line in enumerate(lines)
        if (score := _fuzzy_score(words, line)) > 0
    ]
    best = sorted(scored, key=lambda pair: (-pair[0], pair[1]))[:MAX_JOURNAL_RESULTS]
    ranges: list[list[int]] = []
    for _score, index in sorted(best, key=lambda pair: pair[1]):
        low, high = max(0, index - context), min(len(lines) - 1, index + context)
        if ranges and low <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], high)
        else:
            ranges.append([low, high])
    return {
        "passages": [
            {
                "from_line": low + 1,
                "to_line": high + 1,
                "text": "\n".join(lines[low : high + 1]),
            }
            for low, high in ranges
        ]
    }


def _journal_lines(state: CampaignState) -> list[str]:
    return state.journal.split("\n") if state.journal else []


def _fuzzy_score(words: Sequence[str], paragraph: str) -> int:
    """How many of the query words the paragraph matches."""
    found = re.findall(r"\w+", paragraph.casefold())
    return sum(
        1
        for word in words
        if any(word in candidate for candidate in found)
        or get_close_matches(word, found, n=1, cutoff=FUZZY_CUTOFF)
    )


def all_at_location(state: CampaignState, location: str | None) -> dict[str, Any]:
    """A location as the GM needs it: its description, the NPCs there, whether the player character
    is there, and the notes about the place or the NPCs in it. Without a location, it is the
    player character's location. Statblocks are left out; `get_statblock` returns them."""
    if location is None:
        pc = state.pc()
        if pc is None or pc.location is None:
            raise InvalidInput("the player character is not at a location yet; name a location")
        location = pc.location
    place = _place_named(state, location)
    npcs = _npcs_at(state, place.name)
    pc = state.pc()
    subjects = {place.name, *(npc.name for npc in npcs)}
    return {
        "location": _location_entry(place),
        "player_character_here": pc is not None and pc.location == place.name,
        "npcs": [_npc_summary(npc) for npc in npcs],
        "notes": [
            {"id": note.id, "subject": note.subject, "text": note.text}
            for note in reversed(state.notes)
            if note.subject in subjects
        ],
    }


def search_location(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """Locations whose name or description contains every word of `query`."""
    words = _words(query)
    matches = [
        _location_entry(place)
        for place in state.places.values()
        if _has_words(words, place.name, place.description)
    ]
    return {"locations": matches}


def search_npc(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """NPCs anywhere in the world whose details contain every word of `query`."""
    words = _words(query)
    matches = [
        _npc_summary(npc)
        for npc in state.characters.values()
        if npc.kind == NPC_KIND and _has_words(words, *_npc_words(npc))
    ]
    return {"npcs": matches}


def lookup_index(state: CampaignState) -> dict[str, list[dict[str, Any]]]:
    """Every location and NPC by name, each with a one-sentence brief. The brief is enough to know
    what to look up; the full entry comes from the lookup tools."""
    locations = [
        {"name": place.name, "brief": brief(place.description)} for place in state.places.values()
    ]
    npcs = [
        {"name": npc.name, "location": npc.location, "brief": brief(npc.description)}
        for npc in state.characters.values()
        if npc.kind == NPC_KIND
    ]
    return {"locations": locations, "npcs": npcs}


def _required[T](value: T | None, what: str) -> T:
    if value is None:
        raise InvalidInput(f"this rule set does not track {what}")
    return value


def speaker_at_player(state: CampaignState, name: str) -> bool:
    """Whether the named NPC or hireling is at the player character's location."""
    npc = _character_named(state, name)
    pc = state.pc()
    return pc is not None and npc.location == pc.location


def _npc_event(
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    *,
    name: str,
    location: str,
    description: str,
    personality: str | None,
    accent: str | None,
    quirks: Sequence[str] | None,
    character: Mapping[str, Any] | None = None,
    plus: str,
    story_only: bool = False,
    important: bool = False,
    rules: Sequence[str] = (),
    pronouns: str | None = None,
) -> NpcAdded:
    """The event that adds an NPC. A trait or character option that is not given is picked at
    random, so the picked values are stored in the event and replays do not roll again. A
    story-only NPC gets no stats at all."""
    trait_personality = personality or rng.choice(tables["personality"]).value
    trait_accent = accent or rng.choice(tables["accent"]).value
    if quirks is None:
        pool = [option.value for option in tables["quirk"]]
        trait_quirks = rng.sample(pool, min(QUIRKS_PER_NPC, len(pool)))
    else:
        trait_quirks = list(quirks)
    sheet_fields: dict[str, Any] = {}
    if not story_only:
        try:
            sheet = resolve(spec, character or {}, rng, plus=plus)
        except CharacterError as error:
            raise InvalidInput(str(error)) from error
        _check_sheet_text(sheet)
        sheet_fields = _sheet_fields(sheet)
        sheet_fields["armor"] = sheet.armor
    return NpcAdded(
        name=name,
        location=location,
        description=description,
        personality=trait_personality,
        accent=trait_accent,
        quirks=trait_quirks,
        story_only=story_only,
        important=important,
        rules=list(rules),
        pronouns=pronouns,
        **sheet_fields,
    )


def _sheet_fields(sheet: Sheet) -> dict[str, Any]:
    return {
        "stats": dict(sheet.stats),
        "templates": dict(sheet.templates),
        "features": dict(sheet.features),
        "fields": dict(sheet.fields),
        "counters": dict(sheet.counters),
        "skills": dict(sheet.skills),
    }


def _check_sheet_text(sheet: Sheet) -> None:
    for value in sheet.fields.values():
        if len(value) > MAX_TEXT:
            raise InvalidInput(f"character text must be at most {MAX_TEXT} characters")


def _ensure_free_name(state: CampaignState, name: str) -> None:
    if state.named(name) is not None:
        raise InvalidInput(f"the name {name!r} is already used by a location or character")


def _ensure_no_same_place(state: CampaignState, name: str) -> None:
    """A new place may not repeat an existing one under a different article, such as "The Farming
    Path" for "Farming Path"; discover_location reveals the existing place instead."""
    bare = _without_article(name)
    for place in state.places.values():
        if _without_article(place.name) == bare:
            raise InvalidInput(
                f"there is already a location called {place.name!r}; "
                "use discover_location to reveal it"
            )


def _without_article(name: str) -> str:
    key = name_key(name)
    return key.removeprefix("the ")


def _enclosing_places(state: CampaignState, place: Place) -> list[Place]:
    """The place and the places that enclose it, nearest first."""
    chain = [place]
    parent = place.parent
    while parent is not None:
        outer = state.named(parent)
        if not isinstance(outer, Place) or any(outer is seen for seen in chain):
            break
        chain.append(outer)
        parent = outer.parent
    return chain


def _parent_for_new_place(
    state: CampaignState, parent: str, x: float | None, y: float | None
) -> str:
    """Check a place to be created inside another. Such a place has no point of its own, and only
    a top-level place can hold others."""
    if x is not None or y is not None:
        raise InvalidInput("a place inside another has no map position; leave out x and y")
    outer = _place_named(state, parent)
    if outer.parent is not None:
        raise InvalidInput(
            f"{outer.name} is itself inside {outer.parent}; place it inside {outer.parent} instead"
        )
    return outer.name


def _ensure_open_point(
    state: CampaignState, name: str, x: float | None, y: float | None, force: bool
) -> None:
    """Refuse a point that another place already has, unless the GM passes force. Places do not
    share a point by default, so their pins never sit on top of each other."""
    if not isinstance(force, bool):
        raise InvalidInput("force must be true or false")
    if x is None or y is None or force:
        return
    for other in state.places.values():
        if other.name == name or other.position is None:
            continue
        if abs(other.position.x - x) < SAME_POINT_KM and abs(other.position.y - y) < SAME_POINT_KM:
            raise InvalidInput(
                f"{other.name!r} is already at that point. Choose another point, or pass "
                "force=true to put both places there."
            )


def _npc_named(state: CampaignState, name: str) -> Character:
    npc = _character_named(state, name)
    if npc.kind != NPC_KIND:
        raise InvalidInput(f"{npc.name} is the player character, not an NPC")
    return npc


def _note_with_id(state: CampaignState, note_id: int) -> Note:
    note = next((n for n in state.notes if n.id == note_id), None)
    if note is None:
        raise InvalidInput(f"there is no note with id {note_id}")
    return note


MAX_MARKET_RESULTS = 20
MAX_JOURNAL_RESULTS = 5
MAX_JOURNAL_CONTEXT = 3
JOURNAL_VIEW_LINES = 50
FUZZY_CUTOFF = 0.8


def _words(query: str) -> list[str]:
    return bounded_text(query, "query", MAX_TEXT).casefold().split()


def _has_words(words: Sequence[str], *parts: str) -> bool:
    haystack = " ".join(parts).casefold()
    return all(word in haystack for word in words)


def _optional_text(value: object, label: str, limit: int = MAX_NAME) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return bounded_text(value, label, limit)


def _coordinates(x: object, y: object) -> tuple[float | None, float | None]:
    """Both map coordinates or neither. Neither means the place has no position on the map."""
    if x is None and y is None:
        return None, None
    if x is None or y is None:
        raise InvalidInput("give both x and y, or neither")
    return _kilometers(x, "x"), _kilometers(y, "y")


def _kilometers(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InvalidInput(f"{label} must be a number of kilometers from the map's center")
    return float(value)


def _quirks(value: Sequence[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str) or len(value) > MAX_QUIRKS:
        raise InvalidInput(f"quirks must be a list of up to {MAX_QUIRKS} short texts")
    return [bounded_text(item, "each quirk", MAX_NAME) for item in value]
