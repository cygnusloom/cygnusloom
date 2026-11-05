"""Local maps outside encounters: tokens, the local map tab, and seeded maps.

A local map is an encounter map used before a fight: it holds the room's furniture and the
people and props in it. Starting an encounter on the same map keeps those tokens as the
participants' cells, so the board does not have to be built twice."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from cygnusloom.services import encounters
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
    assert by_name["Ada"]["cell"] is not None
    assert by_name["Ada"]["is_pc"]


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
