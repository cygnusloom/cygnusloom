"""Species and cultures: the peoples a rule set or world offers the GM."""

from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services import world
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import load_state, name_key
from cygnusloom.services.state import CampaignState
from cygnusloom.services.views import get_statblock
from tests.conftest import content_library
from tests.factories import (
    campaign_ready_fast,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    place_pc,
    send_message,
    signed_in_client,
)
from tests.support import DEFAULT_RULE_SET, content_tree, world_definitions
from tests.test_dnd_srd_play import FIGHTER_FIELDS

LIBRARY = content_library()
DND = LIBRARY.rule_sets["dnd-srd-2024"]
WWN = LIBRARY.rule_sets["worldswithoutnumber"]
CAIRN = LIBRARY.rule_sets["cairn2e"]
SPEC = LIBRARY.rule_sets["rolltest"].character
TOOLS = LIBRARY.rule_sets["rolltest"].tool_calls


def _tools(app: FastAPI, campaign_id: int, species=DND.species) -> world.WorldTools:
    return world.WorldTools(
        engine=app.state.engine,
        campaign_id=campaign_id,
        tables=LIBRARY.core.tables,
        rng=random.Random(0),
        character=SPEC,
        plus="sum",
        species=species,
    )


def _state(app: FastAPI, campaign_id: int) -> CampaignState:
    with Session(app.state.engine, expire_on_commit=False) as db:
        return load_state(db, campaign_id)


def _campaign(
    client_factory: Callable[[], TestClient],
) -> tuple[TestClient, int]:
    return campaign_ready_fast(client_factory)


def test_only_rule_sets_and_worlds_declare_peoples():
    assert [entry.name for entry in DND.species] == [
        "Dragonborn",
        "Dwarf",
        "Elf",
        "Gnome",
        "Goliath",
        "Halfling",
        "Human",
        "Orc",
        "Tiefling",
    ]
    # World Without Number is humanocentric, with the dwarf and elf origins.
    assert [entry.name for entry in WWN.species] == ["Human", "Dwarf", "Elf"]
    # Core declares no peoples, so a rule set that does not name its own leaves the GM free.
    assert LIBRARY.core.species == ()
    assert CAIRN.species == ()
    assert LIBRARY.rule_sets["academia-arcana"].species == ()
    assert all(entry.culture and entry.blurb for entry in DND.species)


def test_a_world_may_replace_the_rule_sets_peoples(tmp_path: Path):
    tree = content_tree(
        tmp_path,
        definitions=[
            {
                "type": "species",
                "id": "dwarf",
                "name": "Dwarf",
                "plural": "dwarves",
                "adjective": "dwarven",
                "blurb": "Stone folk.",
                "culture": "They live under the mountain.",
            }
        ],
        world=[
            *world_definitions(),
            {
                "type": "species",
                "id": "deep-elf",
                "name": "Deep Elf",
                "plural": "deep elves",
                "adjective": "deep-elven",
                "blurb": "Pale folk of the under roads.",
                "culture": "They never see the sun.",
            },
        ],
    )
    library = ContentLibrary.load(tree)
    assert [entry.name for entry in library.rule_sets[DEFAULT_RULE_SET].species] == ["Dwarf"]
    assert [entry.name for entry in library.world_templates["testland"].species] == ["Deep Elf"]


def test_a_people_needs_its_whole_name(tmp_path: Path):
    tree = content_tree(
        tmp_path,
        definitions=[{"type": "species", "id": "dwarf", "name": "Dwarf", "blurb": "Stone folk."}],
    )
    with pytest.raises(ContentError, match="'plural' must be a non-empty string"):
        ContentLibrary.load(tree)


def test_two_peoples_cannot_share_a_name(tmp_path: Path):
    tree = content_tree(
        tmp_path,
        definitions=[
            {
                "type": "species",
                "id": "elf",
                "name": "Elf",
                "plural": "elves",
                "adjective": "elven",
                "blurb": "Forest folk.",
                "culture": "They keep to the trees.",
            },
            {
                "type": "species",
                "id": "elf-two",
                "name": "Elf",
                "plural": "elf-two",
                "adjective": "elf-two",
                "blurb": "Another kind of elf.",
                "culture": "They keep to the hills.",
            },
        ],
    )
    with pytest.raises(ContentError, match="both call themselves"):
        ContentLibrary.load(tree)


def test_a_place_records_its_majority_people(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _client, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    created = tools.create_location(
        "Stonehold", "A hall under the crag.", majority_species="Dwarven"
    )
    assert created["majority_species"] == "dwarves"
    state = _state(app, campaign_id)
    assert state.places[name_key("Stonehold")].majority_species == "dwarves"

    updated = tools.update_location("Stonehold", majority_species="mixed")
    assert updated["majority_species"] == "mixed"
    state = _state(app, campaign_id)
    assert state.places[name_key("Stonehold")].majority_species == "mixed"

    tools.update_location("Stonehold", majority_species="")
    assert _state(app, campaign_id).places[name_key("Stonehold")].majority_species == ""


def test_an_unknown_people_is_refused(client_factory: Callable[[], TestClient], app: FastAPI):
    _client, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    with pytest.raises(InvalidInput, match="not one of the peoples here"):
        tools.create_location("Nowhere", "An empty hall.", majority_species="Klingon")
    tools.create_location("Stonehold", "A hall under the crag.")
    with pytest.raises(InvalidInput, match="or Tiefling"):
        tools.create_npc("Orin", "Stonehold", "A smith.", species="Klingon")


def test_a_new_npc_takes_the_places_majority_people(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _client, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_location("Stonehold", "A hall under the crag.", majority_species="dwarves")
    borin = tools.create_npc("Borin", "Stonehold", "A smith with soot in his beard.")
    assert borin["species"] == "Dwarf"
    lia = tools.create_npc("Lia", "Stonehold", "A trader from the woods.", species="elves")
    assert lia["species"] == "Elf"
    state = _state(app, campaign_id)
    assert state.characters[name_key("Borin")].species == "Dwarf"
    assert state.characters[name_key("Lia")].species == "Elf"

    changed = tools.update_npc("Borin", species="gnomes")
    assert changed["species"] == "Gnome"
    assert _state(app, campaign_id).characters[name_key("Borin")].species == "Gnome"


def test_a_mixed_place_gives_a_new_npc_no_default_people(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _client, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_location("Crossroads", "A busy meeting of roads.", majority_species="mixed")
    result = tools.create_npc("Tan", "Crossroads", "A porter between wagons.")
    assert "species" not in result
    assert _state(app, campaign_id).characters[name_key("Tan")].species == ""


def test_lookup_species_reads_the_cultures(client_factory: Callable[[], TestClient], app: FastAPI):
    _client, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    every = tools.lookup_species()
    assert [entry["name"] for entry in every["peoples"]] == [entry.name for entry in DND.species]
    one = tools.lookup_species("dwarves")
    assert one["peoples"][0]["name"] == "Dwarf"
    assert "clans live in halls" in one["peoples"][0]["culture"]
    with pytest.raises(InvalidInput, match="not one of the peoples here"):
        tools.lookup_species("Klingon")
    empty = _tools(app, campaign_id, species=()).lookup_species()
    assert empty["peoples"] == []
    assert "declare no peoples" in empty["note"]


def test_a_campaign_with_no_peoples_records_none(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _client, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, species=())
    with pytest.raises(InvalidInput, match="declare no peoples"):
        tools.create_location("Nowhere", "An empty hall.", majority_species="dwarves")
    with pytest.raises(InvalidInput, match="declare no peoples"):
        tools.create_npc("Orin", "Mossbell Hollow", "A smith.", species="Dwarf")
    result = tools.create_npc("Orin", "Mossbell Hollow", "A smith.")
    assert "species" not in result


def test_the_orientation_names_the_peoples_and_a_places_majority(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign(client_factory)
    place_pc(client, campaign_id, "Mossbell Hollow")
    tools = _tools(app, campaign_id)
    tools.update_location("Mossbell Hollow", majority_species="gnomes")
    tools.create_npc("Nim", "Mossbell Hollow", "A tinker mending a pot.")

    text = world.world_state_text(
        _state(app, campaign_id), LIBRARY.rule_sets["rolltest"].campaign, species=DND.species
    )
    assert "Peoples of this world: Dragonborn —" in text
    assert "Mossbell Hollow is mostly gnomes." in text
    assert "create_npc takes species" in text
    assert "Nim (Gnome;" in text


def test_a_turn_tells_the_gm_the_peoples(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(client, campaign_id, fields=FIGHTER_FIELDS)
    close_opening(client, campaign_id)
    response = send_message(
        client,
        campaign_id,
        'Look around. __FAKE__:{"turns": [{"text": "The road is quiet."}]}',
    )
    assert response.status_code == 303, response.text
    system = "\n".join(
        message.get("content", "")
        for messages in fake.calls
        for message in messages
        if message.get("role") == "system"
    )
    assert "Peoples of this world: Dragonborn —" in system
    assert "create_npc takes species" in system


def test_the_statblock_names_an_npcs_people(client_factory: Callable[[], TestClient], app: FastAPI):
    _client, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_location("Stonehold", "A hall under the crag.", majority_species="dwarves")
    tools.create_npc("Borin", "Stonehold", "A smith.")
    block = get_statblock(_state(app, campaign_id), "Borin", SPEC, include_description=False)
    assert "People: Dwarf" in block["statblock"]
