from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.library import EncounterGrid
from cygnusforge.llm.base import ToolRequest
from cygnusforge.services import encounters
from cygnusforge.services.events import CellMark, load_state, name_key
from cygnusforge.services.tools import ToolRun, run_tool
from cygnusforge.web.campaigns import _cell_tip
from tests.conftest import content_library
from tests.test_campaigns import _campaign_id, _place_pc
from tests.test_world import _ctx, _db, _state

LIBRARY = content_library()
RULES = LIBRARY.rule_sets["cairn2e"]
CAIRN_GRID = RULES.grid
assert CAIRN_GRID is not None
MAP = "Mossbell Green"
TOWN = "Mossbell Hollow"


def _call(
    app: FastAPI,
    campaign_id: int,
    name: str,
    arguments: dict[str, Any],
    grid: EncounterGrid | None = None,
) -> dict[str, Any]:
    """Run a tool with the cairn2e rule set's grid, terrain, objects and templates."""
    ctx = _ctx(app, campaign_id)
    world = replace(
        ctx.world,
        grid=grid,
        terrain=RULES.terrain,
        encounter_objects=RULES.encounter_objects,
        encounter_templates=RULES.encounter_templates,
    )
    ctx = replace(ctx, world=world)
    request = ToolRequest(id="call_1", name=name, arguments=json.dumps(arguments))
    run = asyncio.run(run_tool(RULES.tool_calls, request, None, ctx))
    assert isinstance(run, ToolRun)
    return json.loads(run.output)


def _map(app: FastAPI, campaign_id: int, name: str = MAP) -> Any:
    return _state(app, campaign_id).maps[name_key(name)]


def _started(
    client_factory: Callable[[], TestClient], map_args: dict[str, Any]
) -> tuple[TestClient, int]:
    """A campaign with a map built from `map_args`, an encounter started on it, and Pip and Ada
    joined in that order."""
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    args = {"name": MAP, "location": TOWN, "description": "Test ground.", **map_args}
    created = _call(app, campaign_id, "create_encounter_map", args)
    assert "error" not in created, created
    assert _call(app, campaign_id, "start_encounter", {"map": MAP})["started"] == "encounter"
    _call(
        app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    assert _call(app, campaign_id, "add_participant", {"name": "Pip"})["added"] == "Pip"
    assert _call(app, campaign_id, "add_participant", {"name": "Ada"})["added"] == "Ada"
    return client, campaign_id


def _move(app: FastAPI, campaign_id: int, column: int, row: int) -> dict[str, Any]:
    return _call(
        app,
        campaign_id,
        "place_participant",
        {"name": "Pip", "column": column, "row": row},
        CAIRN_GRID,
    )


def _begin_pip(app: FastAPI, campaign_id: int) -> None:
    with _db(app) as db:
        encounters.begin_turn(db, campaign_id, "Pip")


def _paint(app: FastAPI, campaign_id: int, cells: list[dict[str, Any]]) -> dict[str, Any]:
    return _call(app, campaign_id, "set_map_cells", {"map": MAP, "cells": cells})


def _wall(column: int, rows: range) -> list[dict[str, Any]]:
    return [{"column": column, "row": row, "object": "wall"} for row in rows]


def test_template_creates_a_map_with_its_size_background_and_marks(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    created = _call(
        app,
        campaign_id,
        "create_encounter_map",
        {"name": MAP, "location": TOWN, "description": "A hall.", "template": "room"},
    )
    assert created["created"] == "encounter map"
    entry = _map(app, campaign_id)
    assert (entry.columns, entry.rows, entry.background) == (10, 8, "floor")
    assert len(entry.marks) == 80
    assert entry.marks[(0, 0)] == CellMark("floor", "wall")
    assert entry.marks[(4, 0)] == CellMark("floor", "door")
    assert entry.marks[(1, 1)] == CellMark("floor", "")


def test_template_size_can_be_overridden_and_out_of_bounds_marks_are_refused(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    base = {"name": MAP, "location": TOWN, "description": "A hall.", "template": "room"}
    wider = _call(app, campaign_id, "create_encounter_map", {**base, "columns": 12})
    assert wider["columns"] == 12
    entry = _map(app, campaign_id)
    assert (entry.columns, entry.rows) == (12, 8)
    assert len(entry.marks) == 80

    refused = _call(app, campaign_id, "create_encounter_map", {**base, "name": "Short", "rows": 5})
    assert "column 0, row 5" in refused["error"]
    assert "outside the 10 by 5 map" in refused["error"]
    assert name_key("Short") not in _state(app, campaign_id).maps


def test_unknown_template_lists_the_known_ids(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    refused = _call(
        app,
        campaign_id,
        "create_encounter_map",
        {"name": MAP, "location": TOWN, "description": "A hall.", "template": "castle"},
    )
    assert "there is no encounter template 'castle'" in refused["error"]
    assert "clearing" in refused["error"] and "shoreline" in refused["error"]
    assert MAP not in _state(app, campaign_id).maps


def test_empty_map_still_needs_columns_and_rows(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    refused = _call(
        app,
        campaign_id,
        "create_encounter_map",
        {"name": MAP, "location": TOWN, "description": "x"},
    )
    assert "error" in refused
    assert MAP not in _state(app, campaign_id).maps


def test_set_map_cells_validates_ids_bounds_and_shape(client_factory: Callable[[], TestClient]):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app

    bad_terrain = _paint(app, campaign_id, [{"column": 1, "row": 1, "terrain": "lava"}])
    assert "there is no terrain 'lava'" in bad_terrain["error"]
    assert "mud" in bad_terrain["error"]
    bad_object = _paint(app, campaign_id, [{"column": 1, "row": 1, "object": "dragon"}])
    assert "there is no encounter object 'dragon'" in bad_object["error"]
    off_map = _paint(app, campaign_id, [{"column": 10, "row": 0, "terrain": "mud"}])
    assert "from 0 to 9" in off_map["error"]
    empty = _paint(app, campaign_id, [{"column": 1, "row": 1}])
    assert "needs terrain or object" in empty["error"]
    too_many = _paint(app, campaign_id, [{"column": 0, "row": 0, "terrain": "mud"}] * 201)
    assert "at most 200 cells" in too_many["error"]
    unknown = _call(
        app,
        campaign_id,
        "set_map_cells",
        {"map": "Nowhere", "cells": [{"column": 0, "row": 0, "terrain": "mud"}]},
    )
    assert "error" in unknown
    assert _map(app, campaign_id).marks == {}


def test_set_map_cells_sets_and_clears_keys(client_factory: Callable[[], TestClient]):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app
    result = _paint(
        app,
        campaign_id,
        [
            {"column": 1, "row": 1, "terrain": "mud"},
            {"column": 2, "row": 2, "terrain": "grass", "object": "tree"},
        ],
    )
    assert result == {"map": MAP, "changed": 2}
    marks = _map(app, campaign_id).marks
    assert marks[(1, 1)] == CellMark("mud", "")
    assert marks[(2, 2)] == CellMark("grass", "tree")

    _paint(app, campaign_id, [{"column": 2, "row": 2, "object": ""}])
    assert _map(app, campaign_id).marks[(2, 2)] == CellMark("grass", "")
    _paint(app, campaign_id, [{"column": 1, "row": 1, "terrain": ""}])
    assert (1, 1) not in _map(app, campaign_id).marks


def test_blocked_destination_is_refused_even_for_the_first_placement(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app
    _paint(app, campaign_id, _wall(5, range(10)))
    refused = _move(app, campaign_id, 5, 5)
    assert "is blocked by wall" in refused["error"]
    assert _move(app, campaign_id, 4, 5)["placed"] == "Pip"


def test_path_around_a_wall_costs_more_than_the_straight_line(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app
    # Open only at row 9, so (4,5) to (6,5) goes down to row 9 and back: 8 moves, not 2.
    _paint(app, campaign_id, _wall(5, range(9)))
    assert _move(app, campaign_id, 4, 5)["placed"] == "Pip"
    _begin_pip(app, campaign_id)
    moved = _move(app, campaign_id, 6, 5)
    assert moved["placed"] == "Pip"
    assert moved["moves_left"] == 0

    too_far = _move(app, campaign_id, 4, 5)
    assert "the way is too far, it costs 8 and 0 moves are left" in too_far["error"]


def test_no_route_at_all_is_refused(client_factory: Callable[[], TestClient]):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app
    _paint(app, campaign_id, _wall(5, range(10)))
    assert _move(app, campaign_id, 4, 5)["placed"] == "Pip"
    _begin_pip(app, campaign_id)
    refused = _move(app, campaign_id, 6, 5)
    assert "has no way to column 6, row 5: it is blocked" in refused["error"]


def test_off_turn_placement_still_needs_a_route_around_walls(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app
    _paint(app, campaign_id, _wall(5, range(10)))
    assert _move(app, campaign_id, 4, 5)["placed"] == "Pip"
    refused = _move(app, campaign_id, 6, 5)
    assert "has no way to column 6, row 5: it is blocked" in refused["error"]
    assert _state(app, campaign_id).encounter.cells[name_key("Pip")] == (4, 5)


def test_off_turn_placement_around_a_partial_wall_costs_nothing(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app
    _paint(app, campaign_id, _wall(5, range(9)))
    assert _move(app, campaign_id, 4, 5)["placed"] == "Pip"
    moved = _move(app, campaign_id, 6, 5)
    assert moved["placed"] == "Pip"
    assert _state(app, campaign_id).encounter.cells[name_key("Pip")] == (6, 5)


def test_difficult_terrain_costs_two_per_cell_entered(client_factory: Callable[[], TestClient]):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app
    mud = [
        {"column": column, "row": row, "terrain": "mud"} for column in (5, 6) for row in range(10)
    ]
    _paint(app, campaign_id, mud)
    assert _move(app, campaign_id, 4, 5)["placed"] == "Pip"
    _begin_pip(app, campaign_id)
    moved = _move(app, campaign_id, 6, 5)
    assert moved["placed"] == "Pip"
    assert moved["moves_left"] == 4


def test_movement_without_marks_is_the_plain_distance(client_factory: Callable[[], TestClient]):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app
    assert _move(app, campaign_id, 0, 0)["placed"] == "Pip"
    _begin_pip(app, campaign_id)
    moved = _move(app, campaign_id, 5, 3)
    assert moved["moves_left"] == 3


def test_template_marks_survive_a_replay_of_the_event_log(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _started(client_factory, {"template": "room"})
    app = client.app
    _paint(app, campaign_id, [{"column": 1, "row": 1, "terrain": "mud"}])
    with _db(app) as db:
        replayed = load_state(db, campaign_id).maps[name_key(MAP)]
    assert replayed.background == "floor"
    assert replayed.marks[(0, 0)] == CellMark("floor", "wall")
    assert replayed.marks[(1, 1)] == CellMark("mud", "")
    assert len(replayed.marks) == 80


def test_shrinking_a_map_drops_marks_outside_it_and_rename_keeps_them(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    _call(
        app,
        campaign_id,
        "create_encounter_map",
        {"name": MAP, "location": TOWN, "description": "A hall.", "template": "room"},
    )
    updated = _call(
        app,
        campaign_id,
        "update_encounter_map",
        {
            "name": MAP,
            "columns": 8,
            "rows": 8,
            "description": "A smaller hall.",
            "new_name": "Village Green",
        },
    )
    assert "error" not in updated
    entry = _map(app, campaign_id, "Village Green")
    assert (entry.columns, entry.rows) == (8, 8)
    assert len(entry.marks) == 64
    assert all(column < 8 for column, _ in entry.marks)
    assert entry.marks[(0, 0)] == CellMark("floor", "wall")


def test_gm_view_lists_the_background_and_marked_cells(client_factory: Callable[[], TestClient]):
    client, campaign_id = _started(client_factory, {"template": "room"})
    app = client.app
    _paint(app, campaign_id, [{"column": 1, "row": 1, "terrain": "mud"}])
    view = encounters.encounter_view(
        _state(app, campaign_id), CAIRN_GRID, None, RULES.terrain, RULES.encounter_objects
    )
    assert view is not None
    assert view["background"] == {
        "id": "floor",
        "name": RULES.terrain["floor"].name,
        "color": RULES.terrain["floor"].color,
        "rule": RULES.terrain["floor"].rule,
        "difficult": False,
    }
    cells = {(cell["column"], cell["row"]): cell for cell in view["marked_cells"]}
    assert len(cells) == 80
    assert cells[(0, 0)] == {
        "column": 0,
        "row": 0,
        "terrain": RULES.terrain["floor"].name,
        "terrain_color": RULES.terrain["floor"].color,
        "terrain_rule": RULES.terrain["floor"].rule,
        "object": RULES.encounter_objects["wall"].name,
        "object_icon": RULES.encounter_objects["wall"].icon,
        "object_color": RULES.encounter_objects["wall"].color,
        "object_solid": True,
        "blocks": True,
        "difficult": False,
    }
    assert cells[(4, 0)]["blocks"] is False
    assert cells[(1, 1)] == {
        "column": 1,
        "row": 1,
        "terrain": RULES.terrain["mud"].name,
        "terrain_color": RULES.terrain["mud"].color,
        "terrain_rule": RULES.terrain["mud"].rule,
        "object": "",
        "object_icon": "",
        "object_color": "",
        "object_solid": False,
        "blocks": False,
        "difficult": True,
    }


def test_campaign_board_draws_marked_terrain_and_objects(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _started(client_factory, {"template": "room"})
    app = client.app
    _paint(
        app,
        campaign_id,
        [{"column": 1, "row": 1, "terrain": "mud"}, {"column": 2, "row": 1, "object": "wall"}],
    )
    page = client.get(f"/campaigns/{campaign_id}").text
    wall = RULES.encounter_objects["wall"]
    mud = RULES.terrain["mud"]
    assert f'class="cell-solid" style="background-color: {wall.color}"' in page
    assert f'style="background-color: {mud.color}"' in page
    assert 'class="encounter-cell blocked"' in page
    assert 'class="encounter-cell difficult"' in page
    assert f'data-terrain="{RULES.terrain["mud"].name}"' in page


def test_board_cells_hover_with_their_terrain_rule_object_and_any_token(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _started(client_factory, {"template": "room"})
    app = client.app
    _paint(
        app,
        campaign_id,
        [{"column": 1, "row": 1, "terrain": "mud"}, {"column": 3, "row": 3, "terrain": "mud"}],
    )
    _call(app, campaign_id, "place_participant", {"name": "Ada", "column": 1, "row": 1}, CAIRN_GRID)
    page = client.get(f"/campaigns/{campaign_id}").text
    mud_rule = RULES.terrain["mud"].rule
    assert f'data-tip="Ada on Mud. {mud_rule}"' in page
    floor = RULES.terrain["floor"]
    assert f'data-tip="{floor.name}. {floor.rule}"' in page
    assert f'data-tip="Mud. {mud_rule}"' in page
    _paint(app, campaign_id, [{"column": 2, "row": 2, "object": "wall"}])
    wall = RULES.encounter_objects["wall"]
    assert (
        f'data-tip="{wall.name} on {floor.name}. Blocks movement'
        in client.get(f"/campaigns/{campaign_id}").text
    )


def test_cell_tip_names_the_token_object_and_ground_or_neither():
    blocked = "Blocks movement: no one can enter this cell."
    assert _cell_tip(None, "", "") == ""
    assert _cell_tip(None, "Mud", "Difficult.") == "Mud. Difficult."
    assert _cell_tip({"name": "Ada"}, "Mud", "Difficult.") == "Ada on Mud. Difficult."
    assert _cell_tip({"name": "Ada"}, "", "") == "Ada"
    assert _cell_tip(None, "", "", "Table") == "Table."
    assert _cell_tip({"name": "Ada"}, "Grass", "Normal.", "Campfire") == (
        "Ada on Campfire on Grass. Normal."
    )
    assert _cell_tip(None, "Stone floor", "Normal.", "Wall", True) == (
        f"Wall on Stone floor. {blocked}"
    )
    assert _cell_tip(None, "", "", "Wall", True) == f"Wall. {blocked}"


def test_a_wall_cannot_be_set_on_a_cell_a_participant_occupies(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _started(client_factory, {"columns": 10, "rows": 10})
    app = client.app
    assert _move(app, campaign_id, 4, 5)["placed"] == "Pip"
    refused = _paint(app, campaign_id, [{"column": 4, "row": 5, "object": "wall"}])
    assert refused["error"] == "column 4, row 5 is occupied by Pip, so it cannot be walled off"
    assert _map(app, campaign_id).marks.get((4, 5)) is None
    assert _paint(app, campaign_id, [{"column": 4, "row": 6, "object": "wall"}])["changed"] == 1
