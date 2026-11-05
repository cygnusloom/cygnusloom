"""Local maps outside encounters: tokens, the local map tab, and seeded maps.

A local map is an encounter map used before a fight: it holds the room's furniture and the
people and props in it. Starting an encounter on the same map keeps those tokens as the
participants' cells, so the board does not have to be built twice."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from cygnusloom.services import encounters, views
from cygnusloom.services.events import name_key
from tests.conftest import content_library
from tests.factories import create_campaign, open_campaign, signed_in_client
from tests.test_encounter_marks import MAP, TOWN, _call, _campaign_id, _place_pc

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
