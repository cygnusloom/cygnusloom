"""The statblock dialog: every character name opens the statblock the player may read."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.factories import signed_in_client
from tests.test_campaigns import _campaign_id, _place_pc
from tests.test_combat import FULL_STATS, PC, YARD, _fight_client, _foe
from tests.test_encounter_marks import MAP, TOWN
from tests.test_encounter_marks import _call as _cairn_call
from tests.test_local_maps import _create_local_map


def _statblock(client: TestClient, campaign_id: int, name: str) -> str:
    response = client.get(f"/campaigns/{campaign_id}/statblock", params={"name": name})
    assert response.status_code == 200, response.text
    return response.text


def test_the_encounter_roster_links_every_actor_to_their_statblock(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, tools = _fight_client(client_factory)
    _foe(tools, "Rover")

    page = client.get(f"/campaigns/{campaign_id}").text
    start = page.index('class="encounter-roster"')
    roster = page[start : page.index("</ul>", start)]

    assert f'data-statblock-name="{PC}"' in roster
    assert 'data-statblock-name="Rover"' in roster


def test_the_location_panel_links_people_to_their_statblocks(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")

    page = client.get(f"/campaigns/{campaign_id}").text
    location = page[page.index('id="panel-location"') : page.index('id="panel-character"')]

    assert 'data-statblock-name="Ada"' in location
    assert 'data-statblock-name="Brin Gallowcap"' in location


def test_the_characters_page_links_people_to_their_statblocks(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")

    page = client.get(f"/campaigns/{campaign_id}").text
    characters = page[page.index('id="character-list"') :]

    assert 'data-statblock-name="Ada"' in characters
    assert 'data-statblock-name="Brin Gallowcap"' in characters


def test_the_local_map_roster_links_people_but_not_props(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, TOWN)
    _create_local_map(client, campaign_id)
    _cairn_call(
        client.app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    _cairn_call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Pip", "column": 3, "row": 4},
    )
    _cairn_call(
        client.app,
        campaign_id,
        "place_map_token",
        {"map": MAP, "name": "Wooden Crate", "column": 6, "row": 3},
    )

    page = client.get(f"/campaigns/{campaign_id}").text
    roster = page[page.index('aria-label="People and props here"') :]

    assert 'data-statblock-name="Ada"' in roster
    assert 'data-statblock-name="Pip"' in roster
    assert 'data-statblock-name="Wooden Crate"' not in roster


def test_a_statblock_shows_the_characters_stats_armor_and_attacks(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, tools = _fight_client(client_factory)
    tools.create_monster("Brute", YARD, description="A brute.", stats=FULL_STATS)
    tools.update_npc("Brute", attacks=[{"name": "Claw", "dice": "1d6"}])

    block = _statblock(client, campaign_id, "Brute")

    assert "NPC" in block
    assert "Strength</a> 10" in block
    assert "Dexterity</a> 10" in block
    assert "Willpower</a> 10" in block
    assert "Armor: 0" in block
    assert "Attacks: Claw 1d6" in block


def test_a_player_character_has_a_statblock_too(client_factory: Callable[[], TestClient]):
    client, campaign_id, _tools = _fight_client(client_factory)

    block = _statblock(client, campaign_id, PC)

    assert "Player character" in block
    assert "Strength</a>" in block
    assert "Armor:" in block
    assert "Sword" in block


def test_a_hireling_has_a_statblock_of_their_own(client_factory: Callable[[], TestClient]):
    client, campaign_id, tools = _fight_client(client_factory)
    tools.create_npc("Hesta Vane", YARD, "A quiet guide with a lantern of her own.")
    tools.hire_existing_npc("Hesta Vane", PC, 3, character={"class": "scrapper"})

    block = _statblock(client, campaign_id, "Hesta Vane")

    assert "Hireling" in block
    assert "A quiet guide with a lantern of her own." in block


def test_a_bestiary_creature_links_its_entry_from_the_statblock(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, tools = _fight_client(client_factory)
    tools.create_monster("Blink Dog", YARD)

    block = _statblock(client, campaign_id, "Blink Dog")

    assert "Attacks: bite 1d6" in block
    assert "Open the bestiary entry" in block
    assert 'data-view-link="compendium"' in block


def test_a_plain_npc_still_has_a_statblock_without_a_bestiary_entry(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id, "Glowcap Ring")

    block = _statblock(client, campaign_id, "Brin Gallowcap")

    assert "NPC" in block
    assert "Stats:" in block
    assert "Resolve</a> 12" in block
    assert "Open the bestiary entry" not in block


def test_an_npcs_private_description_stays_out_of_the_statblock(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, tools = _fight_client(client_factory)
    tools.create_monster("Brute", YARD, description="Hides the stolen key.", stats=FULL_STATS)
    tools.update_npc("Brute", attacks=[{"name": "Claw", "dice": "1d6"}])

    block = _statblock(client, campaign_id, "Brute")

    assert "Hides the stolen key." not in block


def test_an_npcs_gm_set_options_stay_out_of_the_statblock(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, tools = _fight_client(client_factory)
    tools.create_npc("Brute", YARD, "A brute.", character={"physique": "Scrawny"})

    block = _statblock(client, campaign_id, "Brute")

    assert "Physique" not in block


def test_a_name_that_is_not_a_character_has_no_statblock(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _tools = _fight_client(client_factory)

    missing = client.get(f"/campaigns/{campaign_id}/statblock", params={"name": "Nobody"})
    location = client.get(f"/campaigns/{campaign_id}/statblock", params={"name": YARD})

    assert missing.status_code == 404
    assert location.status_code == 404


def test_another_account_cannot_open_the_statblock(
    client_factory: Callable[[], TestClient],
):
    _client, campaign_id, _tools = _fight_client(client_factory)
    stranger = signed_in_client(client_factory, email="stranger@example.com")

    response = stranger.get(f"/campaigns/{campaign_id}/statblock", params={"name": PC})

    assert response.status_code == 404
