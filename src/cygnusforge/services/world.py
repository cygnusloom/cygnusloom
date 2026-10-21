"""Places, characters, and notes: the world that a campaign's GM edits.

Writes check the campaign's current state, record events, and commit. Reads work on a state that
is already folded from the log. Problems the model can fix are raised as `InvalidInput`, and their
messages are returned to the model as they are."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session

from cygnusforge.content.character import (
    CampaignSpec,
    CharacterError,
    CharacterSpec,
    Sheet,
    TableOption,
    campaign_schema,
    check_choices,
    check_swaps,
    choice_options,
    describe,
    resolve,
)
from cygnusforge.content.library import EncounterGrid, WorldTemplate
from cygnusforge.rules.context import brief
from cygnusforge.services import encounters
from cygnusforge.services.common import InvalidInput, bounded_text, session_scope
from cygnusforge.services.events import (
    CAMPAIGN_WIDE,
    NPC_KIND,
    PC_KIND,
    ActorMoved,
    CampaignOptionsChosen,
    CampaignState,
    Character,
    LocationAdded,
    LocationDiscovered,
    LocationRemoved,
    LocationUpdated,
    Note,
    NoteAdded,
    NoteRemoved,
    NoteUpdated,
    NpcAdded,
    NpcRemoved,
    NpcUpdated,
    OpeningStarted,
    PcAdded,
    Place,
    load_state,
    name_key,
    record,
)
from cygnusforge.services.tools import check_arguments

MAX_NAME = 120
MAX_TEXT = 2000
MAX_QUIRKS = 10
QUIRKS_PER_NPC = 2
# The map always shows at least this many kilometers from its center, and leaves a margin.
MIN_MAP_RADIUS_KM = 1.0
MAP_MARGIN = 1.1
# Two places count as sharing a point when their positions agree to this many kilometers.
SAME_POINT_KM = 0.01


@dataclass(frozen=True)
class SceneView:
    """What the player may see: the player character, the place they are in, the NPCs there, the
    notes that concern them, and the player character's statblock."""

    pc: Character | None
    location: Place | None
    npcs: list[Character]
    notes: list[Note]
    sheet: list[str]


@dataclass(frozen=True)
class MapMarker:
    """A numbered pin for one place. Left and top are percentages of the map's width and height."""

    name: str
    number: int
    left: float
    top: float
    here: bool


@dataclass(frozen=True)
class MapView:
    """The player's map. Only discovered places appear on it. Places with a position are placed on a
    square centered on the origin and sized to fit the farthest one. Every discovered place is listed.
    The placed ones are numbered, and the numbers match the pins."""

    radius_km: float
    markers: list[MapMarker]
    places: list[Place]
    numbers: dict[str, int]


@dataclass(frozen=True)
class WorldTools:
    """The world as the rule set's tools use it. Each call runs in its own session and commits."""

    engine: Engine
    campaign_id: int
    tables: Mapping[str, Sequence[TableOption]]
    rng: random.Random
    character: CharacterSpec
    # The rule set's encounter grid, or None when its encounters have no movement limit.
    grid: EncounterGrid | None = None

    def create_location(
        self,
        name: str,
        description: str,
        discovered: bool = False,
        x: float | None = None,
        y: float | None = None,
        force: bool = False,
        parent: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_location(
                db, self.campaign_id, name, description, discovered, x, y, force, parent
            )

    def discover_location(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return discover_location(db, self.campaign_id, name)

    def create_npc(
        self,
        name: str,
        location: str,
        description: str,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
        character: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_npc(
                db,
                self.campaign_id,
                self.tables,
                self.rng,
                self.character,
                name,
                location,
                description,
                personality,
                accent,
                quirks,
                character,
            )

    def move_actor(self, name: str, location: str) -> dict[str, str]:
        with session_scope(self.engine) as db:
            return move_actor(db, self.campaign_id, name, location)

    def update_location(
        self,
        name: str,
        description: str,
        x: float | None = None,
        y: float | None = None,
        off_map: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_location(db, self.campaign_id, name, description, x, y, off_map, force)

    def delete_location(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_location(db, self.campaign_id, name)

    def update_npc(
        self,
        name: str,
        description: str | None = None,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_npc(db, self.campaign_id, name, description, personality, accent, quirks)

    def delete_npc(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_npc(db, self.campaign_id, name)

    def create_encounter_map(
        self,
        name: str,
        location: str,
        columns: int,
        rows: int,
        description: str,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.create_map(
                db, self.campaign_id, name, location, columns, rows, description
            )

    def update_encounter_map(
        self, name: str, columns: int, rows: int, description: str
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.update_map(db, self.campaign_id, name, columns, rows, description)

    def delete_encounter_map(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.delete_map(db, self.campaign_id, name)

    def start_encounter(self, map: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.start_encounter(db, self.campaign_id, map)

    def end_encounter(self) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.end_encounter(db, self.campaign_id)

    def add_participant(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.add_participant(db, self.campaign_id, name)

    def remove_participant(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.remove_participant(db, self.campaign_id, name)

    def place_participant(self, name: str, column: int, row: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.place_participant(db, self.campaign_id, name, column, row, self.grid)

    def end_npc_turn(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.end_turn(db, self.campaign_id, name, by_player=False)

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return add_note(db, self.campaign_id, text, subject)

    def update_note(self, note_id: int, text: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_note(db, self.campaign_id, note_id, text)

    def delete_note(self, note_id: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_note(db, self.campaign_id, note_id)

    def search_notes(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_notes(load_state(db, self.campaign_id), query)

    def all_at_location(self, location: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return all_at_location(load_state(db, self.campaign_id), location)

    def search_location(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_location(load_state(db, self.campaign_id), query)

    def search_npc(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_npc(load_state(db, self.campaign_id), query)

    def get_statblock(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return get_statblock(load_state(db, self.campaign_id), name, self.character)

    def character_options(self, field: str | None = None) -> dict[str, Any]:
        return choice_options(self.character, field)

    def lookup_index(self) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return lookup_index(load_state(db, self.campaign_id))

    def speaker_profile(self, name: str) -> str:
        with session_scope(self.engine) as db:
            return speaker_profile(load_state(db, self.campaign_id), name, self.character)


def seed_campaign(
    db: Session,
    campaign_id: int,
    world: WorldTemplate,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
) -> None:
    """Record a campaign's places and the world's NPCs as events. The player character is made
    later, by `create_player_character`."""
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
            ),
        )
    for npc in world.npcs:
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
        )
        record(db, campaign_id, event)
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
    db.commit()
    return {"chosen": "campaign"}


def create_player_character(
    db: Session,
    campaign_id: int,
    campaign: CampaignSpec,
    spec: CharacterSpec,
    name: str,
    rolled: Mapping[str, int],
    submitted: Mapping[str, Any],
    rng: random.Random,
) -> dict[str, Any]:
    """Make the player character from the player's choices. `rolled` are the stats rolled for
    this character; the player may only keep them or swap within a swap group."""
    state = load_state(db, campaign_id)
    if state.pc() is not None:
        raise InvalidInput("the player character already exists")
    if campaign.fields and not state.options:
        raise InvalidInput("the campaign options must be chosen before the player character")
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    chosen = submitted.get("stats", {})
    if set(chosen) != set(spec.stats):
        raise InvalidInput("every stat needs a value")
    try:
        check_swaps(spec, rolled, chosen)
        check_choices(spec, submitted.get("fields", {}))
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    sheet = resolve(spec, submitted, rng)
    _check_sheet_text(sheet)
    record(db, campaign_id, PcAdded(name=name, **_sheet_fields(sheet)))
    record(db, campaign_id, OpeningStarted())
    db.commit()
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
    text = bounded_text(description, "description", MAX_TEXT)
    if not isinstance(discovered, bool):
        raise InvalidInput("discovered must be true or false")
    px, py = _coordinates(x, y)
    inside = None
    if parent is not None:
        inside = _parent_for_new_place(state, parent, px, py)
        px, py = None, None
    else:
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
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    where = _place_named(state, location)
    try:
        check_choices(spec, (character or {}).get("fields", {}))
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
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
    )
    record(db, campaign_id, event)
    db.commit()
    return {
        "created": "npc",
        "name": event.name,
        "location": event.location,
        "personality": event.personality,
        "accent": event.accent,
        "quirks": list(event.quirks),
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


def discover_location(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    if not where.discovered:
        record(db, campaign_id, LocationDiscovered(name=where.name))
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
    name: str,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Change the NPC's details. Only the given fields change; quirks, if given, replace the list."""
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    if all(value is None for value in (description, personality, accent, quirks)):
        raise InvalidInput("give at least one of description, personality, accent, or quirks")
    event = NpcUpdated(
        name=npc.name,
        description=npc.description
        if description is None
        else bounded_text(description, "description", MAX_TEXT),
        personality=npc.personality
        if personality is None
        else bounded_text(personality, "personality", MAX_NAME),
        accent=npc.accent if accent is None else bounded_text(accent, "accent", MAX_NAME),
        quirks=list(npc.quirks) if quirks is None else _quirks(quirks) or [],
    )
    record(db, campaign_id, event)
    db.commit()
    return {"updated": "npc", "name": npc.name}


def delete_npc(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    if state.encounter is not None and npc.name in state.encounter.order:
        raise InvalidInput(f"{npc.name} is in the encounter; remove them from the encounter first")
    record(db, campaign_id, NpcRemoved(name=npc.name))
    db.commit()
    return {"deleted": "npc", "name": npc.name}


def move_actor(db: Session, campaign_id: int, name: str, location: str) -> dict[str, str]:
    state = load_state(db, campaign_id)
    actor = _character_named(state, name)
    where = _place_named(state, location)
    record(db, campaign_id, ActorMoved(name=actor.name, location=where.name))
    # Going somewhere in person means the player character knows the place in detail.
    if actor.kind == PC_KIND and not where.discovered:
        record(db, campaign_id, LocationDiscovered(name=where.name))
    db.commit()
    return {"moved": actor.name, "location": where.name}


def add_note(
    db: Session, campaign_id: int, text: str, subject: str | None = None
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    text = bounded_text(text, "text", MAX_TEXT)
    subject_name: str | None = None
    label = _optional_text(subject, "subject")
    if label is not None:
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


def get_statblock(state: CampaignState, name: str, spec: CharacterSpec) -> dict[str, Any]:
    """A character's stats, template, features, and fields, one line each. Works for the player
    character and for NPCs."""
    character = _character_named(state, name)
    kind = "player character" if character.kind == PC_KIND else "npc"
    return {
        "name": character.name,
        "kind": kind,
        "location": character.location,
        "statblock": describe(spec, _sheet_of(character)).splitlines(),
    }


def speaker_profile(state: CampaignState, name: str, spec: CharacterSpec) -> str:
    """The NPC details a speech step needs. The NPC must be at the player character's location."""
    npc = _character_named(state, name)
    if npc.kind != NPC_KIND:
        raise InvalidInput(f"{npc.name} is the player character and does not need speaking for")
    pc = state.pc()
    if pc is None or npc.location != pc.location:
        raise InvalidInput(f"{npc.name} is not at the player character's location")
    quirks = "; ".join(npc.quirks) or "none"
    lines = [
        f"Speaker: {npc.name}",
        f"Who they are: {npc.description}",
        f"Personality: {npc.personality}",
        f"Accent: {npc.accent}",
        f"Quirks: {quirks}",
        f"Location: {npc.location}",
    ]
    sheet = describe(spec, _sheet_of(npc))
    if sheet:
        lines.append(sheet)
    return "\n".join(lines)


def map_view(state: CampaignState) -> MapView:
    """The player's map. Only discovered places appear on it. Each marker is placed on a square
    centered on the origin, sized to fit the farthest discovered place plus a margin."""
    places = sorted(
        (place for place in state.places.values() if place.discovered),
        key=lambda place: name_key(place.name),
    )
    placed = [place for place in places if place.position is not None]
    reach = max(
        [MIN_MAP_RADIUS_KM]
        + [max(abs(p.position.x), abs(p.position.y)) for p in placed if p.position is not None]
    )
    radius = reach * MAP_MARGIN
    pc = state.pc()
    here = pc.location if pc is not None else None
    numbers = {place.name: index for index, place in enumerate(placed, start=1)}
    markers = [
        MapMarker(
            name=place.name,
            number=numbers[place.name],
            left=round(50 + place.position.x / radius * 50, 2),
            top=round(50 - place.position.y / radius * 50, 2),
            here=place.name == here,
        )
        for place in placed
        if place.position is not None
    ]
    return MapView(radius_km=round(radius, 2), markers=markers, places=places, numbers=numbers)


def scene_view(state: CampaignState, spec: CharacterSpec) -> SceneView:
    """The player's view. It shows only what is at the player character's location."""
    pc = state.pc()
    location = pc.location if pc is not None else None
    found = state.named(location) if location is not None else None
    place = found if isinstance(found, Place) else None
    npcs = _npcs_at(state, place.name) if place is not None else []
    subjects = {CAMPAIGN_WIDE}
    if pc is not None:
        subjects.add(pc.name)
    if place is not None:
        subjects.add(place.name)
    subjects.update(npc.name for npc in npcs)
    notes = [note for note in reversed(state.notes) if note.subject in subjects]
    sheet = describe(spec, _sheet_of(pc)).splitlines() if pc is not None else []
    return SceneView(pc=pc, location=place, npcs=npcs, notes=notes, sheet=sheet)


def world_state_text(
    state: CampaignState, campaign: CampaignSpec, grid: EncounterGrid | None = None
) -> str:
    """The GM's orientation at the start of a turn: the campaign options, the player character and
    where they are, the NPCs there, and any encounter in progress. The rest is looked up with the
    lookup tools."""
    pc = state.pc()
    if pc is None:
        return ""
    lines = [
        f"Campaign {campaign.fields[field_id].name.lower()}: {value}."
        for field_id, value in state.options.items()
        if field_id in campaign.fields
    ]
    if pc.location is None:
        lines.append(
            f"Player character: {pc.name}, not yet placed; place them with move_actor on your first turn."
        )
    else:
        lines.append(f"Player character: {pc.name}, at {pc.location}.")
        names = [npc.name for npc in _npcs_at(state, pc.location)]
        lines.append(f"NPCs here: {', '.join(names) if names else 'none'}.")
    lines.extend(_encounter_lines(state, grid))
    return "\n".join(lines)


def _encounter_lines(state: CampaignState, grid: EncounterGrid | None) -> list[str]:
    view = encounters.encounter_view(state, grid)
    if view is None:
        return []
    lines = [
        f"Encounter in progress on the map {view['map']} at {view['location']}, round {view['round']}.",
        f"Map: {view['columns']} by {view['rows']} cells, columns and rows counted from 0 at the top left. {view['description']}",
    ]
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
        lines.append(f"- {who['number']}. {who['name']} ({kind}), {where}.")
    return lines


def _location_entry(place: Place) -> dict[str, Any]:
    position = None
    if place.position is not None:
        position = {"x": place.position.x, "y": place.position.y}
    return {
        "name": place.name,
        "description": place.description,
        "discovered": place.discovered,
        "position_km": position,
        "parent": place.parent,
    }


def _npcs_at(state: CampaignState, location: str) -> list[Character]:
    return [c for c in state.characters.values() if c.kind == NPC_KIND and c.location == location]


def _npc_summary(npc: Character) -> dict[str, Any]:
    return {
        "name": npc.name,
        "location": npc.location,
        "description": npc.description,
        "personality": npc.personality,
        "accent": npc.accent,
        "quirks": list(npc.quirks),
    }


def _npc_words(npc: Character) -> list[str]:
    return [npc.name, npc.location or "", npc.description, npc.personality, npc.accent, *npc.quirks]


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
) -> NpcAdded:
    """The event that adds an NPC. A trait or character option that is not given is picked at
    random, so the picked values are stored in the event and replays do not roll again."""
    trait_personality = personality or rng.choice(tables["personality"]).value
    trait_accent = accent or rng.choice(tables["accent"]).value
    if quirks is None:
        pool = [option.value for option in tables["quirk"]]
        trait_quirks = rng.sample(pool, min(QUIRKS_PER_NPC, len(pool)))
    else:
        trait_quirks = list(quirks)
    sheet = resolve(spec, character or {}, rng)
    _check_sheet_text(sheet)
    return NpcAdded(
        name=name,
        location=location,
        description=description,
        personality=trait_personality,
        accent=trait_accent,
        quirks=trait_quirks,
        **_sheet_fields(sheet),
    )


def _sheet_fields(sheet: Sheet) -> dict[str, Any]:
    return {
        "stats": dict(sheet.stats),
        "templates": dict(sheet.templates),
        "features": dict(sheet.features),
        "fields": dict(sheet.fields),
    }


def _sheet_of(character: Character) -> Sheet:
    return Sheet(
        stats=character.stats,
        templates=character.templates,
        features=character.features,
        fields=character.fields,
    )


def _check_sheet_text(sheet: Sheet) -> None:
    for value in sheet.fields.values():
        if len(value) > MAX_TEXT:
            raise InvalidInput(f"character text must be at most {MAX_TEXT} characters")


def _ensure_free_name(state: CampaignState, name: str) -> None:
    if state.named(name) is not None:
        raise InvalidInput(f"the name {name!r} is already used by a location or character")


def _place_named(state: CampaignState, name: str) -> Place:
    entry = state.named(name)
    if not isinstance(entry, Place):
        raise InvalidInput(f"there is no location named {name!r}")
    return entry


def _children_of(state: CampaignState, name: str) -> list[str]:
    return sorted(p.name for p in state.places.values() if p.parent == name)


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


def _character_named(state: CampaignState, name: str) -> Character:
    entry = state.named(name)
    if not isinstance(entry, Character):
        raise InvalidInput(f"there is no character named {name!r}")
    return entry


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


def _words(query: str) -> list[str]:
    return bounded_text(query, "query", MAX_TEXT).casefold().split()


def _has_words(words: Sequence[str], *parts: str) -> bool:
    haystack = " ".join(parts).casefold()
    return all(word in haystack for word in words)


def _optional_text(value: object, label: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return bounded_text(value, label, MAX_NAME)


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
