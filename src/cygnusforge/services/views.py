"""Read-only views of a campaign: the player's scene and map, statblocks, and turn orientation.

Every function here folds the state it is given into text or view records. Nothing writes events,
opens a session, or changes the state. The world tools import these functions to answer reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from cygnusforge.content.character import CampaignSpec, CharacterSpec, Sheet, describe
from cygnusforge.content.library import (
    ClockRules,
    CombatRules,
    ConditionRules,
    CounterRules,
    EncounterGrid,
    EncounterObject,
    EncounterTerrain,
    Hooks,
    InventoryRules,
    RelationRules,
    WorldTerrain,
)
from cygnusforge.services import encounters
from cygnusforge.services import inventory as inventory_service
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.conditions import active_conditions
from cygnusforge.services.events import (
    NPC_KIND,
    PC_KIND,
    CampaignState,
    Character,
    Place,
    Region,
    WorldObject,
    current,
    defeat_status,
    name_key,
    same_item_name,
    token_labels,
)
from cygnusforge.services.geometry import contains
from cygnusforge.services.hooks import HookContext, call_hook
from cygnusforge.services.objects import visible_objects

# The map always shows at least this many kilometers from its center, and leaves a margin.
MIN_MAP_RADIUS_KM = 1.0
MAP_MARGIN = 1.1


@dataclass(frozen=True)
class KnownNpcView:
    """An NPC the player has met, as the player knows them: the details seen at the meeting, the
    relationship the story has revealed, and where the player last knew them to be."""

    name: str
    description: str
    personality: str
    accent: str
    quirks: tuple[str, ...]
    pronouns: str | None
    relationship: str
    # The rule set's named link, such as "Friend", when the player character holds one.
    relation: str
    # The place the player last knew the NPC to be, or None when the player does not know.
    location: str | None
    # The NPC is at the player character's location now.
    here: bool
    dead: bool


@dataclass(frozen=True)
class SceneView:
    """What the player may see: the player character, the place they are in, the NPCs there, the
    player character's statblock, the journal, and everyone the player has met."""

    pc: Character | None
    location: Place | None
    npcs: list[Character]
    sheet: list[str]
    journal: str = ""
    pack: PackView | None = None
    hirelings: tuple[Character, ...] = ()
    objects: tuple[WorldObject, ...] = ()
    known: tuple[KnownNpcView, ...] = ()


@dataclass(frozen=True)
class PackItem:
    """One item the player character carries, as the player sees it."""

    name: str
    size: str
    slots: int
    uses: int
    max_uses: int
    kind: str
    text: str
    damage: str = ""
    ready: bool = True
    equipped: str = ""


@dataclass(frozen=True)
class PackView:
    """The player character's items in carried order, with the slots used and the capacity."""

    items: list[PackItem]
    used: int
    capacity: int
    has_readied: bool = False


@dataclass(frozen=True)
class MapMarker:
    """A token for one place. Left and top are percentages of the map's width and height."""

    name: str
    label: str
    left: float
    top: float
    here: bool
    icon: str | None = None


@dataclass(frozen=True)
class MapRegion:
    """An area of ground on the map. Its points are percentages of the map's width and height.
    `name` and `text` are None until the region is known, and `text` is what hovering it shows.
    `pattern` is the id of the texture drawn over its colour, when its kind has one."""

    name: str | None
    text: str | None
    color: str
    points: tuple[tuple[float, float], ...]
    pattern: str | None = None


@dataclass(frozen=True)
class MapPattern:
    """A texture drawn over a kind of ground. `id` names it in the map's SVG, `shape` is one of
    TERRAIN_PATTERNS, and `ink` colours its marks."""

    id: str
    shape: str
    ink: str


@dataclass(frozen=True)
class TimeView:
    """The campaign day and the part of that day, shown beside the map."""

    day: int
    part: str
    night: bool


@dataclass(frozen=True)
class MapView:
    """The player's map. Only discovered places appear on it. The map is centered on the player
    character's place, and places with a position are placed on a square sized to fit the farthest
    one. Every discovered place is listed. `where` holds each place's line of location text, and
    `origin` is the percentage position of the true origin, or None when it is off the board.
    """

    radius_km: float
    markers: list[MapMarker]
    places: list[Place]
    where: dict[str, str] = field(default_factory=dict)
    origin: tuple[float, float] | None = None
    regions: list[MapRegion] = field(default_factory=list)
    patterns: list[MapPattern] = field(default_factory=list)


def get_statblock(
    state: CampaignState,
    name: str,
    spec: CharacterSpec,
    combat: CombatRules | None = None,
    conditions: Mapping[str, ConditionRules] | None = None,
    inventory: InventoryRules | None = None,
    hooks: Hooks | None = None,
    counters: Mapping[str, CounterRules] | None = None,
) -> dict[str, Any]:
    """A character's stats, template, features, and fields, one line each. Works for the player
    character and for NPCs. With a rule set's `combat`, the armor, attacks, harm, and status follow.
    With its `conditions`, `inventory`, `hooks`, and `counters`, the conditions, injuries,
    hooks-driven holds, items, and counter values follow."""
    character = _character_named(state, name)
    kind = "player character" if character.kind == PC_KIND else "npc"
    lines = _sheet_text(spec, character).splitlines()
    if character.description:
        lines.insert(0, f"Description: {character.description}")
    if combat is not None:
        lines += _combat_lines(character, spec, combat)
    lines += _status_lines(character, conditions, inventory, hooks, counters)
    for key, choices in character.feature_choices.items():
        lines.append(f"Feature choice {key}: {', '.join(choices)}")
    return {
        "name": character.name,
        "kind": kind,
        "location": character.location,
        "statblock": lines,
    }


def _combat_lines(character: Character, spec: CharacterSpec, combat: CombatRules) -> list[str]:
    lines = [f"Armor: {character.armor}"]
    if character.defense_tracked:
        lines.append(
            f"Worn armor: {character.equipped_armor or 'none'}; "
            f"wielded shield: {character.equipped_shield or 'none'}"
        )
    if character.large_group:
        if combat.groups is None:
            lines.append("Large group: a group that attacks with Blast")
        else:
            lines.append(
                "Large group: a group that attacks with Blast and is hit only by "
                f"{combat.groups.incoming_modifier} attacks"
            )
    if character.attacks:
        attacks = ", ".join(
            f"{name} {formula}{_flag_text(character.attack_flags.get(name, ()))}{_shock_text(character, name)}"
            for name, formula in character.attacks.items()
        )
        lines.append(f"Attacks: {attacks}")
    if any(character.harm.values()):
        harm = ", ".join(
            f"{spec.stats[stat].name} {amount}" for stat, amount in character.harm.items() if amount
        )
        lines.append(f"Harm: {harm}")
    if character.defeated:
        lines.append(f"Status: {defeat_status(character, combat.defeat)}")
    elif character.fled:
        lines.append("Status: fled")
    elif character.surrendered:
        lines.append("Status: surrendered")
    elif character.critical_due:
        lines.append("Status: broken, awaiting the GM's decision (resolve_group_break)")
    elif character.save_due is not None:
        lines.append(f"Status: {spec.stats[character.save_due].name} save due")
    return lines


def _shock_text(character: Character, attack: str) -> str:
    """An attack's Shock as a suffix for the record, such as " (Shock 2 / AC 13)", or ""."""
    pair = character.attack_shock.get(attack) or ()
    if len(pair) < 2 or not pair[0]:
        return ""
    return f" (Shock {pair[0]} / AC {pair[1]})"


def _status_lines(
    character: Character,
    conditions: Mapping[str, ConditionRules] | None,
    inventory: InventoryRules | None,
    hooks: Hooks | None,
    counters: Mapping[str, CounterRules] | None = None,
) -> list[str]:
    """The character's conditions, counters, injuries, the rule set's status lines, and what they carry. Each part
    shows only when the rule set has it, or when the character has something to report."""
    lines: list[str] = []
    if conditions is not None:
        held = [rules.label for rules in active_conditions(character, conditions)]
        lines.append(f"Conditions: {', '.join(held) if held else 'none'}")
    if counters:
        shown = [
            f"{rules.label} {character.counters.get(rules.id, rules.initial)}"
            + (f" of {rules.maximum}" if rules.maximum is not None else "")
            for rules in counters.values()
        ]
        lines.append(f"Counters: {', '.join(shown)}")
    if character.injuries:
        lines.append(f"Injuries: {'; '.join(character.injuries)}")
    lines.extend(
        call_hook(
            hooks, "status", HookContext.for_rendering(hooks, inventory), character, default=[]
        )
    )
    if inventory is not None:
        used = inventory_service.used_slots(character)
        capacity = inventory_service.capacity_of(character, inventory)
        flag = " (over capacity)" if used > capacity else ""
        lines.append(f"Inventory: {used} of {capacity} slots{flag}")
        carried = ", ".join(item.name for item in character.items)
        lines.append(f"Carrying: {carried or 'nothing'}")
    return lines


def speaker_profile(state: CampaignState, name: str, spec: CharacterSpec) -> str:
    """The NPC details a speech step needs."""
    npc = _character_named(state, name)
    if npc.kind != NPC_KIND and not npc.hireling:
        raise InvalidInput(f"{npc.name} is the player character and does not need speaking for")
    quirks = "; ".join(npc.quirks) or "none"
    lines = [f"Speaker: {npc.name}", f"Who they are: {npc.description}"]
    if npc.personality:
        lines.append(f"Personality: {npc.personality}")
    if npc.accent:
        lines.append(f"Accent: {npc.accent}")
    lines += [f"Quirks: {quirks}", f"Location: {npc.location}"]
    sheet = _sheet_text(spec, npc)
    if sheet:
        lines.append(sheet)
    return "\n".join(lines)


def _map_place(state: CampaignState) -> Place | None:
    """The place the map marks as the player's: the player character's own place, or the nearest
    place above it that has a position. None when there is no player character or no placed place
    above it."""
    pc = state.pc()
    name = pc.location if pc is not None else None
    seen: set[str] = set()
    while name is not None and name_key(name) not in seen:
        seen.add(name_key(name))
        place = state.named(name)
        if not isinstance(place, Place):
            return None
        if place.position is not None:
            return place
        name = place.parent
    return None


def _km_text(km: float) -> str:
    return f"{abs(km):.1f}".rstrip("0").rstrip(".")


def _offset_text(dx: float, dy: float) -> str:
    east = "east" if dx >= 0 else "west"
    north = "north" if dy >= 0 else "south"
    return f"{_km_text(dx)} km {east}, {_km_text(dy)} km {north}"


def time_view(state: CampaignState, clock: ClockRules | None) -> TimeView | None:
    """The clock's day and part. The last part of the day is night, as the wilderness rules count it."""
    if clock is None or not clock.parts:
        return None
    index = state.clock.part
    return TimeView(
        day=state.clock.day,
        part=clock.parts[index],
        night=index == len(clock.parts) - 1,
    )


def map_view(state: CampaignState, terrain: Mapping[str, WorldTerrain] | None = None) -> MapView:
    """The player's map. Only discovered places appear on it. Every region is drawn, but only known
    regions are named, and only they set the map's reach. Each marker is placed on a square centered
    on the player character's place, sized to fit the farthest discovered place or known region
    corner plus a margin. The player's place is marked as theirs, or, when it is inside another
    place, the nearest place above it that has a position. `terrain` gives the colour and texture of
    each region's kind."""
    places = sorted(
        (place for place in state.places.values() if place.discovered),
        key=lambda place: name_key(place.name),
    )
    # The map centers on the player's place, and marks it as theirs.
    marked = _map_place(state)
    marked_name = marked.name if marked is not None else None
    center = None if marked is None or marked.position is None else marked.position
    cx, cy = (center.x, center.y) if center is not None else (0.0, 0.0)
    placed = [(place, place.position) for place in places if place.position is not None]
    known = [region for region in state.regions.values() if _region_known(state, region)]
    corners = [point for region in known for point in region.points]
    reach = max(
        [MIN_MAP_RADIUS_KM]
        + [max(abs(pos.x - cx), abs(pos.y - cy)) for _, pos in placed]
        + [max(abs(x - cx), abs(y - cy)) for x, y in corners]
    )
    radius = reach * MAP_MARGIN
    pc = state.pc()
    at = pc.location if pc is not None else None
    labels = token_labels(place.name for place in places)
    markers = [
        MapMarker(
            name=place.name,
            label=labels[place.name],
            left=round(50 + (pos.x - cx) / radius * 50, 2),
            top=round(50 - (pos.y - cy) / radius * 50, 2),
            here=place.name == marked_name,
            icon=place.icon,
        )
        for place, pos in placed
    ]
    where: dict[str, str] = {}
    for place in places:
        if place.parent:
            where[place.name] = f"Inside {place.parent}"
        elif place.name == at or place.name == marked_name:
            where[place.name] = "You are here"
        elif place.position is not None and center is not None:
            where[place.name] = _offset_text(place.position.x - cx, place.position.y - cy)
    origin_left = round(50 - cx / radius * 50, 2)
    origin_top = round(50 + cy / radius * 50, 2)
    on_board = 0 <= origin_left <= 100 and 0 <= origin_top <= 100
    kinds = terrain or {}
    regions = [
        _map_region(region, kinds, cx, cy, radius, _region_known(state, region))
        for region in sorted(state.regions.values(), key=lambda region: name_key(region.name))
    ]
    patterns = [
        MapPattern(id=_pattern_id(kind.id), shape=kind.pattern, ink=kind.ink)
        for kind in kinds.values()
        if kind.pattern is not None and kind.ink is not None
    ]
    return MapView(
        radius_km=round(radius, 2),
        markers=markers,
        places=places,
        where=where,
        origin=(origin_left, origin_top) if on_board else None,
        regions=regions,
        patterns=patterns,
    )


def _region_known(state: CampaignState, region: Region) -> bool:
    """A region is known once it is discovered, or once a discovered place with a map point lies
    inside it."""
    if region.discovered:
        return True
    return any(
        place.discovered
        and place.position is not None
        and contains((place.position.x, place.position.y), region.points)
        for place in state.places.values()
    )


def _pattern_id(kind: str) -> str:
    return f"terrain-{kind}"


def _map_region(
    region: Region,
    kinds: Mapping[str, WorldTerrain],
    cx: float,
    cy: float,
    radius: float,
    known: bool,
) -> MapRegion:
    kind = kinds.get(region.terrain)
    color = kind.color if kind is not None else "#888888"
    name = text = None
    if known:
        name = region.name
        text = region.name if kind is None else f"{region.name} ({kind.name}). {kind.rule}"
    pattern = _pattern_id(kind.id) if kind is not None and kind.pattern is not None else None
    points = tuple(
        (round(50 + (x - cx) / radius * 50, 2), round(50 - (y - cy) / radius * 50, 2))
        for x, y in region.points
    )
    return MapRegion(name=name, text=text, color=color, points=points, pattern=pattern)


def scene_view(
    state: CampaignState,
    spec: CharacterSpec,
    inventory: InventoryRules | None = None,
    relations: Mapping[str, RelationRules] | None = None,
) -> SceneView:
    """The player's view. It shows only what is at the player character's location, including any
    hirelings there, and everyone the player has met. The pack shows the player character's items
    when the rule set has an inventory."""
    pc = state.pc()
    location = pc.location if pc is not None else None
    found = state.named(location) if location is not None else None
    place = found if isinstance(found, Place) else None
    npcs = _npcs_at(state, place.name) if place is not None else []
    sheet = _sheet_text(spec, pc).splitlines() if pc is not None else []
    pack = None
    if pc is not None and inventory is not None:
        armor, shield = pc.equipped_armor, pc.equipped_shield
        items = []
        for item in pc.items:
            equipped = ""
            if armor and same_item_name(item.name, armor):
                equipped, armor = "Worn", ""
            elif shield and same_item_name(item.name, shield):
                equipped, shield = "Wielded", ""
            items.append(
                PackItem(
                    name=item.name,
                    size=item.size,
                    slots=item.slots,
                    uses=item.uses,
                    max_uses=item.max_uses,
                    kind=item.kind,
                    text=item.text,
                    damage=item.damage,
                    ready=item.ready,
                    equipped=equipped,
                )
            )
        pack = PackView(
            items=items,
            used=inventory_service.used_slots(pc),
            capacity=inventory_service.capacity_of(pc, inventory, spec),
            has_readied=bool(inventory.readied_stat or any(pool.ready for pool in inventory.pools)),
        )
    hirelings = tuple(
        c
        for c in state.characters.values()
        if c.hireling and place is not None and c.location == place.name
    )
    objects = tuple(visible_objects(state, place.name)) if place is not None else ()
    return SceneView(
        pc=pc,
        location=place,
        npcs=npcs,
        sheet=sheet,
        journal=state.journal,
        pack=pack,
        hirelings=hirelings,
        objects=objects,
        known=_known_npc_views(state, pc, place, relations),
    )


def world_state_text(
    state: CampaignState,
    campaign: CampaignSpec,
    grid: EncounterGrid | None = None,
    combat: CombatRules | None = None,
    conditions: Mapping[str, ConditionRules] | None = None,
    clock_rules: ClockRules | None = None,
    inventory: InventoryRules | None = None,
    terrain: Mapping[str, EncounterTerrain] | None = None,
    encounter_objects: Mapping[str, EncounterObject] | None = None,
    hooks: Hooks | None = None,
    counters: Mapping[str, CounterRules] | None = None,
) -> str:
    """The GM's orientation at the start of a turn: the campaign options, the day, the player
    character and where they are, the NPCs there, and any encounter in progress. The rest is looked
    up with the lookup tools."""
    pc = state.pc()
    if pc is None:
        return ""
    lines = [
        f"Campaign {campaign.fields[field_id].name.lower()}: {value}."
        for field_id, value in state.options.items()
        if field_id in campaign.fields
    ]
    if state.settings:
        lines.append("Setting:")
        lines.extend(f"  {topic}: {detail}" for topic, detail in state.settings.items())
    if clock_rules is not None:
        when = f"day {state.clock.day}, {clock_rules.parts[state.clock.part]}"
        if clock_rules.hours_per_part > 1:
            when += f", hour {state.clock.hour + 1} of {clock_rules.hours_per_part}"
        lines.append(f"It is {when}.")
    if pc.pronouns:
        lines.append(f"Pronouns: {pc.name}, {pc.pronouns}.")
    if pc.defense_tracked:
        lines.append(
            f"Armor: {pc.armor}; worn armor: {pc.equipped_armor or 'none'}; "
            f"wielded shield: {pc.equipped_shield or 'none'}."
        )
    if pc.dead:
        where = f" the body lies at {pc.location}." if pc.location is not None else ""
        lines.append(f"Player character {pc.name} is dead;{where}".rstrip(".") + ".")
    elif pc.location is None:
        lines.append(
            f"Player character: {pc.name}, not yet placed; place them with move_actor on your first turn."
        )
    else:
        lines.append(f"Player character: {pc.name}, at {pc.location}.")
        names = [
            f"{npc.name} (story only, no stats yet)" if npc.story_only else npc.name
            for npc in _npcs_at(state, pc.location)
        ]
        lines.append(f"NPCs here: {', '.join(names) if names else 'none'}.")
        for npc in _npcs_at(state, pc.location):
            if npc.pronouns:
                lines.append(f"Pronouns: {npc.name}, {npc.pronouns}.")
        things = [o.contents for o in visible_objects(state, pc.location)]
        if things:
            lines.append(f"Objects here: {'; '.join(things)}.")
        here = _place_named(state, pc.location)
        if here.parent is not None:
            lines.append(f"{here.name} is inside {here.parent}.")
        inside = [_place_named(state, name) for name in _children_of(state, here.name)]
        if inside:
            rooms = ", ".join(
                f"{room.name} ({'discovered' if room.discovered else 'not yet discovered'})"
                for room in inside
            )
            lines.append(f"Places inside {here.name}: {rooms}.")
    hirelings = [c for c in state.characters.values() if c.hireling]
    if hirelings:
        lines.append(
            "Hirelings, controlled by the player: "
            + "; ".join(
                f"{h.name}, at {h.location or 'no location'}, hired by {h.employer} "
                f"at {h.wage} a day"
                for h in hirelings
            )
            + "."
        )
        for h in hirelings:
            if h.pronouns:
                lines.append(f"Pronouns: {h.name}, {h.pronouns}.")
    wind = _npcs_in_the_wind(state)
    if wind:
        lines.append(f"NPCs in the wind, at no location: {', '.join(wind)}.")
    lines.extend(_status_lines(pc, conditions, inventory, hooks, counters))
    lines.extend(_encounter_lines(state, grid, combat, terrain, encounter_objects))
    return "\n".join(lines)


def _encounter_lines(
    state: CampaignState,
    grid: EncounterGrid | None,
    combat: CombatRules | None,
    terrain: Mapping[str, EncounterTerrain] | None = None,
    encounter_objects: Mapping[str, EncounterObject] | None = None,
) -> list[str]:
    view = encounters.encounter_view(state, grid, combat, terrain, encounter_objects)
    if view is None:
        return []
    lines = [
        f"Encounter in progress on the map {view['map']} at {view['location']}, round {view['round']}.",
        f"Map: {view['columns']} by {view['rows']} cells, columns and rows counted from 0 at the top left. {view['description']}",
    ]
    if view["background"] is not None:
        lines.append(_terrain_sentence("Background", view["background"]))
    if view["marked_cells"]:
        marked = [_marked_cell_text(cell) for cell in view["marked_cells"]]
        lines.append("Marked cells: " + "; ".join(marked) + ".")
    if view["awaiting_initiative"]:
        waiting = ", ".join(view["awaiting_initiative"])
        lines.append(
            f"Initiative is not set yet. {waiting} must roll for initiative with player_initiative."
        )
    if view["acting"] is not None:
        left = (
            f", with {view['moves_left']} of {view['move_cells']} cells of movement left"
            if view["moves_left"] is not None
            else ""
        )
        lines.append(f"Taking their turn now: {view['acting']}{left}.")
    if not view["participants"]:
        lines.append("No participants yet.")
    for who in view["participants"]:
        cell = who["cell"]
        where = f"column {cell[0]}, row {cell[1]}" if cell else "not yet placed"
        kind = "player character" if who["is_pc"] else "NPC"
        status = f", {who['status']}" if who["status"] else ""
        lines.append(
            f"- {who['number']}. {who['name']} ({kind}, {who['side']} side{status}), {where}."
        )
    return lines


def _terrain_sentence(label: str, terrain: dict[str, Any]) -> str:
    difficult = (
        " It is difficult: each cell entered costs two moves." if terrain["difficult"] else ""
    )
    return f"{label} terrain: {terrain['name']}.{difficult}"


def _marked_cell_text(cell: dict[str, Any]) -> str:
    parts = []
    if cell["terrain"]:
        parts.append(f"{cell['terrain']}{' (difficult)' if cell['difficult'] else ''}")
    if cell["object"]:
        parts.append(f"{cell['object']}{' (blocks)' if cell['blocks'] else ''}")
    return f"column {cell['column']}, row {cell['row']}: {', '.join(parts)}"


def _location_entry(place: Place) -> dict[str, Any]:
    position = None
    if place.position is not None:
        position = {"x": place.position.x, "y": place.position.y}
    return {
        "name": place.name,
        "description": place.description,
        "summary": place.summary,
        "discovered": place.discovered,
        "position_km": position,
        "parent": place.parent,
    }


def _npcs_at(state: CampaignState, location: str) -> list[Character]:
    return [c for c in state.characters.values() if c.kind == NPC_KIND and c.location == location]


def _known_npc_views(
    state: CampaignState,
    pc: Character | None,
    place: Place | None,
    relations: Mapping[str, RelationRules] | None,
) -> tuple[KnownNpcView, ...]:
    """Everyone the player has met, with the rule set's named relation where the player character
    holds one. A known NPC whose record was removed is left out."""
    views = []
    for key, knowledge in state.known.items():
        npc = state.characters.get(key)
        if npc is None or npc.kind != NPC_KIND:
            continue
        relation = ""
        if pc is not None:
            for relation_id, names in pc.relations.items():
                if any(name_key(name) == key for name in names):
                    rules = relations.get(relation_id) if relations is not None else None
                    relation = rules.label if rules is not None else relation_id.capitalize()
                    break
        views.append(
            KnownNpcView(
                name=knowledge.name,
                description=knowledge.description,
                personality=knowledge.personality,
                accent=knowledge.accent,
                quirks=knowledge.quirks,
                pronouns=knowledge.pronouns,
                relationship=knowledge.relationship,
                relation=relation,
                location=knowledge.location,
                here=place is not None and npc.location == place.name,
                dead=npc.dead,
            )
        )
    views.sort(key=lambda view: view.name.casefold())
    return tuple(views)


def present_rules_text(state: CampaignState, rules: Mapping[str, str]) -> str:
    """The text of the rules attached to what is present: the place the player character is in,
    the NPCs there, defeated or not, and the items the player character carries. Empty when
    nothing is attached."""
    pc = state.pc()
    if pc is None:
        return ""
    sources: dict[str, list[str]] = {}
    if pc.location is not None:
        place = state.places.get(name_key(pc.location))
        if place is not None:
            for rule_id in place.rules:
                sources.setdefault(rule_id, []).append(place.name)
        for npc in _npcs_at(state, pc.location):
            for rule_id in npc.rules:
                sources.setdefault(rule_id, []).append(npc.name)
    for item in pc.items:
        for rule_id in item.rules:
            sources.setdefault(rule_id, []).append(item.name)
    lines = [
        f"- {rule_id} (from {', '.join(names)}): {rules.get(rule_id, '')}"
        for rule_id, names in sources.items()
    ]
    if not lines:
        return ""
    return "\n".join(
        [
            "These rules belong to the place, NPCs, and items present now. Follow them as written.",
            *lines,
        ]
    )


def _npcs_in_the_wind(state: CampaignState) -> list[str]:
    return sorted(
        c.name for c in state.characters.values() if c.kind == NPC_KIND and c.location is None
    )


def _npc_summary(npc: Character) -> dict[str, Any]:
    return {
        "name": npc.name,
        "location": npc.location,
        "description": npc.description,
        "personality": npc.personality,
        "accent": npc.accent,
        "quirks": list(npc.quirks),
        "pronouns": npc.pronouns,
        "defeated": npc.defeated,
    }


def _npc_words(npc: Character) -> list[str]:
    return [npc.name, npc.location or "", npc.description, npc.personality, npc.accent, *npc.quirks]


def _flag_text(flags: Sequence[str]) -> str:
    """The flags of one attack as a suffix for the record, such as " (blast)", or "" if none."""
    return f" ({', '.join(flags)})" if flags else ""


def _sheet_of(character: Character) -> Sheet:
    return Sheet(
        stats=character.stats,
        templates=character.templates,
        features=character.features,
        fields=character.fields,
        counters=character.counters,
        skills=character.skills,
    )


def _sheet_text(spec: CharacterSpec, character: Character) -> str:
    """A character's sheet as text. A player character's sheet leaves out the rule set's
    statblock defaults: stats it derives with no inputs, such as a monster's Morale, which a
    statblock supplies to an NPC and a player character never uses. A stat the character has
    taken harm to shows its remaining value beside its score, so a wounded character reads as
    wounded."""
    sheet = _sheet_of(character)
    own = sheet.stats
    if character.kind == PC_KIND:
        own = {
            stat: value
            for stat, value in sheet.stats.items()
            if not _is_statblock_default(spec, stat)
        }
    text = describe(spec, replace(sheet, stats=own))
    harmed = []
    for stat, taken in character.harm.items():
        if taken <= 0 or stat not in spec.stats:
            continue
        score = own.get(stat, character.stats.get(stat, 0))
        harmed.append(f"{spec.stats[stat].name} {current(character, stat)} of {score}")
    if harmed:
        text = f"{text}\nCurrent: {', '.join(harmed)}"
    return text


def _is_statblock_default(spec: CharacterSpec, stat: str) -> bool:
    """A derived stat with no inputs is a value a statblock supplies, not one computed from the
    character's own scores, skills, counters, or templates."""
    found = spec.stats.get(stat)
    if found is None or found.derived is None:
        return False
    derived = found.derived
    return not any(
        (
            derived.plus,
            derived.minus_counters,
            derived.best_modifier,
            derived.best_score,
            derived.plus_skills,
            derived.plus_skill_picks,
            derived.minus_best_modifier,
            derived.minus_templates,
        )
    )


def _place_named(state: CampaignState, name: str) -> Place:
    entry = state.named(name)
    if not isinstance(entry, Place):
        raise InvalidInput(f"there is no location named {name!r}")
    return entry


def _children_of(state: CampaignState, name: str) -> list[str]:
    return sorted(p.name for p in state.places.values() if p.parent == name)


def _character_named(state: CampaignState, name: str) -> Character:
    entry = state.named(name)
    if not isinstance(entry, Character):
        raise InvalidInput(f"there is no character named {name!r}")
    return entry
