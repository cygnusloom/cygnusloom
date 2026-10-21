"""Encounters: a tactical scene on an encounter map. Participants take turns in the order they
joined, and the rule set's grid says how far a participant may move in one turn.

Like the rest of the world service, each function loads the campaign's state, validates the
change, records its events, and commits."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import EncounterGrid
from cygnusforge.services.common import InvalidInput, bounded_text
from cygnusforge.services.events import (
    NPC_KIND,
    PC_KIND,
    CampaignState,
    Character,
    Encounter,
    EncounterEnded,
    EncounterMap,
    EncounterStarted,
    EncounterTurnBegun,
    EncounterTurnEnded,
    MapCreated,
    MapDeleted,
    MapUpdated,
    ParticipantJoined,
    ParticipantLeft,
    ParticipantPlaced,
    Place,
    load_state,
    name_key,
    record,
    token_labels,
)

MAX_MAP_NAME = 120
MAX_MAP_TEXT = 2000
# The largest number of cells along either side of a map.
MAX_MAP_SIDE = 60


def _size(columns: object, rows: object) -> tuple[int, int]:
    for label, value in (("columns", columns), ("rows", rows)):
        if not _is_int(value) or not 1 <= value <= MAX_MAP_SIDE:
            raise InvalidInput(f"{label} must be a whole number from 1 to {MAX_MAP_SIDE}")
    assert isinstance(columns, int) and isinstance(rows, int)
    return columns, rows


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _map_named(state: CampaignState, name: str) -> EncounterMap:
    entry = state.maps.get(name_key(name))
    if entry is None:
        raise InvalidInput(f"there is no encounter map named {name!r}")
    return entry


def _encounter(state: CampaignState) -> Encounter:
    if state.encounter is None:
        raise InvalidInput("there is no encounter in progress")
    return state.encounter


def _participant(state: CampaignState, name: str) -> Character:
    """A character who is taking part in the encounter in progress."""
    encounter = _encounter(state)
    character = _character(state, name)
    if character.name not in encounter.order:
        raise InvalidInput(f"{character.name} is not in the encounter")
    return character


def _character(state: CampaignState, name: str) -> Character:
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    return character


def _moves_left(encounter: Encounter, grid: EncounterGrid | None) -> int | None:
    if grid is None or encounter.acting is None:
        return None
    return grid.move_cells - encounter.moved


def create_map(
    db: Session,
    campaign_id: int,
    name: str,
    location: str,
    columns: int,
    rows: int,
    description: str,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_MAP_NAME)
    if name_key(name) in state.maps:
        raise InvalidInput(f"there is already an encounter map named {name!r}")
    place = state.named(bounded_text(location, "location", MAX_MAP_NAME))
    if not isinstance(place, Place):
        raise InvalidInput(f"there is no location named {location!r}")
    columns, rows = _size(columns, rows)
    description = bounded_text(description, "description", MAX_MAP_TEXT)
    record(
        db,
        campaign_id,
        MapCreated(
            name=name,
            location=place.name,
            columns=columns,
            rows=rows,
            description=description,
        ),
    )
    db.commit()
    return {
        "created": "encounter map",
        "name": name,
        "location": place.name,
        "columns": columns,
        "rows": rows,
    }


def update_map(
    db: Session,
    campaign_id: int,
    name: str,
    columns: int,
    rows: int,
    description: str,
    new_name: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    entry = _map_named(state, name)
    columns, rows = _size(columns, rows)
    description = bounded_text(description, "description", MAX_MAP_TEXT)
    renamed = ""
    if new_name is not None:
        renamed = bounded_text(new_name, "name", MAX_MAP_NAME)
        if name_key(renamed) != name_key(entry.name) and name_key(renamed) in state.maps:
            raise InvalidInput(f"there is already an encounter map named {renamed!r}")
    moved = ""
    if location is not None:
        place = state.named(bounded_text(location, "location", MAX_MAP_NAME))
        if not isinstance(place, Place):
            raise InvalidInput(f"there is no location named {location!r}")
        moved = place.name
    in_use = state.encounter is not None and name_key(state.encounter.map) == name_key(entry.name)
    if in_use:
        for who, (column, row) in state.encounter.cells.items():
            if column >= columns or row >= rows:
                raise InvalidInput(
                    f"{state.characters[who].name} stands at column {column}, row {row}, "
                    f"outside the new size of {columns} by {rows}"
                )
    record(
        db,
        campaign_id,
        MapUpdated(
            name=entry.name,
            columns=columns,
            rows=rows,
            description=description,
            new_name=renamed,
            location=moved,
        ),
    )
    db.commit()
    return {
        "updated": "encounter map",
        "name": renamed or entry.name,
        "location": moved or entry.location,
        "columns": columns,
        "rows": rows,
    }


def delete_map(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    entry = _map_named(state, name)
    if state.encounter is not None and name_key(state.encounter.map) == name_key(entry.name):
        raise InvalidInput(f"{entry.name} is in use by the encounter; end the encounter first")
    record(db, campaign_id, MapDeleted(name=entry.name))
    db.commit()
    return {"deleted": "encounter map", "name": entry.name}


def start_encounter(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    if state.encounter is not None:
        raise InvalidInput(
            f"an encounter is already in progress at {state.encounter.map}; end it first"
        )
    entry = _map_named(state, name)
    record(db, campaign_id, EncounterStarted(map=entry.name, location=entry.location))
    db.commit()
    _open_player_turn(db, campaign_id)
    return {"started": "encounter", "map": entry.name, "location": entry.location}


def end_encounter(db: Session, campaign_id: int) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    encounter = _encounter(state)
    record(db, campaign_id, EncounterEnded())
    db.commit()
    return {"ended": "encounter", "map": encounter.map}


def add_participant(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    encounter = _encounter(state)
    character = _character(state, name)
    if character.name in encounter.order:
        raise InvalidInput(f"{character.name} is already in the encounter")
    record(db, campaign_id, ParticipantJoined(name=character.name))
    db.commit()
    _open_player_turn(db, campaign_id)
    result: dict[str, Any] = {"added": character.name}
    if character.location != encounter.location:
        where = character.location or "nowhere in particular"
        result["warning"] = (
            f"{character.name} is at {where}, not at {encounter.location}, where the "
            "encounter is. Move them there first if they should be here."
        )
    return result


def remove_participant(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    character = _participant(state, name)
    record(db, campaign_id, ParticipantLeft(name=character.name))
    db.commit()
    _open_player_turn(db, campaign_id)
    return {"removed": character.name}


def place_participant(
    db: Session,
    campaign_id: int,
    name: str,
    column: int,
    row: int,
    grid: EncounterGrid | None,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    encounter = _encounter(state)
    character = _participant(state, name)
    entry = state.maps[name_key(encounter.map)]
    if not (_is_int(column) and _is_int(row)) or not (
        0 <= column < entry.columns and 0 <= row < entry.rows
    ):
        raise InvalidInput(
            f"{character.name} can only go to columns 0 to {entry.columns - 1} and rows 0 to "
            f"{entry.rows - 1} on {entry.name}"
        )
    key = name_key(character.name)
    for other, cell in encounter.cells.items():
        if other != key and cell == (column, row):
            raise InvalidInput(
                f"column {column}, row {row} is taken by {state.characters[other].name}"
            )
    cost = 0
    previous = encounter.cells.get(key)
    if encounter.acting == character.name and previous is not None:
        cost = max(abs(column - previous[0]), abs(row - previous[1]))
        left = _moves_left(encounter, grid)
        if left is not None and cost > left:
            raise InvalidInput(
                f"{character.name} can move {left} more cells this turn, and that is {cost}"
            )
    record(
        db,
        campaign_id,
        ParticipantPlaced(name=character.name, column=column, row=row, cost=cost),
    )
    db.commit()
    after = _moves_left(encounter, grid)
    if after is not None:
        after -= cost
    return {
        "placed": character.name,
        "column": column,
        "row": row,
        "moves_left": after,
    }


def begin_turn(db: Session, campaign_id: int, name: str) -> Character:
    """Start the turn of the NPC who is next in order. The GM plays it out afterwards. A player
    character's turn starts on its own, see `_open_player_turn`."""
    state = load_state(db, campaign_id)
    encounter = _encounter(state)
    if state.turn is not None:
        raise InvalidInput("the GM is still answering; wait for the reply to finish")
    if encounter.acting is not None:
        raise InvalidInput(f"{encounter.acting} is still taking their turn")
    if not encounter.order:
        raise InvalidInput("there is no one in the encounter to take a turn")
    character = state.characters[name_key(encounter.order[encounter.up])]
    if name_key(name) != name_key(character.name):
        raise InvalidInput(f"it is {character.name}'s turn next, not {name}'s")
    if character.kind != NPC_KIND:
        raise InvalidInput(f"{character.name}'s turn starts on its own; send a message to act")
    instruction = f"It is {character.name}'s turn in the encounter. Play out their turn."
    record(db, campaign_id, EncounterTurnBegun(name=character.name, instruction=instruction))
    db.commit()
    return character


def _open_player_turn(db: Session, campaign_id: int) -> None:
    """A player character whose turn is next starts it at once. The player acts by sending a
    message, so nothing has to be pressed first."""
    state = load_state(db, campaign_id)
    encounter = state.encounter
    if encounter is None or encounter.acting is not None or not encounter.order:
        return
    character = state.characters[name_key(encounter.order[encounter.up])]
    if character.kind == PC_KIND:
        record(db, campaign_id, EncounterTurnBegun(name=character.name))
        db.commit()


def end_turn(db: Session, campaign_id: int, name: str, by_player: bool) -> dict[str, Any]:
    """End the running turn. The GM ends NPC turns and the player ends their own character's."""
    state = load_state(db, campaign_id)
    encounter = _encounter(state)
    if encounter.acting is None or name_key(encounter.acting) != name_key(name):
        raise InvalidInput(f"{name} is not taking a turn")
    character = state.characters[name_key(encounter.acting)]
    if (character.kind == PC_KIND) != by_player:
        owner = "the player" if character.kind == PC_KIND else "the GM"
        raise InvalidInput(f"{owner} ends {character.name}'s turn")
    record(db, campaign_id, EncounterTurnEnded(name=character.name))
    db.commit()
    _open_player_turn(db, campaign_id)
    return {"ended_turn": character.name}


def encounter_view(state: CampaignState, grid: EncounterGrid | None) -> dict[str, Any] | None:
    """The encounter in progress as the UI and the GM's orientation text show it."""
    encounter = state.encounter
    if encounter is None:
        return None
    entry = state.maps[name_key(encounter.map)]
    participants = []
    characters = [state.characters[name_key(display)] for display in encounter.order]
    labels = token_labels(character.name for character in characters)
    for number, character in enumerate(characters, start=1):
        acting = encounter.acting == character.name
        participants.append(
            {
                "number": number,
                "label": labels[character.name],
                "name": character.name,
                "is_pc": character.kind == PC_KIND,
                "cell": encounter.cells.get(name_key(character.name)),
                "acting": acting,
                "next": encounter.acting is None
                and encounter.order[encounter.up] == character.name,
                "location": character.location,
            }
        )
    return {
        "map": entry.name,
        "location": encounter.location,
        "description": entry.description,
        "columns": entry.columns,
        "rows": entry.rows,
        "round": encounter.round,
        "acting": encounter.acting,
        "moves_left": _moves_left(encounter, grid),
        "move_cells": grid.move_cells if grid is not None else None,
        "cell_feet": grid.cell_feet if grid is not None else None,
        "participants": participants,
    }
