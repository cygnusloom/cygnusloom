"""Local maps outside encounters: tokens, the local map tab, and seeded maps.

A local map is an encounter map used before a fight: it holds the room's furniture and the
people and props in it. Starting an encounter on the same map keeps those tokens as the
participants' cells, so the board does not have to be built twice."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from cygnusloom.db.models import Event
from cygnusloom.services import encounters, views
from cygnusloom.services.events import CellMark, name_key
from tests.conftest import content_library
from tests.factories import create_campaign, open_campaign, signed_in_client
from tests.test_encounter_marks import (
    MAP,
    TOWN,
    _call,
    _campaign_id,
    _db,
    _map,
    _place_pc,
)

LIBRARY = content_library()


def _create_local_map(client: TestClient, campaign_id: int, template: str = "tavern") -> dict:
    return _call(
        client.app,
        campaign_id,
        "create_local_map",
        {
            "name": MAP,
            "location": TOWN,
            "description": "The common room, with its tables and hearth.",
            "template": template,
        },
    )


def test_a_local_map_is_created_without_starting_an_encounter(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    created = _create_local_map(client, campaign_id)
    assert created["created"] == "local map"
    state = open_campaign(client.app, campaign_id)
    entry = state.maps[name_key(MAP)]
    assert entry.location == TOWN
    assert entry.columns == 14
    assert entry.rows == 10
    assert entry.marks
    assert state.encounter is None


def test_a_token_marks_where_an_npc_stands_and_carries_into_the_encounter(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    placed = _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Pip", "column": 3, "row": 4},
    )
    assert placed["placed"] == "Pip"
    state = open_campaign(client.app, campaign_id)
    assert state.maps[name_key(MAP)].tokens[name_key("Pip")] == (3, 4)

    _call(client.app, campaign_id, "start_encounter", {"map": MAP})
    _call(client.app, campaign_id, "add_participant", {"name": "Pip"})
    state = open_campaign(client.app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.cells[name_key("Pip")] == (3, 4)


def test_a_map_token_can_be_removed(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Pip", "column": 3, "row": 4},
    )
    removed = _call(client.app, campaign_id, "remove_map_token", {"map": MAP, "name": "Pip"})
    assert removed["removed"] == "Pip"
    state = open_campaign(client.app, campaign_id)
    assert name_key("Pip") not in state.maps[name_key(MAP)].tokens


def test_the_local_map_view_shows_the_characters_in_the_room(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Pip", "column": 2, "row": 1},
    )
    state = open_campaign(client.app, campaign_id)
    player = state.pc()
    assert player is not None
    view = encounters.local_map_view(
        state,
        LIBRARY.rule_sets["cairn2e"].grid,
        LIBRARY.rule_sets["cairn2e"].terrain,
        LIBRARY.rule_sets["cairn2e"].encounter_objects,
        state.characters,
        player.location,
    )
    assert view is not None
    by_name = {participant["name"]: participant for participant in view["participants"]}
    assert by_name["Pip"]["cell"] == (2, 1)
    assert by_name["Pip"]["placed"] is True
    assert by_name["Pip"]["kind"] == "npc"
    # No token means no logical position: the view does not invent a cell for Ada.
    assert by_name["Ada"]["cell"] is None
    assert by_name["Ada"]["placed"] is False
    assert by_name["Ada"]["is_pc"]
    assert by_name["Ada"]["kind"] == "player character"

    _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Ada", "column": 5, "row": 5},
    )
    state = open_campaign(client.app, campaign_id)
    view = encounters.local_map_view(
        state,
        LIBRARY.rule_sets["cairn2e"].grid,
        LIBRARY.rule_sets["cairn2e"].terrain,
        LIBRARY.rule_sets["cairn2e"].encounter_objects,
        state.characters,
        player.location,
    )
    assert view is not None
    by_name = {participant["name"]: participant for participant in view["participants"]}
    assert by_name["Ada"]["cell"] == (5, 5)
    assert by_name["Ada"]["placed"] is True


def test_a_token_left_at_another_place_is_not_shown_on_the_map(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Pip", "column": 2, "row": 1},
    )
    _call(
        client.app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Pip", "location": "Spore Bridge"},
    )
    state = open_campaign(client.app, campaign_id)
    player = state.pc()
    assert player is not None
    view = encounters.local_map_view(
        state,
        LIBRARY.rule_sets["cairn2e"].grid,
        LIBRARY.rule_sets["cairn2e"].terrain,
        LIBRARY.rule_sets["cairn2e"].encounter_objects,
        state.characters,
        player.location,
    )
    assert view is not None
    names = {participant["name"] for participant in view["participants"]}
    assert "Pip" not in names


def test_a_character_inside_a_place_can_be_placed_on_its_map(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app,
        campaign_id,
        "create_location",
        {
            "name": "The Drowsy Cap",
            "description": "A tavern under the oak.",
            "parent": TOWN,
        },
    )
    _call(
        client.app,
        campaign_id,
        "create_npc",
        {
            "name": "Pip",
            "location": "The Drowsy Cap",
            "description": "A scruffy courier.",
        },
    )
    placed = _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Pip", "column": 4, "row": 4},
    )
    assert placed["placed"] == "Pip"
    state = open_campaign(client.app, campaign_id)
    player = state.pc()
    assert player is not None
    view = encounters.local_map_view(
        state,
        LIBRARY.rule_sets["cairn2e"].grid,
        LIBRARY.rule_sets["cairn2e"].terrain,
        LIBRARY.rule_sets["cairn2e"].encounter_objects,
        state.characters,
        player.location,
    )
    assert view is not None
    by_name = {participant["name"]: participant for participant in view["participants"]}
    assert by_name["Pip"]["cell"] == (4, 4)


def test_a_prop_token_keeps_its_cell_without_a_character(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Wooden Crate", "column": 6, "row": 3},
    )
    state = open_campaign(client.app, campaign_id)
    player = state.pc()
    assert player is not None
    view = encounters.local_map_view(
        state,
        LIBRARY.rule_sets["cairn2e"].grid,
        LIBRARY.rule_sets["cairn2e"].terrain,
        LIBRARY.rule_sets["cairn2e"].encounter_objects,
        state.characters,
        player.location,
    )
    assert view is not None
    by_name = {participant["name"]: participant for participant in view["participants"]}
    assert by_name["Wooden Crate"]["cell"] == (6, 3)
    assert by_name["Wooden Crate"]["kind"] == "prop"
    assert by_name["Wooden Crate"]["placed"] is True


def test_the_gm_orientation_lists_local_map_positions_and_unplaced_characters(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Pip", "column": 2, "row": 1},
    )
    rules = LIBRARY.rule_sets["cairn2e"]
    text = views.world_state_text(
        open_campaign(client.app, campaign_id),
        rules.campaign,
        grid=rules.grid,
        terrain=rules.terrain,
        encounter_objects=rules.encounter_objects,
    )
    assert f"Local map: {MAP} at {TOWN}" in text
    assert "- Pip (npc), column 2, row 1." in text
    assert "- Ada (player character), not yet placed." in text
    assert "Place every unplaced character with place_map_token" in text
    assert "Move a character's token with place_map_token whenever the scene moves them" in text


def test_get_statblock_reports_a_local_map_position(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Pip", "column": 2, "row": 1},
    )
    pip = _call(client.app, campaign_id, "get_statblock", {"name": "Pip"})
    assert pip["position"] == {"map": MAP, "column": 2, "row": 1}
    ada = _call(client.app, campaign_id, "get_statblock", {"name": "Ada"})
    assert "position" not in ada


def test_the_local_map_roster_marks_placed_and_unplaced_characters(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    _call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Pip", "column": 2, "row": 1},
    )
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "column 2, row 1" in page
    assert "not yet placed" in page


def test_the_campaign_page_offers_the_local_map_tab(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'id="tab-local"' in page
    assert "Local map" in page
    assert MAP in page


def test_a_bundled_world_seeds_its_authored_local_maps(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(
        client, world_template="twin-lakes", rule_set="cairn2e", label="Twin Lakes"
    )
    state = open_campaign(client.app, campaign_id)
    names = {entry.name for entry in state.maps.values()}
    assert "The Stuffed Inn local map" in names
    assert "Cellar local map" in names
    inn = next(entry for entry in state.maps.values() if entry.name == "The Stuffed Inn local map")
    assert inn.location == "The Stuffed Inn"
    # The inn's NPCs are already standing somewhere on its map.
    assert inn.tokens


def test_a_local_map_copies_its_template_style_and_can_override_it(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id, "great_hall")
    entry = _map(client.app, campaign_id)
    assert (entry.palette, entry.ambient) == ("royal", "torchlit")
    assert entry.background_color == "#d9d4c8"
    assert entry.grid_color == "#b9b1a2"

    _call(
        client.app,
        campaign_id,
        "create_local_map",
        {
            "name": "Frost Hall",
            "location": TOWN,
            "description": "A cold, echoing hall.",
            "template": "great_hall",
            "palette": "frost",
            "ambient": "night",
            "background_color": "#112233",
            "grid_color": "#445566",
        },
    )
    cold = _map(client.app, campaign_id, "Frost Hall")
    assert (cold.palette, cold.ambient) == ("frost", "night")
    assert (cold.background_color, cold.grid_color) == ("#112233", "#445566")


def test_create_map_validates_palette_ambient_and_colours(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    base = {
        "name": MAP,
        "location": TOWN,
        "description": "A hall.",
        "template": "tavern",
    }
    for change, message in (
        ({"palette": "neon"}, "palette must be one of"),
        ({"ambient": "stormy"}, "ambient must be one of"),
        ({"background_color": "white"}, "hex colour"),
        ({"grid_color": "#12"}, "hex colour"),
    ):
        refused = _call(client.app, campaign_id, "create_local_map", {**base, **change})
        assert message in refused["error"]
    assert name_key(MAP) not in open_campaign(client.app, campaign_id).maps


def test_map_options_lists_terrains_objects_templates_and_looks(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    options = _call(client.app, campaign_id, "map_options", {})
    terrains = {terrain["id"]: terrain for terrain in options["terrains"]}
    assert {"marble", "wood", "lava", "void"} <= set(terrains)
    assert terrains["marble"]["pattern"] == "tiles"
    assert terrains["lava"]["difficult"] is True
    assert terrains["marble"]["category"] == "stone"
    objects = {item["id"]: item for item in options["objects"]}
    assert {"window", "alchemy-table", "rune-circle", "cauldron-small"} <= set(objects)
    assert objects["alchemy-table"]["blocks"] is True
    assert objects["alchemy-table"]["category"] == "furniture"
    templates = {template["id"]: template for template in options["templates"]}
    assert templates["great_hall"]["palette"] == "royal"
    assert templates["alchemy_lab"]["ambient"] == "magical"
    assert "royal" in options["palettes"]
    assert "torchlit" in options["ambients"]
    assert {"tiles", "runes", "bricks", "wood"} <= set(options["patterns"])


def test_update_local_map_renames_restyles_and_moves_a_map(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(
        client.app, campaign_id, "create_location", {"name": "Far Shore", "description": "Across."}
    )
    updated = _call(
        client.app,
        campaign_id,
        "update_local_map",
        {
            "map": MAP,
            "new_name": "Moon Hall",
            "location": "Far Shore",
            "palette": "sea",
            "ambient": "dusk",
            "background": "wood",
            "background_color": "#112233",
            "grid_color": "#445566",
        },
    )
    assert updated["updated"] == "local map"
    assert updated["name"] == "Moon Hall"
    assert updated["location"] == "Far Shore"
    state = open_campaign(client.app, campaign_id)
    assert name_key(MAP) not in state.maps
    entry = state.maps[name_key("Moon Hall")]
    assert (entry.palette, entry.ambient) == ("sea", "dusk")
    assert (entry.background, entry.background_color, entry.grid_color) == (
        "wood",
        "#112233",
        "#445566",
    )


def test_extending_left_and_up_shifts_marks_tokens_and_encounter_cells(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    _call(
        app,
        campaign_id,
        "create_encounter_map",
        {"name": MAP, "location": TOWN, "columns": 10, "rows": 10, "description": "Open."},
    )
    _call(
        app,
        campaign_id,
        "set_map_cells",
        {"map": MAP, "cells": [{"column": 1, "row": 1, "terrain": "mud", "label": "Corner"}]},
    )
    _call(app, campaign_id, "place_map_token", {"map": MAP, "name": "Ada", "column": 2, "row": 2})
    _call(
        app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    _call(app, campaign_id, "place_map_token", {"map": MAP, "name": "Pip", "column": 3, "row": 3})
    _call(app, campaign_id, "start_encounter", {"map": MAP})
    _call(app, campaign_id, "add_participant", {"name": "Ada"})
    _call(app, campaign_id, "add_participant", {"name": "Pip"})

    updated = _call(
        app,
        campaign_id,
        "update_local_map",
        {"map": MAP, "extend_left": 2, "extend_up": 3},
    )
    assert updated["columns"] == 12
    assert updated["rows"] == 13
    state = open_campaign(app, campaign_id)
    entry = state.maps[name_key(MAP)]
    assert (entry.columns, entry.rows) == (12, 13)
    assert entry.marks[(3, 4)] == CellMark("mud", "", label="Corner")
    assert entry.tokens[name_key("Ada")] == (4, 5)
    assert entry.tokens[name_key("Pip")] == (5, 6)
    assert state.encounter is not None
    assert state.encounter.cells[name_key("Ada")] == (4, 5)
    assert state.encounter.cells[name_key("Pip")] == (5, 6)

    # A replay of the events gives the same shifted scene.
    replayed = open_campaign(app, campaign_id)
    assert replayed.maps[name_key(MAP)].marks[(3, 4)].label == "Corner"


def test_extending_right_and_down_keeps_cells_where_they_are(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _call(app, campaign_id, "place_map_token", {"map": MAP, "name": "Ada", "column": 2, "row": 2})
    updated = _call(
        app,
        campaign_id,
        "update_local_map",
        {"map": MAP, "extend_right": 3, "extend_down": 1},
    )
    assert (updated["columns"], updated["rows"]) == (17, 11)
    entry = _map(app, campaign_id)
    assert entry.tokens[name_key("Ada")] == (2, 2)


def test_shrinking_reports_the_marks_and_tokens_it_drops(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    _call(
        app,
        campaign_id,
        "create_encounter_map",
        {"name": MAP, "location": TOWN, "columns": 10, "rows": 10, "description": "Open."},
    )
    _call(
        app,
        campaign_id,
        "set_map_cells",
        {"map": MAP, "cells": [{"column": 9, "row": 9, "terrain": "mud"}]},
    )
    _call(app, campaign_id, "place_map_token", {"map": MAP, "name": "Ada", "column": 8, "row": 8})
    updated = _call(app, campaign_id, "update_local_map", {"map": MAP, "columns": 5, "rows": 5})
    assert updated["dropped_marks"] == 1
    assert updated["dropped_tokens"] == 1
    entry = _map(app, campaign_id)
    assert (entry.columns, entry.rows) == (5, 5)
    assert not entry.marks and not entry.tokens


def test_the_local_map_panel_shows_the_palette_ambient_pattern_and_legend(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id, "great_hall")
    _call(
        client.app,
        campaign_id,
        "set_map_cells",
        {
            "map": MAP,
            "cells": [
                {
                    "column": 5,
                    "row": 5,
                    "terrain": "marble",
                    "color": "#123456",
                    "pattern": "runes",
                    "label": "Dais",
                }
            ],
        },
    )
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "palette-royal" in page
    assert "ambient-torchlit" in page
    assert 'class="map-legend"' in page
    assert "Royal" in page and "Torchlit" in page
    assert 'class="cell-pattern pattern-runes"' in page
    assert 'style="background-color: #123456"' in page
    assert 'data-label="Dais"' in page
    assert "Dais" in page


def test_old_map_events_without_the_new_fields_still_replay(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    with _db(client.app) as db:
        newest = db.exec(
            select(func.max(Event.seq)).where(Event.campaign_id == campaign_id)
        ).scalar_one()
        seq = (newest or 0) + 1
        db.add(
            Event(
                campaign_id=campaign_id,
                seq=seq,
                channel="log",
                kind="map.created",
                payload=json.dumps(
                    {
                        "name": "Old Map",
                        "location": TOWN,
                        "columns": 4,
                        "rows": 3,
                        "description": "A map from before styles.",
                        "background": "floor",
                        "marks": [{"column": 1, "row": 1, "terrain": "mud", "object": ""}],
                    }
                ),
            )
        )
        db.add(
            Event(
                campaign_id=campaign_id,
                seq=seq + 1,
                channel="log",
                kind="map.cells_changed",
                payload=json.dumps(
                    {
                        "name": "Old Map",
                        "marks": [{"column": 2, "row": 2, "terrain": "", "object": ""}],
                    }
                ),
            )
        )
        db.commit()
    entry = open_campaign(client.app, campaign_id).maps[name_key("Old Map")]
    assert (entry.columns, entry.rows) == (4, 3)
    assert entry.marks[(1, 1)] == CellMark("mud", "")
    assert entry.palette == "" and entry.ambient == ""
    assert entry.background_color == "" and entry.grid_color == ""
    assert (2, 2) not in entry.marks
