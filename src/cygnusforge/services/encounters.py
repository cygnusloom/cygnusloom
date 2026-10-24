"""Encounters: a tactical scene on an encounter map. Participants take turns in the order they
joined, and the rule set's grid says how far a participant may move in one turn.

Like the rest of the world service, each function loads the campaign's state, validates the
change, records its events, and commits."""

from __future__ import annotations

import heapq
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import (
    CombatRules,
    EncounterGrid,
    EncounterObject,
    EncounterTemplate,
    EncounterTerrain,
)
from cygnusforge.services.common import InvalidInput, bounded_text
from cygnusforge.services.events import (
    FOES_SIDE,
    NPC_KIND,
    PARTY_SIDE,
    PC_KIND,
    CampaignState,
    CellMark,
    Character,
    Encounter,
    EncounterEnded,
    EncounterMap,
    EncounterStarted,
    EncounterTurnBegun,
    EncounterTurnEnded,
    InitiativeSettled,
    MapCellsChanged,
    MapCreated,
    MapDeleted,
    MapUpdated,
    ParticipantJoined,
    ParticipantLeft,
    ParticipantPlaced,
    Place,
    defeat_status,
    load_state,
    name_key,
    record,
    token_labels,
)

# Given the entries (name, passed the initiative save, or None for an NPC), the order of round one.
InitiativeOrder = Callable[[Sequence[tuple[str, bool | None]]], list[str]]

MAX_MAP_NAME = 120
MAX_MAP_TEXT = 2000
# The largest number of cells along either side of a map.
MAX_MAP_SIDE = 60
# The most cells one set_map_cells call may change, and how many ids an error message lists.
MAX_CELLS_PER_CALL = 200
MAX_LISTED_IDS = 20


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


def _id_list(known: Mapping[str, object]) -> str:
    ids = sorted(known)
    shown = ", ".join(ids[:MAX_LISTED_IDS])
    return shown + (" and more" if len(ids) > MAX_LISTED_IDS else "")


def _id_value(value: object, known: Mapping[str, object], label: str) -> str:
    """A terrain or object id from the rule set. An empty string means none."""
    if not isinstance(value, str):
        raise InvalidInput(f"{label} must be an id given as text")
    if value and value not in known:
        raise InvalidInput(f"there is no {label} {value!r}; the valid ids are {_id_list(known)}")
    return value


def _mark_dicts(marks: Mapping[tuple[int, int], CellMark]) -> list[dict[str, Any]]:
    return [
        {"column": column, "row": row, "terrain": mark.terrain, "object": mark.object}
        for (column, row), mark in sorted(marks.items())
    ]


def _template_named(templates: Mapping[str, EncounterTemplate], template: str) -> EncounterTemplate:
    layout = templates.get(template)
    if layout is None:
        raise InvalidInput(
            f"there is no encounter template {template!r}; the valid ids are {_id_list(templates)}"
        )
    return layout


def _template_marks(
    layout: EncounterTemplate, columns: int, rows: int
) -> dict[tuple[int, int], CellMark]:
    marks: dict[tuple[int, int], CellMark] = {}
    for cell in layout.cells:
        if not (cell.terrain or cell.object):
            continue
        if not (0 <= cell.column < columns and 0 <= cell.row < rows):
            raise InvalidInput(
                f"the {layout.name} template has a cell at column {cell.column}, row {cell.row}, "
                f"which is outside the {columns} by {rows} map"
            )
        marks[(cell.column, cell.row)] = CellMark(cell.terrain, cell.object)
    return marks


def _cell_terrain(entry: EncounterMap, cell: tuple[int, int]) -> str:
    mark = entry.marks.get(cell)
    return mark.terrain if mark and mark.terrain else entry.background


def _blocks(
    entry: EncounterMap, cell: tuple[int, int], objects: Mapping[str, EncounterObject]
) -> str:
    """The object id that blocks a cell, or '' when the cell can be entered."""
    mark = entry.marks.get(cell)
    if mark is None or not mark.object:
        return ""
    found = objects.get(mark.object)
    return mark.object if found is not None and found.blocks else ""


def _step_cost(
    entry: EncounterMap, cell: tuple[int, int], terrain: Mapping[str, EncounterTerrain]
) -> int:
    found = terrain.get(_cell_terrain(entry, cell))
    return 2 if found is not None and found.difficult else 1


def _path_cost(
    entry: EncounterMap,
    start: tuple[int, int],
    goal: tuple[int, int],
    terrain: Mapping[str, EncounterTerrain],
    objects: Mapping[str, EncounterObject],
) -> int | None:
    """The fewest movement points to walk from start to goal in 8 directions, or None when no
    route exists. Blocking cells cannot be entered; entering difficult terrain costs 2."""
    best = {start: 0}
    queue: list[tuple[int, tuple[int, int]]] = [(0, start)]
    while queue:
        spent, cell = heapq.heappop(queue)
        if cell == goal:
            return spent
        if spent > best.get(cell, spent):
            continue
        for d_column in (-1, 0, 1):
            for d_row in (-1, 0, 1):
                nxt = (cell[0] + d_column, cell[1] + d_row)
                if nxt == cell or not (0 <= nxt[0] < entry.columns and 0 <= nxt[1] < entry.rows):
                    continue
                if _blocks(entry, nxt, objects):
                    continue
                total = spent + _step_cost(entry, nxt, terrain)
                if total < best.get(nxt, float("inf")):
                    best[nxt] = total
                    heapq.heappush(queue, (total, nxt))
    return None


def create_map(
    db: Session,
    campaign_id: int,
    name: str,
    location: str,
    columns: int | None = None,
    rows: int | None = None,
    description: str = "",
    template: str = "",
    templates: Mapping[str, EncounterTemplate] | None = None,
) -> dict[str, Any]:
    """Create a map. With a `template`, its size and background come from the template and its
    cells become marks; `columns` and `rows`, if given, override the template size. Without one,
    columns and rows are required and the map starts empty."""
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_MAP_NAME)
    if name_key(name) in state.maps:
        raise InvalidInput(f"there is already an encounter map named {name!r}")
    place = state.named(bounded_text(location, "location", MAX_MAP_NAME))
    if not isinstance(place, Place):
        raise InvalidInput(f"there is no location named {location!r}")
    background = ""
    marks: dict[tuple[int, int], CellMark] = {}
    if template:
        layout = _template_named(templates or {}, template)
        background = layout.background
        columns = layout.columns if columns is None else columns
        rows = layout.rows if rows is None else rows
        columns, rows = _size(columns, rows)
        marks = _template_marks(layout, columns, rows)
    elif columns is None or rows is None:
        raise InvalidInput("columns and rows are required unless a template is given")
    else:
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
            background=background,
            marks=_mark_dicts(marks),
        ),
    )
    db.commit()
    result: dict[str, Any] = {
        "created": "encounter map",
        "name": name,
        "location": place.name,
        "columns": columns,
        "rows": rows,
    }
    if template:
        result["template"] = template
    return result


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


def set_map_cells(
    db: Session,
    campaign_id: int,
    name: str,
    cells: Sequence[Mapping[str, Any]],
    terrain: Mapping[str, EncounterTerrain] | None = None,
    encounter_objects: Mapping[str, EncounterObject] | None = None,
) -> dict[str, Any]:
    """Set the terrain or object on cells of a map. A key left out of a cell keeps its value; an
    empty string clears it."""
    state = load_state(db, campaign_id)
    entry = _map_named(state, name)
    if not isinstance(cells, Sequence) or isinstance(cells, str) or not cells:
        raise InvalidInput("cells must be a non-empty list of cells")
    if len(cells) > MAX_CELLS_PER_CALL:
        raise InvalidInput(f"set at most {MAX_CELLS_PER_CALL} cells in one call")
    terrain = terrain or {}
    encounter_objects = encounter_objects or {}
    marks = dict(entry.marks)
    touched: set[tuple[int, int]] = set()
    for item in cells:
        if not isinstance(item, Mapping):
            raise InvalidInput("each cell must be an object with column, row and terrain or object")
        column, row = item.get("column"), item.get("row")
        for label, value, size in (("column", column, entry.columns), ("row", row, entry.rows)):
            if not _is_int(value) or not 0 <= value < size:
                raise InvalidInput(
                    f"{label} must be a whole number from 0 to {size - 1} on {entry.name}"
                )
        if "terrain" not in item and "object" not in item:
            raise InvalidInput(f"the cell at column {column}, row {row} needs terrain or object")
        cell = (column, row)
        current = marks.get(cell, CellMark())
        new_terrain = current.terrain
        new_object = current.object
        if "terrain" in item:
            new_terrain = _id_value(item["terrain"], terrain, "terrain")
        if "object" in item:
            new_object = _id_value(item["object"], encounter_objects, "encounter object")
        found = encounter_objects.get(new_object) if new_object else None
        on_this_map = state.encounter is not None and (
            name_key(state.encounter.map) == name_key(entry.name)
        )
        if found is not None and found.blocks and on_this_map:
            for key, where in state.encounter.cells.items():
                if where == cell:
                    raise InvalidInput(
                        f"column {column}, row {row} is occupied by "
                        f"{state.characters[key].name}, so it cannot be walled off"
                    )
        marks[cell] = CellMark(new_terrain, new_object)
        touched.add(cell)
    record(
        db,
        campaign_id,
        MapCellsChanged(
            name=entry.name,
            marks=_mark_dicts({cell: marks[cell] for cell in touched}),
        ),
    )
    db.commit()
    return {"map": entry.name, "changed": len(touched)}


def delete_map(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    entry = _map_named(state, name)
    if state.encounter is not None and name_key(state.encounter.map) == name_key(entry.name):
        raise InvalidInput(f"{entry.name} is in use by the encounter; end the encounter first")
    record(db, campaign_id, MapDeleted(name=entry.name))
    db.commit()
    return {"deleted": "encounter map", "name": entry.name}


def start_encounter(
    db: Session, campaign_id: int, name: str, rolls_initiative: bool = False
) -> dict[str, Any]:
    """Start an encounter on a map. With `rolls_initiative`, round one waits for each player
    character to roll for initiative before anyone acts."""
    state = load_state(db, campaign_id)
    if state.encounter is not None:
        raise InvalidInput(
            f"an encounter is already in progress at {state.encounter.map}; end it first"
        )
    entry = _map_named(state, name)
    record(
        db,
        campaign_id,
        EncounterStarted(
            map=entry.name, location=entry.location, rolls_initiative=rolls_initiative
        ),
    )
    db.commit()
    _open_player_turn(db, campaign_id)
    return {"started": "encounter", "map": entry.name, "location": entry.location}


def end_encounter(db: Session, campaign_id: int) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    encounter = _encounter(state)
    record(db, campaign_id, EncounterEnded())
    db.commit()
    return {"ended": "encounter", "map": encounter.map}


def add_participant(
    db: Session, campaign_id: int, name: str, side: str | None = None
) -> dict[str, Any]:
    """Add a character to the encounter on a side. Player characters default to the party and
    other characters to the foes. A side is any name the GM gives it."""
    state = load_state(db, campaign_id)
    encounter = _encounter(state)
    character = _character(state, name)
    if character.name in encounter.order:
        raise InvalidInput(f"{character.name} is already in the encounter")
    if character.story_only:
        raise InvalidInput(
            f"{character.name} has no stats yet; give them stats with update_npc first"
        )
    if side is None:
        side = PARTY_SIDE if character.kind == PC_KIND else FOES_SIDE
    elif not isinstance(side, str) or not side.strip():
        raise InvalidInput("a side needs a name, such as party or foes")
    side = side.strip()
    record(db, campaign_id, ParticipantJoined(name=character.name, side=side))
    db.commit()
    _open_player_turn(db, campaign_id)
    result: dict[str, Any] = {"added": character.name, "side": side}
    if character.location != encounter.location:
        where = character.location or "nowhere in particular"
        result["warning"] = (
            f"{character.name} is at {where}, not at {encounter.location}, where the "
            "encounter is. Move them there first if they should be here."
        )
    return result


def remove_participant(
    db: Session,
    campaign_id: int,
    name: str,
    initiative_order: InitiativeOrder | None = None,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    character = _participant(state, name)
    record(db, campaign_id, ParticipantLeft(name=character.name))
    db.commit()
    if initiative_order is not None:
        settle_initiative(db, campaign_id, initiative_order)
    _open_player_turn(db, campaign_id)
    return {"removed": character.name}


def place_participant(
    db: Session,
    campaign_id: int,
    name: str,
    column: int,
    row: int,
    grid: EncounterGrid | None,
    terrain: Mapping[str, EncounterTerrain] | None = None,
    encounter_objects: Mapping[str, EncounterObject] | None = None,
) -> dict[str, Any]:
    """Move a participant to a cell. Obstacles block; difficult terrain costs two moves per cell
    entered. Without marks or a background, the cost is the plain distance in cells."""
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
    target = (column, row)
    blocker = _blocks(entry, target, encounter_objects or {})
    if blocker:
        raise InvalidInput(f"column {column}, row {row} is blocked by {blocker} on {entry.name}")
    cost = 0
    previous = encounter.cells.get(key)
    if previous is not None:
        # A route around obstacles is required at any time, but moves are charged only on the
        # participant's own turn.
        acting = encounter.acting == character.name
        left = _moves_left(encounter, grid) if acting else None
        if entry.marks or entry.background:
            route = _path_cost(entry, previous, target, terrain or {}, encounter_objects or {})
            if route is None:
                raise InvalidInput(
                    f"{character.name} has no way to column {column}, row {row}: it is blocked"
                )
            too_far = (
                f"{character.name} cannot reach column {column}, row {row} this turn: the way is "
                f"too far, it costs {route} and {left} moves are left"
            )
        else:
            route = max(abs(column - previous[0]), abs(row - previous[1]))
            too_far = f"{character.name} can move {left} more cells this turn, and that is {route}"
        if acting:
            cost = route
            if left is not None and cost > left:
                raise InvalidInput(too_far)
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
        "cost": cost,
        "moves_left": after,
    }


def _initiative_window(encounter: Encounter) -> bool:
    """Whether round one is still waiting for its initiative rolls. Once a turn has begun, the
    order is fixed, so a participant who joins later does not roll."""
    return (
        encounter.rolls_initiative
        and not encounter.settled
        and encounter.round == 1
        and encounter.up == 0
        and encounter.acting is None
    )


def awaiting_initiative(state: CampaignState) -> list[str]:
    """The display names of the player characters who must still roll for initiative."""
    encounter = state.encounter
    if encounter is None or not _initiative_window(encounter):
        return []
    return [
        name
        for name in encounter.order
        if state.characters[name_key(name)].kind == PC_KIND
        and name_key(name) not in encounter.initiative
    ]


def initiative_open(state: CampaignState) -> bool:
    """Whether no one may act until the player characters have rolled for initiative."""
    return bool(awaiting_initiative(state))


def settle_initiative(db: Session, campaign_id: int, initiative_order: InitiativeOrder) -> None:
    """Set round one's order once every player character has rolled. Nothing happens while a roll
    is still owed, or when no player character is taking part."""
    state = load_state(db, campaign_id)
    encounter = state.encounter
    if encounter is None or not _initiative_window(encounter) or awaiting_initiative(state):
        return
    entries: list[tuple[str, bool | None]] = []
    for name in encounter.order:
        key = name_key(name)
        if state.characters[key].kind == PC_KIND:
            entries.append((name, encounter.initiative[key]))
        else:
            entries.append((name, None))
    if not any(passed is not None for _, passed in entries):
        return
    record(db, campaign_id, InitiativeSettled(order=initiative_order(entries)))
    db.commit()
    _open_player_turn(db, campaign_id)


def begin_turn(db: Session, campaign_id: int, name: str) -> Character:
    """Start the turn of the NPC who is next in order. The GM plays it out afterwards. A player
    character's turn starts on its own, see `_open_player_turn`."""
    state = load_state(db, campaign_id)
    encounter = _encounter(state)
    if initiative_open(state):
        waiting = ", ".join(awaiting_initiative(state))
        raise InvalidInput(f"initiative is not set yet; {waiting} must roll for it first")
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
    if initiative_open(state):
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


def _status(character: Character, combat: CombatRules | None) -> str:
    if character.defeated:
        return defeat_status(character, combat.defeat if combat else {})
    if character.fled:
        return "fled"
    if character.surrendered:
        return "surrendered"
    if character.save_due is not None:
        return f"{character.save_due} save due"
    return ""


def _creature_label(character: Character) -> str:
    """The statblock to show beside a combatant, or nothing when it would repeat the name."""
    if name_key(character.creature) == name_key(character.name):
        return ""
    return character.creature


def _terrain_view(
    identifier: str, terrain: Mapping[str, EncounterTerrain]
) -> dict[str, Any] | None:
    if not identifier:
        return None
    found = terrain.get(identifier)
    return {
        "id": identifier,
        "name": found.name if found is not None else identifier,
        "color": found.color if found is not None else "",
        "difficult": found.difficult if found is not None else False,
    }


def _marked_cells(
    entry: EncounterMap,
    terrain: Mapping[str, EncounterTerrain],
    objects: Mapping[str, EncounterObject],
) -> list[dict[str, Any]]:
    cells = []
    for (column, row), mark in sorted(entry.marks.items()):
        found_object = objects.get(mark.object)
        cells.append(
            {
                "column": column,
                "row": row,
                "terrain": terrain[mark.terrain].name if mark.terrain in terrain else mark.terrain,
                "terrain_color": terrain[mark.terrain].color if mark.terrain in terrain else "",
                "object": found_object.name if found_object is not None else mark.object,
                "object_icon": found_object.icon if found_object is not None else "",
                "object_color": found_object.color if found_object is not None else "",
                "object_solid": bool(found_object is not None and found_object.solid),
                "blocks": bool(found_object is not None and found_object.blocks),
                "difficult": bool(mark.terrain in terrain and terrain[mark.terrain].difficult),
            }
        )
    return cells


def encounter_view(
    state: CampaignState,
    grid: EncounterGrid | None,
    combat: CombatRules | None,
    terrain: Mapping[str, EncounterTerrain] | None = None,
    encounter_objects: Mapping[str, EncounterObject] | None = None,
) -> dict[str, Any] | None:
    """The encounter in progress as the UI and the GM's orientation text show it."""
    encounter = state.encounter
    if encounter is None:
        return None
    entry = state.maps[name_key(encounter.map)]
    terrain = terrain or {}
    encounter_objects = encounter_objects or {}
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
                "creature": _creature_label(character),
                "cell": encounter.cells.get(name_key(character.name)),
                "acting": acting,
                "next": encounter.acting is None
                and encounter.order[encounter.up] == character.name,
                "location": character.location,
                "side": encounter.sides.get(name_key(character.name), ""),
                "initiative": encounter.initiative.get(name_key(character.name)),
                "defeated": character.defeated,
                "fled": character.fled,
                "status": _status(character, combat),
            }
        )
    return {
        "map": entry.name,
        "location": encounter.location,
        "description": entry.description,
        "columns": entry.columns,
        "rows": entry.rows,
        "background": _terrain_view(entry.background, terrain),
        "marked_cells": _marked_cells(entry, terrain, encounter_objects),
        "round": encounter.round,
        "acting": encounter.acting,
        "awaiting_initiative": awaiting_initiative(state),
        "moves_left": _moves_left(encounter, grid),
        "move_cells": grid.move_cells if grid is not None else None,
        "cell_feet": grid.cell_feet if grid is not None else None,
        "participants": participants,
    }
