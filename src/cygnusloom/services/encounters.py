"""Encounters: a tactical scene on an encounter map. Participants take turns in the order they
joined, and the rule set's grid says how far a participant may move in one turn.

Like the rest of the world service, each function loads the campaign's state, validates the
change, records its events, and commits."""

from __future__ import annotations

import heapq
import random
import re
from collections.abc import Callable, Mapping, Sequence
from difflib import get_close_matches
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.definitions import ContentError
from cygnusloom.content.icons import check_icon
from cygnusloom.content.library import (
    MAP_AMBIENTS,
    MAP_PALETTES,
    TERRAIN_PATTERNS,
    CombatRules,
    EncounterGrid,
    EncounterObject,
    EncounterTemplate,
    EncounterTerrain,
    InitiativeRules,
)
from cygnusloom.services import death, resolution
from cygnusloom.services.common import InvalidInput, bounded_text
from cygnusloom.services.events import (
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
    MapExtended,
    MapStyleChanged,
    MapTokenPlaced,
    MapTokenRemoved,
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
# A cell label is a short caption for the tooltip.
MAX_CELL_LABEL = 60

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _hex_color(value: object, label: str) -> str:
    """A hex colour, or "" when none is given."""
    if value == "":
        return ""
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
        raise InvalidInput(f"{label} must be a hex colour like #3a3f47, not {value!r}")
    return value


def _style_id(value: object, allowed: Sequence[str], label: str) -> str:
    """A palette or ambient id, or "" when none is given."""
    if value == "":
        return ""
    if not isinstance(value, str) or value not in allowed:
        raise InvalidInput(f"{label} must be one of {', '.join(allowed)}, not {value!r}")
    return value


def _cell_label(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidInput("label must be text")
    text = value.strip()
    if len(text) > MAX_CELL_LABEL:
        raise InvalidInput(f"label must be at most {MAX_CELL_LABEL} characters")
    return text


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
    if entry is None and not name_key(name).endswith(" local map"):
        # Local maps are commonly authored with a suffix, while the GM often names the place.
        entry = state.maps.get(name_key(f"{name} local map"))
    if entry is None:
        names = [encounter_map.name for encounter_map in state.maps.values()]
        hint = get_close_matches(name, names, n=3, cutoff=0.6)
        extra = f"; did you mean {', '.join(repr(option) for option in hint)}?" if hint else ""
        raise InvalidInput(f"there is no encounter map named {name!r}{extra}")
    return entry


def _map_space(
    entry: EncounterMap,
    cells: Mapping[str, tuple[int, int]],
    objects: Mapping[str, EncounterObject],
) -> dict[str, Any]:
    """How big a map is and how much of it is still free, so a placement result tells the GM
    where the next token can go."""
    occupied = set(cells.values())
    free = 0
    for column in range(entry.columns):
        for row in range(entry.rows):
            if (column, row) in occupied:
                continue
            if _blocks(entry, (column, row), objects):
                continue
            free += 1
    return {
        "columns": entry.columns,
        "rows": entry.rows,
        "free_cells": free,
        "occupied": len(occupied),
    }


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
        {
            "column": column,
            "row": row,
            "terrain": mark.terrain,
            "object": mark.object,
            "color": mark.color,
            "pattern": mark.pattern,
            "icon": mark.icon,
            "label": mark.label,
        }
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
    return found.move_cost if found is not None else 1


def _path_cost(
    entry: EncounterMap,
    start: tuple[int, int],
    goal: tuple[int, int],
    terrain: Mapping[str, EncounterTerrain],
    objects: Mapping[str, EncounterObject],
) -> int | None:
    """The fewest movement points to walk from start to goal in 8 directions, or None when no
    route exists. Blocking cells cannot be entered; each cell entered costs its terrain's move_cost."""
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
    palette: str = "",
    ambient: str = "",
    background_color: str = "",
    grid_color: str = "",
    *,
    state: CampaignState | None = None,
) -> dict[str, Any]:
    """Create a map at a location. A map is not necessarily an encounter: outside one it is a
    local map that shows the player character, NPCs, and props where they stand. With a
    `template`, its size, background, and look come from the template and its cells become
    marks; `columns` and `rows`, if given, override the template size, and a palette, ambient,
    or colour the caller gives overrides the template's. Without a template, columns and rows
    are required and the map starts empty. A location may have several maps; the newest is its
    local map, and an encounter can start on any of them.

    A caller that is already holding the campaign state, and keeping it current, can pass it
    as `state`; otherwise the state is loaded from the log."""
    if state is None:
        state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_MAP_NAME)
    existing = state.maps.get(name_key(name))
    if existing is not None:
        raise InvalidInput(
            f"there is already a map named {existing.name!r} at {existing.location!r}; "
            "use update_local_map to change it, or choose another name"
        )
    place = state.named(bounded_text(location, "location", MAX_MAP_NAME))
    if not isinstance(place, Place):
        raise InvalidInput(f"there is no location named {location!r}")
    chosen_palette = _style_id(palette, MAP_PALETTES, "palette")
    chosen_ambient = _style_id(ambient, MAP_AMBIENTS, "ambient")
    chosen_background = _hex_color(background_color, "background_color")
    chosen_grid = _hex_color(grid_color, "grid_color")
    background = ""
    marks: dict[tuple[int, int], CellMark] = {}
    if template:
        layout = _template_named(templates or {}, template)
        background = layout.background
        chosen_palette = chosen_palette or layout.palette
        chosen_ambient = chosen_ambient or layout.ambient
        chosen_background = chosen_background or layout.background_color
        chosen_grid = chosen_grid or layout.grid_color
        columns = layout.columns if columns is None else columns
        rows = layout.rows if rows is None else rows
        columns, rows = _size(columns, rows)
        marks = _template_marks(layout, columns, rows)
    elif columns is None or rows is None:
        raise InvalidInput("columns and rows are required unless a template is given")
    else:
        columns, rows = _size(columns, rows)
    description = bounded_text(description, "description", MAX_MAP_TEXT)
    event = MapCreated(
        name=name,
        location=place.name,
        columns=columns,
        rows=rows,
        description=description,
        background=background,
        palette=chosen_palette,
        ambient=chosen_ambient,
        background_color=chosen_background,
        grid_color=chosen_grid,
        marks=_mark_dicts(marks),
    )
    event.apply(state, record(db, campaign_id, event))
    db.commit()
    result: dict[str, Any] = {
        "created": "encounter map",
        "name": name,
        "location": place.name,
        "columns": columns,
        "rows": rows,
        "palette": chosen_palette,
        "ambient": chosen_ambient,
        "background_color": chosen_background,
        "grid_color": chosen_grid,
    }
    if template:
        result["template"] = template
    return result


def _added(value: object, label: str) -> int:
    """How many cells an extension adds; missing or 0 means none."""
    if value is None:
        return 0
    if not _is_int(value) or value < 0:
        raise InvalidInput(f"{label} must be a whole number of cells, at least 0")
    return value


def update_map(
    db: Session,
    campaign_id: int,
    name: str,
    columns: int | None = None,
    rows: int | None = None,
    description: str | None = None,
    new_name: str | None = None,
    location: str | None = None,
    *,
    palette: str | None = None,
    ambient: str | None = None,
    background: str | None = None,
    background_color: str | None = None,
    grid_color: str | None = None,
    extend_left: int = 0,
    extend_right: int = 0,
    extend_up: int = 0,
    extend_down: int = 0,
    terrain: Mapping[str, EncounterTerrain] | None = None,
) -> dict[str, Any]:
    """Change a map's size, name, location, description, or look, and extend it on any side.

    An extension shifts every mark, token, and encounter cell by the cells added to the left or
    top, so nothing is lost. A shrink crops what falls outside and reports how many marks and
    tokens were dropped. Style changes record their own event so replay keeps the look."""
    state = load_state(db, campaign_id)
    entry = _map_named(state, name)
    base_columns = entry.columns if columns is None else columns
    base_rows = entry.rows if rows is None else rows
    base_columns, base_rows = _size(base_columns, base_rows)
    left = _added(extend_left, "extend_left")
    right = _added(extend_right, "extend_right")
    up = _added(extend_up, "extend_up")
    down = _added(extend_down, "extend_down")
    new_columns, new_rows = _size(base_columns + left + right, base_rows + up + down)
    text = (
        entry.description
        if description is None
        else bounded_text(description, "description", MAX_MAP_TEXT)
    )
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
    new_background = (
        entry.background if background is None else _id_value(background, terrain or {}, "terrain")
    )
    new_palette = entry.palette if palette is None else _style_id(palette, MAP_PALETTES, "palette")
    new_ambient = entry.ambient if ambient is None else _style_id(ambient, MAP_AMBIENTS, "ambient")
    new_background_color = (
        entry.background_color
        if background_color is None
        else _hex_color(background_color, "background_color")
    )
    new_grid_color = (
        entry.grid_color if grid_color is None else _hex_color(grid_color, "grid_color")
    )
    in_use = state.encounter is not None and name_key(state.encounter.map) == name_key(entry.name)
    if in_use:
        for who, (column, row) in state.encounter.cells.items():
            new_column, new_row = column + left, row + up
            if new_column >= new_columns or new_row >= new_rows:
                raise InvalidInput(
                    f"{state.characters[who].name} stands at column {column}, row {row}, "
                    f"outside the new size of {new_columns} by {new_rows}"
                )
    dropped_marks = sum(
        1
        for (column, row) in entry.marks
        if not (0 <= column + left < new_columns and 0 <= row + up < new_rows)
    )
    dropped_tokens = sum(
        1
        for (column, row) in entry.tokens.values()
        if not (0 <= column + left < new_columns and 0 <= row + up < new_rows)
    )
    extended = bool(left or right or up or down)
    if extended:
        record(
            db,
            campaign_id,
            MapExtended(
                name=entry.name,
                columns=new_columns,
                rows=new_rows,
                offset_column=left,
                offset_row=up,
            ),
        )
    styled = (
        new_background,
        new_palette,
        new_ambient,
        new_background_color,
        new_grid_color,
    ) != (
        entry.background,
        entry.palette,
        entry.ambient,
        entry.background_color,
        entry.grid_color,
    )
    if styled:
        record(
            db,
            campaign_id,
            MapStyleChanged(
                name=entry.name,
                background=new_background,
                background_color=new_background_color,
                grid_color=new_grid_color,
                palette=new_palette,
                ambient=new_ambient,
            ),
        )
    resized = (new_columns, new_rows) != (entry.columns, entry.rows)
    if resized and not extended or renamed or moved or description is not None:
        record(
            db,
            campaign_id,
            MapUpdated(
                name=entry.name,
                columns=new_columns,
                rows=new_rows,
                description=text,
                new_name=renamed,
                location=moved,
            ),
        )
    db.commit()
    result: dict[str, Any] = {
        "updated": "encounter map",
        "name": renamed or entry.name,
        "location": moved or entry.location,
        "columns": new_columns,
        "rows": new_rows,
    }
    if dropped_marks:
        result["dropped_marks"] = dropped_marks
    if dropped_tokens:
        result["dropped_tokens"] = dropped_tokens
    return result


def set_map_cells(
    db: Session,
    campaign_id: int,
    name: str,
    cells: Sequence[Mapping[str, Any]],
    terrain: Mapping[str, EncounterTerrain] | None = None,
    encounter_objects: Mapping[str, EncounterObject] | None = None,
) -> dict[str, Any]:
    """Set a cell's terrain, object, colour, pattern, icon, or label. A key left out of a cell
    keeps its value; an empty string clears it. A cell with every field empty is removed."""
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
    fields = ("terrain", "object", "color", "pattern", "icon", "label")
    for item in cells:
        if not isinstance(item, Mapping):
            raise InvalidInput(
                "each cell must be an object with column, row and a terrain, object, color, "
                "pattern, icon, or label"
            )
        column, row = item.get("column"), item.get("row")
        for label, value, size in (("column", column, entry.columns), ("row", row, entry.rows)):
            if not _is_int(value) or not 0 <= value < size:
                raise InvalidInput(
                    f"{label} must be a whole number from 0 to {size - 1} on {entry.name}"
                )
        if not any(field in item for field in fields):
            raise InvalidInput(
                f"the cell at column {column}, row {row} needs a terrain, object, color, "
                "pattern, icon, or label"
            )
        cell = (column, row)
        current = marks.get(cell, CellMark())
        new_terrain = current.terrain
        new_object = current.object
        new_color = current.color
        new_pattern = current.pattern
        new_icon = current.icon
        new_label = current.label
        if "terrain" in item:
            new_terrain = _id_value(item["terrain"], terrain, "terrain")
        if "object" in item:
            new_object = _id_value(item["object"], encounter_objects, "encounter object")
        if "color" in item:
            new_color = _hex_color(item["color"], "cell color")
        if "pattern" in item:
            pattern = item["pattern"]
            if not isinstance(pattern, str):
                raise InvalidInput("cell pattern must be an id given as text")
            if pattern and pattern not in TERRAIN_PATTERNS:
                raise InvalidInput(
                    f"there is no pattern {pattern!r}; the valid ids are "
                    f"{_id_list({name: name for name in TERRAIN_PATTERNS})}"
                )
            new_pattern = pattern
        if "icon" in item:
            icon = item["icon"]
            if not isinstance(icon, str):
                raise InvalidInput("cell icon must be an id given as text")
            if icon:
                try:
                    check_icon(icon, f"the cell at column {column}, row {row}")
                except ContentError as exc:
                    raise InvalidInput(str(exc)) from exc
            new_icon = icon
        if "label" in item:
            new_label = _cell_label(item["label"])
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
        marks[cell] = CellMark(new_terrain, new_object, new_color, new_pattern, new_icon, new_label)
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


def _token_name(key: str) -> str:
    """A readable name for a token that is not a character, from its name key."""
    return " ".join(word.capitalize() for word in key.replace("_", " ").split())


def _in_encounter_on(state: CampaignState, entry: EncounterMap) -> bool:
    """Whether the encounter in progress is using this map, so its cells are the only positions."""
    return state.encounter is not None and name_key(state.encounter.map) == name_key(entry.name)


def _arrival_cell(
    entry: EncounterMap,
    objects: Mapping[str, EncounterObject],
    occupied: set[tuple[int, int]],
) -> tuple[int, int] | None:
    """A free, enterable cell near the middle of the map, so an arrival starts in the room."""
    candidates = [
        (column, row)
        for row in range(entry.rows)
        for column in range(entry.columns)
        if (column, row) not in occupied and not _blocks(entry, (column, row), objects)
    ]
    if not candidates:
        return None
    center_column = (entry.columns - 1) / 2
    center_row = (entry.rows - 1) / 2
    return min(
        candidates,
        key=lambda cell: (
            (cell[0] - center_column) ** 2 + (cell[1] - center_row) ** 2,
            cell[1],
            cell[0],
        ),
    )


def place_arrivals(
    db: Session,
    campaign_id: int,
    names: Sequence[str],
    encounter_objects: Mapping[str, EncounterObject],
) -> list[dict[str, Any]]:
    """Give characters who just moved somewhere a cell on their new place's local map.

    The move tools already know where a character went. Requiring a second tool call to place
    the token let an arrival stay "not yet placed" even though the engine knew the destination.
    An existing token is left alone, and an encounter keeps its own participant cells.
    """
    state = load_state(db, campaign_id)
    placed: list[dict[str, Any]] = []
    for name in names:
        character = state.characters.get(name_key(name))
        if character is None or character.location is None:
            continue
        entry = _local_map_for(state, character.location)
        if entry is None or _in_encounter_on(state, entry):
            continue
        key = name_key(character.name)
        if key in entry.tokens:
            continue
        cell = _arrival_cell(entry, encounter_objects, set(entry.tokens.values()))
        if cell is None:
            continue
        event = MapTokenPlaced(
            map=entry.name,
            name=character.name,
            column=cell[0],
            row=cell[1],
            quiet=character.kind != PC_KIND,
        )
        event.apply(state, record(db, campaign_id, event))
        placed.append(
            {
                "name": character.name,
                "map": entry.name,
                "column": cell[0],
                "row": cell[1],
            }
        )
    return placed


def place_map_token(
    db: Session,
    campaign_id: int,
    map_name: str,
    name: str,
    column: int,
    row: int,
    encounter_objects: Mapping[str, EncounterObject] | None = None,
    *,
    state: CampaignState | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Place a token on a local map outside an encounter. A token may be a character, an NPC, or
    a prop; a character must be at the map's location. During an encounter, positions belong to
    `Encounter.cells`, so it refuses and `place_participant` is used instead.

    The cell must be on the map and free: a blocking object or another token cannot share it.
    Placing a token again moves it. The player's Log says where the player character went; an
    NPC's or prop's move is left to the narration. A caller that is already holding the campaign
    state, and keeping it current, can pass it as `state`; otherwise the state is loaded from the
    log. Seeding a world's starting tokens passes `quiet` so setup does not fill the transcript."""
    if state is None:
        state = load_state(db, campaign_id)
    entry = _map_named(state, map_name)
    if _in_encounter_on(state, entry):
        raise InvalidInput(
            f"an encounter is in progress on {entry.name}; "
            "use place_participant to place a character in the fight"
        )
    name = bounded_text(name, "name", MAX_MAP_NAME)
    key = name_key(name)
    character = state.characters.get(key)
    if character is not None:
        if character.location is None:
            raise InvalidInput(
                f"{character.name} is not at any location; move them to {entry.location} first"
            )
        if not _inside_place(state, character.location, entry.location):
            raise InvalidInput(
                f"{character.name} is at {character.location}, not at {entry.location} or "
                f"inside it, where {entry.name} is"
            )
    if not (_is_int(column) and _is_int(row)) or not (
        0 <= column < entry.columns and 0 <= row < entry.rows
    ):
        raise InvalidInput(
            f"a token can only go to columns 0 to {entry.columns - 1} and rows 0 to "
            f"{entry.rows - 1} on {entry.name}"
        )
    target = (column, row)
    blocker = _blocks(entry, target, encounter_objects or {})
    if blocker:
        raise InvalidInput(f"column {column}, row {row} is blocked by {blocker} on {entry.name}")
    for other, cell in entry.tokens.items():
        if other != key and cell == target:
            raise InvalidInput(
                f"column {column}, row {row} is taken by {_token_name(other)} on {entry.name}"
            )
    display = character.name if character is not None else name
    previous = entry.tokens.get(key)
    moved = previous is not None and previous != target
    quiet = quiet or character is None or character.kind != PC_KIND
    event = MapTokenPlaced(
        map=entry.name,
        name=display,
        column=column,
        row=row,
        from_column=previous[0] if moved else None,
        from_row=previous[1] if moved else None,
        quiet=quiet,
    )
    event.apply(state, record(db, campaign_id, event))
    db.commit()
    result: dict[str, Any] = {
        "placed": display,
        "map": entry.name,
        "column": column,
        "row": row,
    }
    if previous is not None and previous != target:
        result["moved_from"] = {"column": previous[0], "row": previous[1]}
    result.update(_map_space(entry, entry.tokens, encounter_objects or {}))
    return result


def remove_map_token(db: Session, campaign_id: int, map_name: str, name: str) -> dict[str, Any]:
    """Take a token off a local map outside an encounter."""
    state = load_state(db, campaign_id)
    entry = _map_named(state, map_name)
    if _in_encounter_on(state, entry):
        raise InvalidInput(
            f"an encounter is in progress on {entry.name}; "
            "use remove_participant to take a character out of the fight"
        )
    name = bounded_text(name, "name", MAX_MAP_NAME)
    key = name_key(name)
    if key not in entry.tokens:
        raise InvalidInput(f"there is no token named {name!r} on {entry.name}")
    character = state.characters.get(key)
    display = character.name if character is not None else name
    record(db, campaign_id, MapTokenRemoved(map=entry.name, name=display))
    db.commit()
    return {"removed": display, "map": entry.name}


def start_encounter(
    db: Session,
    campaign_id: int,
    name: str,
    rolls_initiative: bool = False,
    *,
    combat: CombatRules | None = None,
    character: CharacterSpec | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Start an encounter on a map. With `rolls_initiative`, round one waits for each player
    character to roll for initiative before anyone acts.

    A rule set whose combat names side initiative never waits for individual rolls: `combat`
    tells this function so, and the side roll is made with `roll_side_initiative` once the
    participants have joined. Until then the order is the order they joined, and nobody is
    blocked from acting."""
    state = load_state(db, campaign_id)
    if state.encounter is not None:
        raise InvalidInput(
            f"an encounter is already in progress at {state.encounter.map}; end it first"
        )
    entry = _map_named(state, name)
    sides = side_initiative(combat)
    record(
        db,
        campaign_id,
        EncounterStarted(
            map=entry.name,
            location=entry.location,
            rolls_initiative=rolls_initiative and sides is None,
        ),
    )
    db.commit()
    if sides is not None and load_state(db, campaign_id).encounter.order:
        roll_side_initiative(db, campaign_id, combat, character, rng)
    result: dict[str, Any] = {
        "started": "encounter",
        "map": entry.name,
        "location": entry.location,
    }
    if sides is not None:
        result["initiative"] = "sides"
    return result


def side_initiative(combat: CombatRules | None) -> InitiativeRules | None:
    """The rule set's side-based initiative, or None when each character rolls for themselves."""
    if combat is None or combat.initiative is None or not combat.initiative.side:
        return None
    return combat.initiative


def _initiative_bonus(member: Character, rules: InitiativeRules, spec: CharacterSpec | None) -> int:
    """A character's modifier in the initiative stat, using the rule set's bands. Zero when the
    rule set was not given or the character has no score in the stat."""
    if spec is None or not rules.bonus_stat:
        return 0
    stat = spec.stats.get(rules.bonus_stat)
    score = member.stats.get(rules.bonus_stat)
    if stat is None or score is None:
        return 0
    return stat.modifier(score)


def _side_of(encounter: Encounter, member: Character) -> str:
    """The side a participant fights on, or the default for their kind when they joined without one."""
    side = encounter.sides.get(name_key(member.name))
    if side:
        return side
    return PARTY_SIDE if member.kind == PC_KIND else FOES_SIDE


def _side_rolls(
    state: CampaignState,
    encounter: Encounter,
    rules: InitiativeRules,
    spec: CharacterSpec | None,
    rng: random.Random | None,
) -> list[dict[str, Any]]:
    """One roll for each side, in the order its first member joined. A side's bonus is the best
    `bonus_stat` modifier among its members."""
    sides: dict[str, list[Character]] = {}
    for name in encounter.order:
        member = state.characters[name_key(name)]
        sides.setdefault(_side_of(encounter, member), []).append(member)
    rolls = []
    for side, members in sides.items():
        bonus = max((_initiative_bonus(member, rules, spec) for member in members), default=0)
        roll = resolution.roll_die(rng or random.Random(), rules.die)
        rolls.append(
            {
                "side": side,
                "roll": roll,
                "bonus": bonus,
                "total": roll + bonus,
                "members": [member.name for member in members],
            }
        )
    return rolls


def _roll_order(rolls: list[dict[str, Any]], tie: str) -> list[dict[str, Any]]:
    """The sides from highest total to lowest. A tie goes to the party when `tie` is "party",
    and to the other side when it is "foes"; otherwise the order they joined breaks it."""
    party_first = tie == "party"

    def key(entry: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        index, roll = entry
        party = roll["side"] == PARTY_SIDE
        return (-roll["total"], 0 if party == party_first else 1, index)

    return [roll for _, roll in sorted(enumerate(rolls), key=key)]


def roll_side_initiative(
    db: Session,
    campaign_id: int,
    combat: CombatRules | None,
    character: CharacterSpec | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Roll once for each side and set round one's order. The side with the highest total acts
    first, ties go the way the rule set says, and each side adds the best `bonus_stat` modifier
    among its members. No player character rolls a save of their own. Call it once the
    participants have joined; until then they act in the order they joined."""
    rules = side_initiative(combat)
    if rules is None:
        raise InvalidInput("this rule set rolls initiative for each character, not for sides")
    state = load_state(db, campaign_id)
    encounter = _encounter(state)
    if not encounter.order:
        raise InvalidInput("there is no one in the encounter to roll initiative")
    if encounter.settled:
        raise InvalidInput("initiative is already set for this encounter")
    if encounter.round > 1:
        raise InvalidInput("initiative is rolled only at the start of an encounter")
    rolls = _side_rolls(state, encounter, rules, character, rng)
    order = [name for entry in _roll_order(rolls, rules.tie) for name in entry["members"]]
    record(db, campaign_id, InitiativeSettled(order=order))
    db.commit()
    return {"initiative": "sides", "rolls": rolls, "order": order}


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
    entry = state.maps.get(name_key(encounter.map))
    token = entry.tokens.get(name_key(character.name)) if entry is not None else None
    record(db, campaign_id, ParticipantJoined(name=character.name, side=side))
    # A local map's token cell becomes the participant's cell, so a fight that starts on the
    # map does not need everyone placed again. The token stays on the map for when it is over.
    if token is not None and token not in encounter.cells.values():
        record(
            db,
            campaign_id,
            ParticipantPlaced(name=character.name, column=token[0], row=token[1], cost=0),
        )
    db.commit()
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
    cells = dict(encounter.cells)
    cells[name_key(character.name)] = (column, row)
    result: dict[str, Any] = {
        "placed": character.name,
        "map": entry.name,
        "column": column,
        "row": row,
        "cost": cost,
        "moves_left": after,
    }
    if previous is not None and previous != target:
        result["moved_from"] = {"column": previous[0], "row": previous[1]}
    result.update(_map_space(entry, cells, encounter_objects or {}))
    return result


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


def begin_turn(db: Session, campaign_id: int, name: str) -> Character:
    """Start the turn of the character who is next in order. An NPC's turn opens a GM reply that
    plays it out; a player character's turn opens nothing and waits for the player."""
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
    instruction = ""
    if character.kind == NPC_KIND:
        instruction = f"It is {character.name}'s turn in the encounter. Play out their turn."
    record(db, campaign_id, EncounterTurnBegun(name=character.name, instruction=instruction))
    db.commit()
    return character


def begin_player_turn_if_next(db: Session, campaign_id: int) -> Character | None:
    """Start the next turn when it belongs to a player character.

    A player character's turn opens no GM reply, so there is nothing for the player to start: they
    act, then press End your turn. NPC turns still wait for the player to press Start, because the
    GM cannot begin replying until the player asks it to. Does nothing while a reply is open, when
    someone is already acting, or when initiative is still owed."""
    state = load_state(db, campaign_id)
    encounter = state.encounter
    if encounter is None or state.turn is not None or encounter.acting is not None:
        return None
    if initiative_open(state) or not encounter.order:
        return None
    upcoming = state.characters[name_key(encounter.order[encounter.up])]
    if upcoming.kind != PC_KIND:
        return None
    return begin_turn(db, campaign_id, upcoming.name)


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
    round_before = encounter.round
    record(db, campaign_id, EncounterTurnEnded(name=character.name))
    db.commit()
    state = load_state(db, campaign_id)
    if state.encounter is not None and state.encounter.round > round_before:
        death.tick_dying_rounds(db, campaign_id)
    # The next player character's turn needs no button press; an NPC's still does.
    begin_player_turn_if_next(db, campaign_id)
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
        "rule": found.rule if found is not None else "",
        "difficult": found.difficult if found is not None else False,
        "pattern": (found.pattern or "") if found is not None else "",
    }


def _marked_cells(
    entry: EncounterMap,
    terrain: Mapping[str, EncounterTerrain],
    objects: Mapping[str, EncounterObject],
) -> list[dict[str, Any]]:
    cells = []
    for (column, row), mark in sorted(entry.marks.items()):
        found_object = objects.get(mark.object)
        found_terrain = terrain.get(mark.terrain)
        cells.append(
            {
                "column": column,
                "row": row,
                "terrain": found_terrain.name if found_terrain is not None else mark.terrain,
                "terrain_color": found_terrain.color if found_terrain is not None else "",
                "terrain_rule": found_terrain.rule if found_terrain is not None else "",
                "terrain_pattern": (found_terrain.pattern or "")
                if found_terrain is not None
                else "",
                "object": found_object.name if found_object is not None else mark.object,
                "object_icon": found_object.icon if found_object is not None else "",
                "object_color": found_object.color if found_object is not None else "",
                "object_solid": bool(found_object is not None and found_object.solid),
                "blocks": bool(found_object is not None and found_object.blocks),
                "difficult": bool(found_terrain is not None and found_terrain.difficult),
                "cell_color": mark.color,
                "pattern": mark.pattern,
                "cell_icon": mark.icon,
                "label": mark.label,
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
        "palette": entry.palette,
        "ambient": entry.ambient,
        "background_color": entry.background_color,
        "grid_color": entry.grid_color,
        "round": encounter.round,
        "acting": encounter.acting,
        "awaiting_initiative": awaiting_initiative(state),
        "moves_left": _moves_left(encounter, grid),
        "move_cells": grid.move_cells if grid is not None else None,
        "cell_feet": grid.cell_feet if grid is not None else None,
        "participants": participants,
    }


def _newest_map_at(state: CampaignState, place_name: str) -> EncounterMap | None:
    """The newest map set in a place, or None when it has none."""
    matching = [
        encounter_map
        for encounter_map in state.maps.values()
        if name_key(encounter_map.location) == name_key(place_name)
    ]
    if not matching:
        return None
    return max(enumerate(matching), key=lambda pair: (pair[1].created, pair[0]))[1]


def _local_map_for(state: CampaignState, current_location: str | None) -> EncounterMap | None:
    """The local map for a place: its newest map, or the newest map of its nearest parent."""
    place = state.places.get(name_key(current_location)) if current_location else None
    while place is not None:
        entry = _newest_map_at(state, place.name)
        if entry is not None:
            return entry
        place = state.places.get(name_key(place.parent)) if place.parent else None
    return None


def _inside_place(state: CampaignState, location: str | None, ancestor: str) -> bool:
    """Whether a place is `ancestor` or lies inside it, following parent places."""
    seen: set[str] = set()
    name = location
    while name is not None:
        key = name_key(name)
        if key == name_key(ancestor):
            return True
        if key in seen:
            return False
        seen.add(key)
        place = state.places.get(key)
        name = place.parent if place is not None else None
    return False


def _uses_local_map(state: CampaignState, entry: EncounterMap, location: str | None) -> bool:
    """Whether this is the map a character's own place resolves to.

    A character in a room with its own map belongs there, not on the settlement map that contains
    the room. A room without a map falls back to its parent's map, so its characters still use it.
    """
    return _local_map_for(state, location) is entry


def local_map_view(
    state: CampaignState,
    grid: EncounterGrid | None,
    terrain: Mapping[str, EncounterTerrain] | None,
    encounter_objects: Mapping[str, EncounterObject] | None,
    characters: Mapping[str, Character],
    current_location: str | None,
) -> dict[str, Any] | None:
    """The local map where the player is, as the UI and the GM's orientation text show it.

    The map is the newest one at the place, or at its nearest parent when the place has none.
    It shows the characters there and every token placed on it; a character with a token stands
    on that cell. None when the player has no place or no map covers it. This is not an
    encounter: there is no round, turn, or movement allowance."""
    entry = _local_map_for(state, current_location)
    if entry is None:
        return None
    terrain = terrain or {}
    encounter_objects = encounter_objects or {}
    found: dict[str, dict[str, Any]] = {}
    for key, cell in entry.tokens.items():
        character = characters.get(key)
        if character is None:
            # A prop has no location of its own; its token is its whole position.
            found[key] = {
                "name": _token_name(key),
                "cell": cell,
                "is_pc": False,
                "kind": "prop",
                "placed": True,
                "creature": "",
                "status": "",
                "defeated": False,
            }
        elif _uses_local_map(state, entry, character.location):
            found[key] = {
                "name": character.name,
                "cell": cell,
                "is_pc": character.kind == PC_KIND,
                "kind": "player character" if character.kind == PC_KIND else "npc",
                "placed": True,
                "creature": _creature_label(character),
                "status": _status(character, None),
                "defeated": character.defeated,
            }
        # A token left at another place is stale. The character is not here, so the map does
        # not show them until they return or are placed again.
    for key, character in characters.items():
        if key in found:
            continue
        if not _uses_local_map(state, entry, character.location):
            continue
        # Only a token gives a character a position. An unplaced character is listed, but no
        # cell is invented for them.
        found[key] = {
            "name": character.name,
            "cell": None,
            "is_pc": character.kind == PC_KIND,
            "kind": "player character" if character.kind == PC_KIND else "npc",
            "placed": False,
            "creature": _creature_label(character),
            "status": _status(character, None),
            "defeated": character.defeated,
        }
    labels = token_labels(participant["name"] for participant in found.values())
    participants = [
        {"number": number, "label": labels[participant["name"]], **participant}
        for number, participant in enumerate(found.values(), start=1)
    ]
    return {
        "map": entry.name,
        "location": entry.location,
        "description": entry.description,
        "columns": entry.columns,
        "rows": entry.rows,
        "background": _terrain_view(entry.background, terrain),
        "marked_cells": _marked_cells(entry, terrain, encounter_objects),
        "palette": entry.palette,
        "ambient": entry.ambient,
        "background_color": entry.background_color,
        "grid_color": entry.grid_color,
        "participants": participants,
    }


def local_map_position(state: CampaignState, character: Character) -> dict[str, Any] | None:
    """Where a character stands on the local map at their location, when a token places them."""
    entry = _local_map_for(state, character.location)
    if entry is None:
        return None
    cell = entry.tokens.get(name_key(character.name))
    if cell is None:
        return None
    return {"map": entry.name, "column": cell[0], "row": cell[1]}
