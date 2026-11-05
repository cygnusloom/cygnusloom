from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.content.character import NO_CAMPAIGN, TableOption, choice_options, option_fields
from cygnusloom.content.library import InventoryRules, LocationTemplate, WorldTerrain
from cygnusloom.db.models import Event
from cygnusloom.llm.base import ChatReply, ToolRequest
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.rules.context import ToolContext, brief
from cygnusloom.services import world
from cygnusloom.services.campaigns import SPEECH_ATTEMPTS, list_messages
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    NPC_KIND,
    CampaignState,
    Character,
    Harmed,
    ItemAdded,
    Position,
    load_state,
    record,
)
from cygnusloom.services.tools import ToolRun, run_tool
from tests.conftest import content_library, csrf_token
from tests.factories import (
    campaign_ready,
    create_campaign,
    create_character,
    place_pc,
    send_message,
    signed_in_client,
)

LIBRARY = content_library()
# The neutral contract rule set, which inherits the core world tools.
CONTRACT = LIBRARY.rule_sets["rolltest"]
TOOLS = CONTRACT.tool_calls
TABLES = LIBRARY.core.tables
# Shipped Cairn content, for the tests about its own campaign options and world.
CAIRN = LIBRARY.rule_sets["cairn2e"]
CAIRN_TOOLS = CAIRN.tool_calls
# Neutral rules for a generic view test, so it carries no source book's content.
INVENTORY = InventoryRules(
    capacity=10,
    penalty_name="Overloaded",
    penalty_lasts="until a full night's rest",
    penalty_item="Fatigue",
    penalty_slots=1,
)
MAP_GROUND = {"lake": WorldTerrain(id="lake", name="Lake", color="#3f6f8f", rule="")}


def _campaign_id(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    """A signed-in client and a campaign past its opening turn, from the shared factory."""
    return campaign_ready(client_factory)


def _values(table: str) -> list[str]:
    return [option.value for option in TABLES[table]]


SPEC = CONTRACT.character
BUNDLED_NPCS = {
    "Mara Thistledown": "Mossbell Hollow",
    "Brin Gallowcap": "Glowcap Ring",
    "Old Hob": "Spore Bridge",
}


def _ctx(
    app: FastAPI,
    campaign_id: int,
    speak: Callable[[str, str, str, bool], Any] | None = None,
) -> ToolContext:
    async def quiet_speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        return None

    return ToolContext(
        world=world.WorldTools(
            engine=app.state.engine,
            campaign_id=campaign_id,
            tables=TABLES,
            rng=random.Random(0),
            character=SPEC,
            plus="sum",
        ),
        speak=speak or quiet_speak,
        ooc=lambda narrate: None,
        rules=CONTRACT.rules,
        lookup_tools={
            tool.name: brief(tool.description)
            for tool in CONTRACT.tool_calls.values()
            if tool.lookup
        },
    )


def _tool(app: FastAPI, campaign_id: int, name: str, arguments: dict[str, Any], speak=None) -> dict:
    request = ToolRequest(id="call_1", name=name, arguments=json.dumps(arguments))
    run = asyncio.run(run_tool(TOOLS, request, None, _ctx(app, campaign_id, speak)))
    assert isinstance(run, ToolRun)
    return json.loads(run.output)


def _give_the_map_ground(app: FastAPI, campaign_id: int) -> None:
    """Covers the test positions with a region, since a placed location needs ground around it."""
    tools = world.WorldTools(
        engine=app.state.engine,
        campaign_id=campaign_id,
        tables=TABLES,
        rng=random.Random(0),
        character=SPEC,
        plus="sum",
        world_terrain=MAP_GROUND,
    )
    tools.create_region("Test Ground", "lake", [[-12, -12], [12, -12], [12, 12], [-12, 12]])


def _db(app: FastAPI) -> Session:
    return Session(app.state.engine, expire_on_commit=False)


def _state(app: FastAPI, campaign_id: int) -> CampaignState:
    with _db(app) as db:
        return load_state(db, campaign_id)


def _actors(app: FastAPI, campaign_id: int) -> dict[str, Character]:
    return {c.name: c for c in _state(app, campaign_id).characters.values()}


def test_player_character_starts_unplaced(client_factory: Callable[[], TestClient], app: FastAPI):
    _, campaign_id = _campaign_id(client_factory)
    state = _state(app, campaign_id)
    pc = state.pc()
    assert pc is not None
    assert pc.name == "Ada"
    assert pc.location is None
    assert "Player character: Ada, not yet placed; place them with move_actor" in (
        world.world_state_text(state, NO_CAMPAIGN)
    )


def test_the_locations_rule_uses_an_authored_route_before_inventing_a_place():
    rule = LIBRARY.core.rules["locations"]
    assert (
        "do not invent a corridor, passage, or other place between two places an authored route connects"
        in rule
    )


def test_the_gm_sees_the_places_inside_the_players_location(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "create_location", {"name": "Old Keep", "description": "A keep."})
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Gate Room", "description": "A gate.", "parent": "Old Keep"},
    )
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Armoury", "description": "Racks.", "parent": "Old Keep"},
    )
    _tool(app, campaign_id, "discover_location", {"name": "Gate Room"})
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Old Keep"},
    )
    text = world.world_state_text(_state(app, campaign_id), NO_CAMPAIGN)
    assert "Places inside Old Keep: Armoury (not yet discovered), Gate Room (discovered)." in text

    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Gate Room"},
    )
    text = world.world_state_text(_state(app, campaign_id), NO_CAMPAIGN)
    assert "Gate Room is inside Old Keep." in text
    assert "Places inside Gate Room" not in text


def test_a_world_can_declare_a_place_inside_another(
    client_factory: Callable[[], TestClient], app: FastAPI, monkeypatch: pytest.MonkeyPatch
):
    template = app.state.content.world_templates["testhollow"]
    inn = LocationTemplate(
        id="drowsy-cap",
        name="The Drowsy Cap",
        description="A tavern carved under the oak.",
        parent="mossbell-hollow",
    )
    world = replace(template, locations=(*template.locations, inn))
    monkeypatch.setitem(app.state.content.world_templates, "testhollow", world)
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client)
    drowsy = _state(app, campaign_id).places["the drowsy cap"]
    assert drowsy.parent == "Mossbell Hollow"
    assert drowsy.position is None
    assert not drowsy.discovered


def test_bundled_npcs_are_placed_at_their_locations(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    npcs = {
        c.name: c.location
        for c in _state(app, campaign_id).characters.values()
        if c.kind == NPC_KIND
    }
    assert npcs == BUNDLED_NPCS


def test_npcs_without_listed_traits_get_random_ones(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    actors = _actors(app, campaign_id)
    for name in ("Brin Gallowcap", "Old Hob"):
        npc = actors[name]
        assert npc.personality in _values("personality")
        assert npc.accent in _values("accent")
        assert len(npc.quirks) == world.QUIRKS_PER_NPC
        assert set(npc.quirks) <= set(_values("quirk"))
        assert len(set(npc.quirks)) == world.QUIRKS_PER_NPC


def test_listed_npc_traits_are_kept(client_factory: Callable[[], TestClient], app: FastAPI):
    _, campaign_id = _campaign_id(client_factory)
    mara = _actors(app, campaign_id)["Mara Thistledown"]
    assert mara.personality == "warm and protective"
    assert mara.accent == "soft and lilting"
    assert mara.quirks == ["refers to everyone as 'friend'", "speaks in proverbs"]


def test_character_name_cannot_match_an_npc(client_factory: Callable[[], TestClient]):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client)
    rejected = create_character(client, campaign_id, name="old hob")
    assert rejected.status_code == 400
    assert "already used" in rejected.text


def test_blank_character_name_is_rejected(client_factory: Callable[[], TestClient]):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client)
    assert create_character(client, campaign_id, name="   ").status_code == 400


def test_random_traits_come_from_the_given_tables(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    tables = {
        "personality": (TableOption(name=None, description="gruff"),),
        "accent": (TableOption(name=None, description="burr"),),
        "quirk": tuple(
            TableOption(name=None, description=quirk) for quirk in ("hums", "sniffs", "winks")
        ),
    }
    with _db(app) as db:
        result = world.create_npc(
            db,
            campaign_id,
            tables,
            random.Random(3),
            SPEC,
            name="Pip",
            location="Mossbell Hollow",
            description="A tinker.",
            monsters={},
            plus="sum",
            npc_hp={},
            npc_hp_stat=None,
        )
        db.commit()
    assert result["personality"] == "gruff"
    assert result["accent"] == "burr"
    assert len(result["quirks"]) == 2
    assert set(result["quirks"]) <= {"hums", "sniffs", "winks"}


def test_too_many_quirks_are_rejected(client_factory: Callable[[], TestClient], app: FastAPI):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db, pytest.raises(InvalidInput):
        world.create_npc(
            db,
            campaign_id,
            TABLES,
            random.Random(0),
            SPEC,
            name="Pip",
            location="Mossbell Hollow",
            description="",
            quirks=["x"] * (world.MAX_QUIRKS + 1),
            monsters={},
            plus="sum",
            npc_hp={},
            npc_hp_stat=None,
        )


def test_gm_creates_a_location_and_an_npc_there(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    place = _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Hollow Pond", "description": "Still, cold water."},
    )
    assert place == {"created": "location", "name": "Hollow Pond", "discovered": False}
    npc = _tool(
        app,
        campaign_id,
        "create_npc",
        {
            "name": "Reed",
            "location": "hollow pond",
            "description": "A fisher.",
            "personality": "Patient",
        },
    )
    assert npc["location"] == "Hollow Pond"
    assert npc["personality"] == "Patient"
    assert len(npc["quirks"]) == world.QUIRKS_PER_NPC


def test_gm_records_npc_pronouns_and_they_replay(client_factory: Callable[[], TestClient], app):
    client, campaign_id = _described_campaign(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    npc = _tool(
        app,
        campaign_id,
        "create_npc",
        {
            "name": "Wren",
            "location": "Glowcap Ring",
            "description": "A reed-cutter.",
            "pronouns": "she/her",
        },
    )
    assert npc["pronouns"] == "she/her"
    assert _actors(app, campaign_id)["Wren"].pronouns == "she/her"
    text = world.world_state_text(_state(app, campaign_id), SPEC)
    assert "Pronouns: Wren, she/her." in text
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Pronouns: she/her." in page


def test_npc_without_pronouns_still_works_and_omits_the_line(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _described_campaign(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    npc = _tool(
        app,
        campaign_id,
        "create_npc",
        {"name": "Tobb", "location": "Glowcap Ring", "description": "A stonemason."},
    )
    assert npc["pronouns"] is None
    assert _actors(app, campaign_id)["Tobb"].pronouns is None
    text = world.world_state_text(_state(app, campaign_id), SPEC)
    assert "Tobb" in text
    assert "Pronouns: Tobb" not in text


def test_location_and_character_names_are_unique(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    error = _tool(
        app, campaign_id, "create_location", {"name": "mossbell hollow", "description": "Again."}
    )
    assert "error" in error
    error = _tool(
        app,
        campaign_id,
        "create_npc",
        {"name": "Ada", "location": "Mossbell Hollow", "description": ""},
    )
    assert "error" in error


def test_a_place_cannot_repeat_an_existing_one_under_an_article(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    error = _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "The Mossbell Hollow", "description": "Again."},
    )
    assert "error" in error
    assert "discover_location" in error["error"]


def test_npc_character_values_come_from_the_lookup(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    field = next(iter(option_fields(CAIRN.character)))
    value = choice_options(CAIRN.character, field)["options"][0]["value"]
    with _db(app) as db, pytest.raises(InvalidInput, match=repr(field)):
        world.create_npc(
            db,
            campaign_id,
            TABLES,
            random.Random(0),
            CAIRN.character,
            name="Pip",
            location="Mossbell Hollow",
            description="A tinker.",
            character={"fields": {field: "Not a value"}},
            monsters={},
            plus=CAIRN.plus,
            npc_hp={},
            npc_hp_stat=None,
        )
    with _db(app) as db:
        world.create_npc(
            db,
            campaign_id,
            TABLES,
            random.Random(0),
            CAIRN.character,
            name="Pip",
            location="Mossbell Hollow",
            description="A tinker.",
            character={"fields": {field: value}},
            monsters={},
            plus=CAIRN.plus,
            npc_hp={},
            npc_hp_stat=None,
        )
        db.commit()


def test_npc_needs_a_known_location(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    error = _tool(
        app, campaign_id, "create_npc", {"name": "Reed", "location": "Nowhere", "description": ""}
    )
    assert "no location named 'Nowhere'" in error["error"]


def test_moving_the_player_changes_the_world_state(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    moved = _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    assert moved == {"moved": "Ada", "location": "Glowcap Ring"}
    text = world.world_state_text(_state(app, campaign_id), NO_CAMPAIGN)
    assert "Player character: Ada, at Glowcap Ring." in text
    assert "NPCs here: Brin Gallowcap." in text


def test_moving_needs_a_known_character_and_location(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    assert (
        "no character named 'Nobody'"
        in _tool(
            app,
            campaign_id,
            "move_actor",
            {"mode": "in_settlement", "name": "Nobody", "location": "Glowcap Ring"},
        )["error"]
    )
    assert (
        "no location named 'Nowhere'"
        in _tool(
            app,
            campaign_id,
            "move_actor",
            {"mode": "in_settlement", "name": "Ada", "location": "Nowhere"},
        )["error"]
    )


def test_an_npc_can_go_into_the_wind_and_be_brought_back(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    gone = _tool(
        app, campaign_id, "move_actor", {"mode": "in_settlement", "name": "Brin Gallowcap"}
    )
    assert gone == {"moved": "Brin Gallowcap", "location": "in the wind"}
    text = world.world_state_text(_state(app, campaign_id), NO_CAMPAIGN)
    assert "NPCs here: none." in text
    assert "NPCs in the wind, at no location: Brin Gallowcap." in text

    back = _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Brin Gallowcap", "location": "Glowcap Ring"},
    )
    assert back == {"moved": "Brin Gallowcap", "location": "Glowcap Ring"}
    text = world.world_state_text(_state(app, campaign_id), NO_CAMPAIGN)
    assert "NPCs here: Brin Gallowcap." in text
    assert "in the wind" not in text


def test_the_player_character_cannot_go_into_the_wind(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    error = _tool(app, campaign_id, "move_actor", {"mode": "in_settlement", "name": "Ada"})
    assert "cannot go into the wind" in error["error"]


def test_notes_attach_to_the_campaign_a_location_or_a_character(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        world.add_note(db, campaign_id, "Find the lost key.")
        world.add_note(db, campaign_id, "The bridge sways.", subject="Spore Bridge")
        world.add_note(db, campaign_id, "Hob owes Mara a favour.", subject="Old Hob")
        db.commit()
    notes = _state(app, campaign_id).notes
    assert [(note.subject, note.text) for note in notes] == [
        (world.CAMPAIGN_WIDE, "Find the lost key."),
        ("Spore Bridge", "The bridge sways."),
        ("Old Hob", "Hob owes Mara a favour."),
    ]


def _described_campaign(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    """A campaign whose player character has an appearance, so the sheet has lines to show."""
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client)
    created = create_character(
        client,
        campaign_id,
        fields={"appearance": "Tall, with a red scarf.", "biography": "Raised in the reeds."},
    )
    assert created.status_code == 303, created.text
    return client, campaign_id


def test_the_player_sees_only_what_is_at_their_location(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _described_campaign(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    state = _state(app, campaign_id)
    scene = world.scene_view(state, SPEC)
    assert scene.pc is not None and scene.pc.name == "Ada"
    assert scene.location is not None and scene.location.name == "Glowcap Ring"
    assert [npc.name for npc in scene.npcs] == ["Brin Gallowcap"]
    assert scene.sheet == world.get_statblock(state, "Ada", SPEC)["statblock"]
    assert "Appearance: Tall, with a red scarf." in scene.sheet


def test_an_unplaced_player_sees_no_location_or_npcs(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    scene = world.scene_view(_state(app, campaign_id), SPEC)
    assert scene.location is None
    assert scene.npcs == []


def test_the_pack_lists_items_with_uses_slots_and_capacity(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        record(db, campaign_id, ItemAdded(name="Ada", item="Rope", size="normal", slots=1))
        record(
            db,
            campaign_id,
            ItemAdded(name="Ada", item="Rations", size="petty", slots=0, uses=2, max_uses=3),
        )
        record(
            db,
            campaign_id,
            ItemAdded(name="Ada", item="Fatigue", size="", slots=0, kind_of_item="fatigue"),
        )
        db.commit()
    state = _state(app, campaign_id)
    pack = world.scene_view(state, SPEC, INVENTORY).pack
    assert pack is not None
    assert [item.name for item in pack.items] == ["Rope", "Rations", "Fatigue"]
    rations = pack.items[1]
    assert (rations.slots, rations.uses, rations.max_uses) == (0, 2, 3)
    assert pack.used == 1
    assert pack.capacity == INVENTORY.capacity


def test_the_pack_is_absent_without_a_character_or_an_inventory_rule(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    assert world.scene_view(_state(app, campaign_id), SPEC).pack is None
    assert world.scene_view(CampaignState(), SPEC, INVENTORY).pack is None


def test_all_at_location_describes_a_place_without_statblocks(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    with _db(app) as db:
        world.add_note(db, campaign_id, "The ring hums.", subject="Glowcap Ring")
        world.add_note(db, campaign_id, "Brin owes a debt.", subject="Brin Gallowcap")
        world.add_note(db, campaign_id, "Hob hides a key.", subject="Old Hob")
        db.commit()
    state = _state(app, campaign_id)
    here = world.all_at_location(state, None)
    assert here["location"]["name"] == "Glowcap Ring"
    assert here["player_character_here"] is True
    assert [npc["name"] for npc in here["npcs"]] == ["Brin Gallowcap"]
    assert all("statblock" not in npc for npc in here["npcs"])
    assert {note["subject"] for note in here["notes"]} == {"Glowcap Ring", "Brin Gallowcap"}
    away = world.all_at_location(state, "Spore Bridge")
    assert away["player_character_here"] is False
    assert [npc["name"] for npc in away["npcs"]] == ["Old Hob"]
    assert [note["subject"] for note in away["notes"]] == ["Old Hob"]
    assert _tool(app, campaign_id, "all_at_location", {"location": "Spore Bridge"})["location"] == {
        "name": "Spore Bridge",
        "description": away["location"]["description"],
        "summary": None,
        "discovered": False,
        "position_km": {"x": -1.2, "y": 1.4},
        "parent": None,
    }


def test_a_defeated_npc_is_marked_as_a_body_where_it_fell(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    with _db(app) as db:
        record(db, campaign_id, Harmed(name="Brin Gallowcap", stat="str", taken=0, defeats=True))
        db.commit()
    here = world.all_at_location(_state(app, campaign_id), None)
    assert [(npc["name"], npc["defeated"]) for npc in here["npcs"]] == [("Brin Gallowcap", True)]


def test_all_at_location_needs_a_location_when_the_player_has_none(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    with pytest.raises(InvalidInput):
        world.all_at_location(_state(app, campaign_id), None)
    assert "error" in _tool(app, campaign_id, "all_at_location", {})


def test_search_lookups_match_every_word_of_npc_and_location_details(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    found = _tool(app, campaign_id, "search_npc", {"query": "ranger glowcap"})
    assert [npc["name"] for npc in found["npcs"]] == ["Brin Gallowcap"]
    assert _tool(app, campaign_id, "search_npc", {"query": "dragon"}) == {"npcs": []}
    places = _tool(app, campaign_id, "search_location", {"query": "bridge"})
    assert [place["name"] for place in places["locations"]] == ["Spore Bridge"]


def test_get_statblock_returns_the_player_or_an_npc_on_request(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _described_campaign(client_factory)
    state = _state(app, campaign_id)
    pc = world.get_statblock(state, "Ada", SPEC)
    assert pc["kind"] == "player character"
    assert "Appearance: Tall, with a red scarf." in pc["statblock"]
    npc = _tool(app, campaign_id, "get_statblock", {"name": "Brin Gallowcap"})
    assert npc["kind"] == "npc"
    assert npc["location"] == "Glowcap Ring"
    assert isinstance(npc["statblock"], list)
    assert "error" in _tool(app, campaign_id, "get_statblock", {"name": "Nobody"})


def test_statblocks_stay_out_of_the_gm_orientation(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    text = world.world_state_text(_state(app, campaign_id), NO_CAMPAIGN)
    assert "Stats" not in text
    assert "Brin Gallowcap" in text


def test_note_subject_must_exist_and_text_must_not_be_blank(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        with pytest.raises(InvalidInput):
            world.add_note(db, campaign_id, "Hello.", subject="Nobody")
        with pytest.raises(InvalidInput):
            world.add_note(db, campaign_id, "   ")


def test_gm_adds_and_searches_notes_through_tools(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    added = _tool(
        app,
        campaign_id,
        "add_note",
        {"text": "The bridge is unsafe.", "subject": "Spore Bridge"},
    )
    assert added == {"noted": True, "id": added["id"], "subject": "Spore Bridge"}
    _tool(app, campaign_id, "add_note", {"text": "Hob hides the key."})
    found = _tool(app, campaign_id, "search_notes", {"query": "bridge"})
    assert found == {
        "notes": [{"id": added["id"], "subject": "Spore Bridge", "text": "The bridge is unsafe."}]
    }


def test_gm_updates_and_deletes_a_location(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "create_location", {"name": "Test Ledge", "description": "A ledge."})
    updated = _tool(
        app,
        campaign_id,
        "update_location",
        {"name": "Test Ledge", "description": "A ledge, now crumbling."},
    )
    assert updated == {"updated": "location", "name": "Test Ledge"}
    assert _state(app, campaign_id).named("Test Ledge").description == "A ledge, now crumbling."
    deleted = _tool(app, campaign_id, "delete_location", {"name": "Test Ledge"})
    assert deleted == {"deleted": "location", "name": "Test Ledge"}
    assert _state(app, campaign_id).named("Test Ledge") is None


def test_a_location_with_someone_in_it_cannot_be_deleted(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "create_location", {"name": "Test Ledge", "description": "A ledge."})
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Test Ledge"},
    )
    refused = _tool(app, campaign_id, "delete_location", {"name": "Test Ledge"})
    assert "still has Ada there" in refused["error"]
    assert _state(app, campaign_id).named("Test Ledge") is not None


def test_gm_updates_an_npc_and_keeps_the_fields_not_given(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    before = _actors(app, campaign_id)["Mara Thistledown"]
    _tool(
        app,
        campaign_id,
        "update_npc",
        {"name": "Mara Thistledown", "description": "Now guards the bridge."},
    )
    after = _actors(app, campaign_id)["Mara Thistledown"]
    assert after.description == "Now guards the bridge."
    assert (after.personality, after.accent, after.quirks) == (
        before.personality,
        before.accent,
        before.quirks,
    )

    _tool(app, campaign_id, "update_npc", {"name": "Mara Thistledown", "quirks": ["hums"]})
    assert _actors(app, campaign_id)["Mara Thistledown"].quirks == ["hums"]

    refused = _tool(app, campaign_id, "update_npc", {"name": "Mara Thistledown"})
    assert "at least one" in refused["error"]


def test_the_player_character_cannot_be_updated_as_an_npc_or_deleted(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    updated = _tool(app, campaign_id, "update_npc", {"name": "Ada", "description": "Changed."})
    deleted = _tool(app, campaign_id, "delete_npc", {"name": "Ada"})
    assert "not an NPC" in updated["error"]
    assert "not an NPC" in deleted["error"]
    assert _actors(app, campaign_id)["Ada"].kind != NPC_KIND


def test_gm_deletes_an_npc_and_keeps_notes_about_them(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "add_note", {"text": "Brin owes a debt.", "subject": "Brin Gallowcap"})
    deleted = _tool(app, campaign_id, "delete_npc", {"name": "Brin Gallowcap"})
    assert deleted == {"deleted": "npc", "name": "Brin Gallowcap"}
    state = _state(app, campaign_id)
    assert state.named("Brin Gallowcap") is None
    assert [note.subject for note in state.notes] == ["Brin Gallowcap"]


def test_a_note_named_campaign_wide_is_not_about_any_subject(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    added = _tool(
        app, campaign_id, "add_note", {"text": "The moon is red.", "subject": "campaign-wide"}
    )
    assert added["subject"] == "campaign-wide"
    assert [note.subject for note in _state(app, campaign_id).notes] == ["campaign-wide"]


def test_gm_updates_and_deletes_notes_by_id(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    added = _tool(app, campaign_id, "add_note", {"text": "The bridge sways."})
    note_id = added["id"]
    updated = _tool(app, campaign_id, "update_note", {"note_id": note_id, "text": "It collapsed."})
    assert updated == {"updated": "note", "id": note_id}
    notes = _state(app, campaign_id).notes
    assert [(note.id, note.text) for note in notes] == [(note_id, "It collapsed.")]

    deleted = _tool(app, campaign_id, "delete_note", {"note_id": note_id})
    assert deleted == {"deleted": "note", "id": note_id}
    assert _state(app, campaign_id).notes == []
    missing = _tool(app, campaign_id, "delete_note", {"note_id": note_id})
    assert "no note with id" in missing["error"]


def test_search_needs_every_word_and_returns_newest_first(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        world.add_note(db, campaign_id, "The bridge is unsafe.", subject="Spore Bridge")
        world.add_note(db, campaign_id, "Hob hides the key.", subject="Old Hob")
        world.add_note(db, campaign_id, "Find the key.")
        db.commit()
    state = _state(app, campaign_id)
    keys = world.search_notes(state, "key")["notes"]
    both = world.search_notes(state, "hob key")["notes"]
    none = world.search_notes(state, "dragon")["notes"]
    assert [note["text"] for note in keys] == ["Find the key.", "Hob hides the key."]
    assert [note["subject"] for note in both] == ["Old Hob"]
    assert none == []


def test_search_returns_every_matching_note(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        for index in range(15):
            world.add_note(db, campaign_id, f"Clue {index}.")
        db.commit()
        found = world.search_notes(load_state(db, campaign_id), "clue")["notes"]
    assert len(found) == 15


def test_speaker_profile_describes_an_npc_at_the_players_location(
    client_factory: Callable[[], TestClient], app
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    profile = world.speaker_profile(_state(app, campaign_id), "Mara Thistledown", SPEC)
    assert profile.splitlines()[0] == "Speaker: Mara Thistledown"
    assert "Personality: warm and protective" in profile
    assert "Location: Mossbell Hollow" in profile


def test_speaker_at_player_tells_whether_an_npc_is_present(
    client_factory: Callable[[], TestClient], app
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    state = _state(app, campaign_id)
    assert world.speaker_at_player(state, "Mara Thistledown") is True
    assert world.speaker_at_player(state, "Brin Gallowcap") is False


def test_speaker_profile_still_describes_an_npc_away_from_the_player(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    profile = world.speaker_profile(_state(app, campaign_id), "Brin Gallowcap", SPEC)
    assert profile.splitlines()[0] == "Speaker: Brin Gallowcap"


def test_the_player_character_still_cannot_be_spoken_for(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    state = _state(app, campaign_id)
    with pytest.raises(InvalidInput, match="does not need speaking for"):
        world.speaker_profile(state, "Ada", SPEC)


def test_speak_tool_queues_dialogue_and_returns_only_an_acknowledgment(
    client_factory: Callable[[], TestClient], app
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    asked: list[tuple[str, str, str]] = []

    async def speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        asked.append((speaker, profile, intent))

    result = _tool(
        app,
        campaign_id,
        "speak",
        {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
        speak=speak,
    )
    assert result == {"speaker": "Mara Thistledown", "status": "dialogue queued for the narration"}
    assert len(asked) == 1
    speaker, profile, intent = asked[0]
    assert speaker == "Mara Thistledown"
    assert profile.startswith("Speaker: Mara Thistledown")
    assert intent == "Warn Ada about the bog."


def test_speak_tool_warns_and_queues_nothing_for_an_npc_away_from_the_player(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    asked: list[str] = []

    async def speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        asked.append(profile)

    result = _tool(
        app, campaign_id, "speak", {"npc": "Old Hob", "intent": "Greet Ada."}, speak=speak
    )
    assert result["speaker"] == "Old Hob"
    assert result["status"] == "dialogue not queued"
    assert "force true" in result["warning"]
    assert "error" not in result
    assert asked == []


def test_speak_tool_with_force_queues_dialogue_for_an_npc_away_from_the_player(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    asked: list[tuple[str, str]] = []

    async def speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        asked.append((speaker, intent))

    result = _tool(
        app,
        campaign_id,
        "speak",
        {"npc": "Old Hob", "intent": "Call to Ada from the lane.", "force": True},
        speak=speak,
    )
    assert result == {"speaker": "Old Hob", "status": "dialogue queued for the narration"}
    assert asked == [("Old Hob", "Call to Ada from the lane.")]


def test_campaign_page_panels_show_only_the_scene(client_factory: Callable[[], TestClient], app):
    client, campaign_id = _described_campaign(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    with _db(app) as db:
        world.add_note(db, campaign_id, "Find the lost key.")
        world.add_note(db, campaign_id, "Mara sings.", subject="Mossbell Hollow")
        world.add_note(db, campaign_id, "Hob hides a key.", subject="Old Hob")
        db.commit()
    page = client.get(f"/campaigns/{campaign_id}").text

    characters = page[page.index('id="panel-location"') : page.index('id="panel-character"')]
    assert "Brin Gallowcap" in characters
    assert "Mara Thistledown" not in characters
    assert "Mossbell Hollow" not in characters

    sheet = page[page.index('id="panel-character"') : page.index('id="panel-inventory"')]
    assert "Tall, with a red scarf." in sheet
    assert ">Location<" in page
    assert ">Character<" in page
    assert ">Sheet<" not in page
    assert ">Notes<" not in page
    assert 'id="panel-notes"' not in page
    assert 'id="drawer-notes"' not in page
    assert "data-drawer-target=" not in page
    assert 'data-view="journal"' in page

    characters_page = page[page.index('id="character-list"') :]
    characters_page = characters_page[
        : characters_page.index("</section>", characters_page.index("People you know"))
    ]
    assert "Brin Gallowcap" in characters_page
    assert "Mara Thistledown" not in characters_page
    assert "Mossbell Hollow" not in characters_page

    # The activity log keeps its history of what the GM wrote, by design.
    assert "New location: Mossbell Hollow." in page


def test_campaign_page_maps_can_be_panned_and_zoomed(client_factory: Callable[[], TestClient]):
    client, campaign_id = _described_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text

    assert '<script src="/static/map_view.js"></script>' in page
    # The world map is framed in the Map view and in the side panel.
    assert page.count('<div class="map-frame">') >= 2


def test_campaign_page_escapes_character_names(client_factory: Callable[[], TestClient]):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client)
    assert create_character(client, campaign_id, name="<b>Zed</b>").status_code == 303
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "&lt;b&gt;Zed&lt;/b&gt;" in page
    assert "<b>Zed</b>" not in page


def test_speech_is_a_separate_call_and_only_the_narration_is_saved(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    speak_call = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    script = _script(
        {
            "rounds": [[speak_call]],
            "text": "Mara looks up from her kettle.",
            "speech": {"Mara Thistledown": "Mind the bog, dearie."},
        }
    )

    send_message(client, campaign_id, script)

    speech_calls = _speech_calls(fake)
    assert len(speech_calls) == 1
    assert "Speaker: Mara Thistledown" in json.dumps(speech_calls[0])
    assert "Intent: Warn Ada about the bog." in json.dumps(speech_calls[0])
    assert _saved(app, campaign_id)[1:] == [
        ("user", script),
        ("assistant", "Mara looks up from her kettle."),
    ]
    assert "Mind the bog, dearie." in json.dumps(fake.calls[-1])


class _EmptySpeech(FakeProvider):
    """Answers the first `empty` speech steps with no text."""

    def __init__(self, empty: int) -> None:
        super().__init__()
        self.empty = empty

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        if self.empty and "Speech step." in str(messages[0]["content"]):
            self.calls.append(messages)
            self.empty -= 1
            return ChatReply(text="")
        return await super().chat(model, messages, tools)


def _speech_calls(provider: FakeProvider) -> list[list[dict[str, Any]]]:
    """The messages the provider was sent for a speech step."""
    return [
        messages
        for messages in provider.calls
        if messages and "Speech step." in str(messages[0]["content"])
    ]


def _saved(app: FastAPI, campaign_id: int) -> list[tuple[str, str]]:
    """The campaign's saved transcript, as role and content pairs."""
    return [(m.role, m.content) for m in list_messages(app.state.engine, campaign_id)]


def _script(*turns: dict[str, Any]) -> str:
    return "__FAKE__:" + json.dumps({"turns": list(turns)})


def test_an_empty_speech_reply_is_retried(client_factory: Callable[[], TestClient], app: FastAPI):
    provider = _EmptySpeech(empty=1)
    client, campaign_id = _campaign_id(client_factory)
    app.state.providers = lambda model_id: (provider, model_id)
    place_pc(client, campaign_id)
    speak_call = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    script = _script(
        {
            "rounds": [[speak_call]],
            "text": "Mara looks up from her kettle.",
            "speech": {"Mara Thistledown": "Mind the bog, dearie."},
        }
    )

    send_message(client, campaign_id, script)

    assert len(_speech_calls(provider)) == 2
    assert _saved(app, campaign_id)[-1] == ("assistant", "Mara looks up from her kettle.")
    assert "Mind the bog, dearie." in json.dumps(provider.calls[-1])


def test_speech_retries_are_bounded(client_factory: Callable[[], TestClient], app: FastAPI):
    """Silence is retried a fixed number of times, then the turn carries on without the line."""
    provider = _EmptySpeech(empty=SPEECH_ATTEMPTS)
    client, campaign_id = _campaign_id(client_factory)
    app.state.providers = lambda model_id: (provider, model_id)
    place_pc(client, campaign_id)
    speak_call = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    script = _script({"rounds": [[speak_call]], "text": "Mara looks up.", "speech": {}})

    response = send_message(client, campaign_id, script)

    assert response.status_code == 303
    assert len(_speech_calls(provider)) == SPEECH_ATTEMPTS
    # The GM is told the line failed, and the reply the player waited for is still delivered.
    assert _saved(app, campaign_id)[-1] == ("assistant", "Mara looks up.")


def test_queued_dialogue_reaches_the_narration_as_json(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    speak_call = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    said = 'Mind the bog: "it swallows boots".\nThen run.'
    script = _script(
        {"rounds": [[speak_call]], "text": "Mara looks up.", "speech": {"Mara Thistledown": said}}
    )

    send_message(client, campaign_id, script)

    queued = [
        json.loads(m["content"])
        for m in fake.calls[-1]
        if m["role"] == "system" and "queued_dialogue" in str(m["content"])
    ]
    assert queued == [{"queued_dialogue": [{"speaker": "Mara Thistledown", "dialogue": said}]}]


def _non_user(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages the model wrote or was given by the GM, without the player's transcript."""
    return [m for m in messages if m["role"] != "user"]


def _contents(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content")) for m in _non_user(messages))


def test_an_npc_gets_one_line_per_reply(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    speak_call = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    script = _script(
        {
            "rounds": [[speak_call], [speak_call]],
            "text": "Mara looks up from her kettle.",
            "speech": {"Mara Thistledown": "Mind the bog, dearie."},
        }
    )

    send_message(client, campaign_id, script)

    assert len(_speech_calls(fake)) == 1
    assert any(
        "already has dialogue for this reply" in json.dumps(messages)
        and "replace: true" in json.dumps(messages)
        for messages in fake.calls
    )
    narration = json.dumps(_non_user(fake.calls[-1]))
    assert narration.count("Mind the bog, dearie.") == 1


def test_replace_amends_the_queued_line(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    first = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    amend = {
        "name": "speak",
        "arguments": {
            "npc": "Mara Thistledown",
            "intent": "Warn Ada about the bog and the lantern-moths.",
            "replace": True,
        },
    }
    script = _script({"rounds": [[first], [amend]], "text": "Mara looks up from her kettle."})

    send_message(client, campaign_id, script)

    assert any(
        "which this intent replaces: Warn Ada about the bog." in json.dumps(messages)
        for messages in fake.calls
    )
    narration = _contents(fake.calls[-1])
    assert '"dialogue": "Warn Ada about the bog and the lantern-moths."' in narration
    assert '"dialogue": "Warn Ada about the bog."' not in narration


def test_a_failed_replace_keeps_the_queued_line(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    speech_steps = 0
    inner_chat = fake.chat

    async def chat(model, messages, tools=None):
        nonlocal speech_steps
        if messages and "Speech step." in str(messages[0]["content"]):
            speech_steps += 1
            if speech_steps == 2:
                markup = '<tool_call>{"name": "create_note", "arguments": {}}</tool_call>'
                return ChatReply(text=markup)
        return await inner_chat(model, messages, tools)

    fake.chat = chat
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    first = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    amend = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada twice.", "replace": True},
    }
    script = _script({"rounds": [[first], [amend]], "text": "Mara looks up from her kettle."})

    send_message(client, campaign_id, script)

    narration = _contents(fake.calls[-1])
    assert '"dialogue": "Warn Ada about the bog."' in narration
    assert "<tool_call" not in narration


def test_speech_with_tool_call_markup_is_not_spoken(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    speak_call = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    markup = '<tool_call>{"name": "create_note", "arguments": {"text": "x"}}</tool_call>'
    script = _script(
        {
            "rounds": [[speak_call]],
            "text": "Mara turns back to her kettle.",
            "speech": {"Mara Thistledown": markup},
        }
    )

    send_message(client, campaign_id, script)

    assert any("wrote a tool call instead" in json.dumps(messages) for messages in fake.calls)
    narration = json.dumps(_non_user(fake.calls[-1]))
    assert "<tool_call" not in narration
    assert _saved(app, campaign_id)[1:] == [
        ("user", script),
        ("assistant", "Mara turns back to her kettle."),
    ]


def test_speech_with_tag_style_tool_markup_is_not_spoken(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    speak_call = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    markup = '<get_statblock name="Mara Thistledown"/>'
    script = _script(
        {
            "rounds": [[speak_call]],
            "text": "Mara turns back to her kettle.",
            "speech": {"Mara Thistledown": markup},
        }
    )

    send_message(client, campaign_id, script)

    assert any("wrote a tool call instead" in json.dumps(messages) for messages in fake.calls)
    narration = json.dumps(_non_user(fake.calls[-1]))
    assert "<get_statblock" not in narration


def test_speech_step_gets_no_rules_or_lookup_instructions(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    speak_call = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    script = _script({"rounds": [[speak_call]], "text": "Mara turns back to her kettle."})

    send_message(client, campaign_id, script)

    speech = _speech_calls(fake)
    assert speech
    system = str(speech[0][0]["content"])
    assert "Warn Ada about the bog." in system
    assert "lookup" not in system
    assert "Rules for this campaign" not in system


def test_speech_shows_a_writing_dialogue_status(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    place_pc(client, campaign_id)
    script = _script(
        {
            "rounds": [
                [
                    {
                        "name": "speak",
                        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada."},
                    }
                ]
            ],
            "text": "Mara looks up.",
            "speech": {"Mara Thistledown": "Mind the bog."},
        }
    )
    token = csrf_token(client, "/campaigns")

    response = client.post(
        f"/campaigns/{campaign_id}/messages/stream",
        data={"csrf_token": token, "content": script},
    )

    statuses = [
        json.loads(line)["text"]
        for line in response.text.splitlines()
        if line.strip() and json.loads(line)["type"] == "status"
    ]
    assert "Writing dialogue for Mara Thistledown..." in statuses
    assert "Running speak..." not in statuses


def test_player_character_waits_for_the_campaign_options(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template="twin-lakes", rule_set="cairn2e")
    with Session(app.state.engine) as db, pytest.raises(InvalidInput, match="campaign options"):
        world.create_player_character(
            db,
            campaign_id,
            CAIRN.campaign,
            CAIRN.character,
            "Mara",
            {},
            {},
            random.Random(0),
            plus=CAIRN.plus,
            pronouns="she/her",
        )


def test_campaign_options_are_stored_once_and_checked(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template="twin-lakes", rule_set="cairn2e")
    omen = CAIRN.campaign.tables["omen"][0].value
    with Session(app.state.engine) as db:
        with pytest.raises(InvalidInput):
            world.set_campaign_options(db, campaign_id, CAIRN.campaign, {"omen": "Not an omen"})
        assert load_state(db, campaign_id).options == {}
        world.set_campaign_options(db, campaign_id, CAIRN.campaign, {"omen": omen})
        assert load_state(db, campaign_id).options == {"omen": omen}
        with pytest.raises(InvalidInput, match="already chosen"):
            world.set_campaign_options(db, campaign_id, CAIRN.campaign, {"omen": omen})


LOOKUP_TOOL_NAMES = {
    "search_notes",
    "search_journal",
    "view_journal",
    "all_at_location",
    "search_location",
    "search_npc",
    "get_statblock",
    "character_options",
    "lookup_rule",
    "lookup_feature",
    "lookup_condition",
    "list_custom_features",
    "list_factions",
    "list_lookups",
    "list_dice_tables",
    "map_options",
    "roll_creative",
}


def test_lookup_rule_returns_a_rule_in_full_or_names_the_rules(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    found = _tool(app, campaign_id, "lookup_rule", {"rule": "notes"})
    assert found == {"rule": "notes", "text": LIBRARY.rule_sets["rolltest"].rules["notes"]}
    missing = _tool(app, campaign_id, "lookup_rule", {"rule": "nope"})
    assert "no rule 'nope'" in missing["error"]
    assert "notes" in missing["error"]


def test_list_lookups_gives_a_brief_of_every_tool_rule_location_and_npc(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    listing = _tool(app, campaign_id, "list_lookups", {})
    assert {entry["tool"] for entry in listing["tools"]} == LOOKUP_TOOL_NAMES
    assert all(entry["brief"] for entry in listing["tools"])
    fixture_rules = LIBRARY.rule_sets["rolltest"].rules
    assert {entry["rule"] for entry in listing["rules"]} == set(fixture_rules)
    assert all(entry["brief"] for entry in listing["rules"])
    locations = {entry["name"]: entry["brief"] for entry in listing["locations"]}
    assert "Mossbell Hollow" in locations
    npcs = {entry["name"]: entry for entry in listing["npcs"]}
    assert set(BUNDLED_NPCS) <= set(npcs)
    assert npcs["Mara Thistledown"]["location"] == BUNDLED_NPCS["Mara Thistledown"]
    assert all(entry["brief"] for entry in listing["npcs"])
    assert "Ada" not in npcs


def _event_kinds(app: FastAPI, campaign_id: int) -> list[str]:
    with _db(app) as db:
        rows = db.exec(select(Event).where(Event.campaign_id == campaign_id).order_by(Event.seq))
        return [row.kind for row in rows]


def test_gm_creates_a_discovered_location_with_a_position(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    created = _tool(
        app,
        campaign_id,
        "create_location",
        {
            "name": "Ashen Ford",
            "description": "A ford over black water.",
            "discovered": True,
            "x": 2.5,
            "y": -1,
        },
    )
    assert created == {"created": "location", "name": "Ashen Ford", "discovered": True}
    place = _state(app, campaign_id).named("Ashen Ford")
    assert place.discovered is True
    assert place.position == Position(x=2.5, y=-1.0)


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"x": 1}, "give both x and y, or neither"),
        ({"y": 1}, "give both x and y, or neither"),
        ({"x": True, "y": 0}, "argument 'x' must be number"),
        ({"x": 0, "y": "north"}, "argument 'y' must be number"),
    ],
)
def test_a_location_position_needs_two_real_numbers(
    client_factory: Callable[[], TestClient], app, arguments: dict, message: str
):
    _, campaign_id = _campaign_id(client_factory)
    refused = _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ashen Ford", "description": "A ford.", **arguments},
    )
    assert message in refused["error"]
    assert _state(app, campaign_id).named("Ashen Ford") is None


def test_the_world_service_checks_position_values_too(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    ctx = _ctx(app, campaign_id)
    with pytest.raises(InvalidInput, match="x must be a number of kilometers"):
        ctx.world.create_location("Ashen Ford", "A ford.", x=float("nan"), y=0)
    with pytest.raises(InvalidInput, match="give both x and y"):
        ctx.world.update_location("Mossbell Hollow", "Still here.", x=1)
    assert _state(app, campaign_id).named("Ashen Ford") is None


def test_update_keeps_a_position_unless_a_new_one_is_given(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ashen Ford", "description": "A ford.", "discovered": True, "x": 2, "y": 3},
    )
    _tool(app, campaign_id, "update_location", {"name": "Ashen Ford", "description": "Deeper now."})
    place = _state(app, campaign_id).named("Ashen Ford")
    assert place.position == Position(x=2.0, y=3.0)
    assert place.discovered is True

    _tool(
        app,
        campaign_id,
        "update_location",
        {"name": "Ashen Ford", "description": "Deeper now.", "x": -4, "y": 0},
    )
    place = _state(app, campaign_id).named("Ashen Ford")
    assert place.position == Position(x=-4.0, y=0.0)
    assert place.discovered is True


def test_two_places_cannot_share_a_point_unless_forced(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ashen Ford", "description": "A ford.", "discovered": True, "x": 2, "y": 3},
    )
    refused = _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ferry Post", "description": "A post.", "x": 2, "y": 3},
    )
    assert "Ashen Ford" in refused["error"] and "force=true" in refused["error"]
    assert _state(app, campaign_id).named("Ferry Post") is None

    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ferry Post", "description": "A post.", "x": 2, "y": 3, "force": True},
    )
    assert _state(app, campaign_id).named("Ferry Post").position == Position(x=2.0, y=3.0)


def test_moving_a_place_onto_another_needs_force(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ashen Ford", "description": "A ford.", "discovered": True, "x": 2, "y": 3},
    )
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ferry Post", "description": "A post.", "discovered": True, "x": 5, "y": 5},
    )
    refused = _tool(
        app,
        campaign_id,
        "update_location",
        {"name": "Ferry Post", "description": "A post.", "x": 2, "y": 3},
    )
    assert "Ashen Ford" in refused["error"]
    assert _state(app, campaign_id).named("Ferry Post").position == Position(x=5.0, y=5.0)

    _tool(
        app,
        campaign_id,
        "update_location",
        {"name": "Ferry Post", "description": "A post.", "x": 2, "y": 3, "force": True},
    )
    assert _state(app, campaign_id).named("Ferry Post").position == Position(x=2.0, y=3.0)

    # A place keeps its own point when it is updated without moving.
    _tool(
        app,
        campaign_id,
        "update_location",
        {"name": "Ashen Ford", "description": "Deeper now.", "x": 2, "y": 3},
    )
    assert _state(app, campaign_id).named("Ashen Ford").position == Position(x=2.0, y=3.0)


def test_force_must_be_true_or_false(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    refused = _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ashen Ford", "description": "A ford.", "x": 1, "y": 1, "force": "yes"},
    )
    assert "force" in refused["error"]
    assert _state(app, campaign_id).named("Ashen Ford") is None


def test_update_can_take_a_place_off_the_map_and_keep_it(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ashen Ford", "description": "A ford.", "discovered": True, "x": 2, "y": 3},
    )
    _tool(
        app,
        campaign_id,
        "update_location",
        {"name": "Ashen Ford", "description": "The ford is lost in fog.", "off_map": True},
    )
    place = _state(app, campaign_id).named("Ashen Ford")
    assert place.position is None
    assert place.discovered is True
    assert place.description == "The ford is lost in fog."

    view = world.map_view(_state(app, campaign_id))
    assert "Ashen Ford" in [p.name for p in view.places]
    assert "Ashen Ford" not in [m.name for m in view.markers]


def test_twin_lakes_map_places_every_module_location_it_has_a_position_for(
    client_factory: Callable[[], TestClient], app
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template="twin-lakes", rule_set="cairn2e")
    placed = {
        loc.name
        for loc in app.state.content.world_templates["twin-lakes"].locations
        if loc.x is not None
    }
    assert len(placed) == 12
    for name in placed:
        _tool(app, campaign_id, "discover_location", {"name": name})

    view = world.map_view(_state(app, campaign_id))
    assert {m.name for m in view.markers} == placed
    assert all(0 <= m.left <= 100 and 0 <= m.top <= 100 for m in view.markers)


def test_off_map_cannot_be_combined_with_a_position(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    ctx = _ctx(app, campaign_id)
    with pytest.raises(InvalidInput, match="off_map"):
        ctx.world.update_location("Mossbell Hollow", "Still here.", x=1, y=1, off_map=True)
    with pytest.raises(InvalidInput, match="off_map must be true or false"):
        ctx.world.update_location("Mossbell Hollow", "Still here.", off_map="yes")
    assert _state(app, campaign_id).named("Mossbell Hollow").position == Position(x=0.0, y=0.0)


def test_a_place_inside_another_has_no_map_point_of_its_own(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ashen Ford", "description": "A ford.", "discovered": True, "x": 2, "y": 3},
    )
    created = _tool(
        app,
        campaign_id,
        "create_location",
        {
            "name": "Ferry Post",
            "description": "A post by the ford.",
            "discovered": True,
            "parent": "Ashen Ford",
        },
    )
    assert created == {"created": "location", "name": "Ferry Post", "discovered": True}
    place = _state(app, campaign_id).named("Ferry Post")
    assert place.parent == "Ashen Ford"
    assert place.position is None

    view = world.map_view(_state(app, campaign_id))
    assert "Ferry Post" in [p.name for p in view.places]
    assert "Ferry Post" not in [m.name for m in view.markers]
    assert {m.name: m.label for m in view.markers} == {"Ashen Ford": "AF", "Mossbell Hollow": "MH"}

    refused = _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Mill", "description": "A mill.", "parent": "Ashen Ford", "x": 5, "y": 5},
    )
    assert "no map position" in refused["error"]
    assert _state(app, campaign_id).named("Mill") is None


def test_a_place_cannot_be_inside_a_place_that_is_inside_another(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "create_location", {"name": "Ashen Ford", "description": "A ford."})
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ferry Post", "description": "A post.", "parent": "Ashen Ford"},
    )
    refused = _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Dock", "description": "A dock.", "parent": "Ferry Post"},
    )
    assert "is itself inside Ashen Ford" in refused["error"]
    assert _state(app, campaign_id).named("Dock") is None

    missing = _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Dock", "description": "A dock.", "parent": "Nowhere"},
    )
    assert "there is no location named 'Nowhere'" in missing["error"]


def test_a_place_inside_another_cannot_be_moved_onto_the_map(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "create_location", {"name": "Ashen Ford", "description": "A ford."})
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ferry Post", "description": "A post.", "parent": "Ashen Ford"},
    )
    refused = _tool(
        app,
        campaign_id,
        "update_location",
        {"name": "Ferry Post", "description": "A post.", "x": 1, "y": 1},
    )
    assert "no map position" in refused["error"]
    assert _state(app, campaign_id).named("Ferry Post").position is None

    _tool(
        app,
        campaign_id,
        "update_location",
        {"name": "Ferry Post", "description": "The post has burned down."},
    )
    place = _state(app, campaign_id).named("Ferry Post")
    assert place.parent == "Ashen Ford"
    assert place.position is None
    assert place.description == "The post has burned down."


def test_a_place_with_places_inside_cannot_be_deleted_until_they_are_gone(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "create_location", {"name": "Ashen Ford", "description": "A ford."})
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ferry Post", "description": "A post.", "parent": "Ashen Ford"},
    )
    refused = _tool(app, campaign_id, "delete_location", {"name": "Ashen Ford"})
    assert "Ferry Post inside it" in refused["error"]
    assert _state(app, campaign_id).named("Ashen Ford") is not None

    _tool(app, campaign_id, "delete_location", {"name": "Ferry Post"})
    _tool(app, campaign_id, "delete_location", {"name": "Ashen Ford"})
    assert _state(app, campaign_id).named("Ashen Ford") is None


def test_discover_location_marks_a_place_once(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    assert _state(app, campaign_id).named("Spore Bridge").discovered is False
    first = _tool(app, campaign_id, "discover_location", {"name": "Spore Bridge"})
    second = _tool(app, campaign_id, "discover_location", {"name": "Spore Bridge"})
    assert first == second == {"discovered": "location", "name": "Spore Bridge"}
    assert _state(app, campaign_id).named("Spore Bridge").discovered is True
    assert _event_kinds(app, campaign_id).count("location.discovered") == 1


def test_moving_into_a_place_inside_another_discovers_the_place_that_holds_it(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ashen Ford", "description": "A ford.", "x": 2, "y": 3},
    )
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ferry Post", "description": "A post.", "parent": "Ashen Ford"},
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Ferry Post"},
    )
    state = _state(app, campaign_id)
    assert state.named("Ferry Post").discovered is True
    assert state.named("Ashen Ford").discovered is True


def test_discover_location_needs_a_known_location(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    refused = _tool(app, campaign_id, "discover_location", {"name": "Nowhere"})
    assert "Nowhere" in refused["error"]


def test_discover_location_sets_the_player_summary(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    text = "Pale mushrooms ring a dry well."
    result = _tool(app, campaign_id, "discover_location", {"name": "Spore Bridge", "summary": text})
    assert result == {"discovered": "location", "name": "Spore Bridge"}
    place = _state(app, campaign_id).named("Spore Bridge")
    assert place.discovered is True
    assert place.summary == text
    assert _event_kinds(app, campaign_id).count("location.discovered") == 1
    view = world.map_view(_state(app, campaign_id))
    assert [p.summary for p in view.places if p.name == "Spore Bridge"] == [text]


def test_a_summary_replaces_the_one_a_discovered_place_has(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "discover_location", {"name": "Spore Bridge"})
    assert _state(app, campaign_id).named("Spore Bridge").summary is None
    full = _state(app, campaign_id).named("Spore Bridge").description
    _tool(app, campaign_id, "discover_location", {"name": "Spore Bridge", "summary": "A bridge."})
    _tool(app, campaign_id, "discover_location", {"name": "Spore Bridge", "summary": "A bridge."})
    place = _state(app, campaign_id).named("Spore Bridge")
    assert place.summary == "A bridge."
    assert place.description == full
    assert _event_kinds(app, campaign_id).count("location.summarized") == 1


def test_a_discovery_without_a_summary_keeps_the_existing_one(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "discover_location", {"name": "Spore Bridge", "summary": "A bridge."})
    _tool(app, campaign_id, "discover_location", {"name": "Spore Bridge"})
    assert _state(app, campaign_id).named("Spore Bridge").summary == "A bridge."
    assert _event_kinds(app, campaign_id).count("location.summarized") == 0


def test_a_summary_is_bounded(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    refused = _tool(
        app, campaign_id, "discover_location", {"name": "Spore Bridge", "summary": "x" * 301}
    )
    assert "summary" in refused["error"]
    assert _state(app, campaign_id).named("Spore Bridge").discovered is False


def test_the_player_sees_the_summary_not_the_full_description(
    client_factory: Callable[[], TestClient], app
):
    client, campaign_id = _described_campaign(client_factory)
    full = _state(app, campaign_id).named("Glowcap Ring").description
    _tool(
        app,
        campaign_id,
        "discover_location",
        {"name": "Glowcap Ring", "summary": "Glowing caps line the ring."},
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    page = client.get(f"/campaigns/{campaign_id}").text
    location = page[page.index('id="panel-location"') : page.index('id="panel-character"')]
    assert "Glowing caps line the ring." in location
    assert full not in page


def test_the_player_discovers_what_they_visit_but_npcs_do_not(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Glowcap Ring"},
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Brin Gallowcap", "location": "Spore Bridge"},
    )
    state = _state(app, campaign_id)
    assert state.named("Glowcap Ring").discovered is True
    assert state.named("Spore Bridge").discovered is False


def test_discovered_places_are_marked_in_lookups(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "discover_location", {"name": "Glowcap Ring"})
    found = _tool(app, campaign_id, "search_location", {"query": "Glowcap"})
    entry = next(item for item in found["locations"] if item["name"] == "Glowcap Ring")
    assert entry["discovered"] is True
    assert entry["position_km"] == {"x": 0.6, "y": 0.5}


def test_map_shows_only_discovered_places_with_positions(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Mossbell Hollow"},
    )
    view = world.map_view(_state(app, campaign_id))
    assert [place.name for place in view.places] == ["Mossbell Hollow"]
    assert [(m.name, m.label, m.left, m.top, m.here) for m in view.markers] == [
        ("Mossbell Hollow", "MH", 50.0, 50.0, True)
    ]
    assert view.radius_km == 13.2

    _tool(app, campaign_id, "discover_location", {"name": "Glowcap Ring"})
    _tool(
        app,
        campaign_id,
        "create_location",
        {
            "name": "Far Shore",
            "description": "Across the ravine.",
            "discovered": True,
            "x": 3,
            "y": -2,
        },
    )
    view = world.map_view(_state(app, campaign_id))
    assert [place.name for place in view.places] == ["Far Shore", "Glowcap Ring", "Mossbell Hollow"]
    assert view.radius_km == 13.2
    markers = {m.name: m for m in view.markers}
    assert set(markers) == {"Far Shore", "Glowcap Ring", "Mossbell Hollow"}
    assert (markers["Far Shore"].left, markers["Far Shore"].top) == (61.36, 57.58)
    assert markers["Mossbell Hollow"].here is True
    assert markers["Glowcap Ring"].here is False
    assert "Spore Bridge" not in markers
    assert all(0 <= m.left <= 100 and 0 <= m.top <= 100 for m in view.markers)
    assert {m.name: m.label for m in view.markers} == {
        "Far Shore": "FS",
        "Glowcap Ring": "GR",
        "Mossbell Hollow": "MH",
    }


def test_map_on_a_fresh_campaign_has_a_default_scale(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    view = world.map_view(_state(app, campaign_id))
    assert view.radius_km == 1.1
    assert [m.name for m in view.markers] == ["Mossbell Hollow"]


def test_campaign_page_draws_discovered_places_on_the_map(
    client_factory: Callable[[], TestClient], app
):
    client, campaign_id = _described_campaign(client_factory)
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Mossbell Hollow"},
    )
    _tool(app, campaign_id, "discover_location", {"name": "Glowcap Ring"})
    page = client.get(f"/campaigns/{campaign_id}").text

    map_view = page[page.index('data-view="map"') : page.index('data-view="journal"')]
    assert (
        'class="map-marker here" style="left: 50.0%; top: 50.0%" role="img" aria-label="MH" '
        'data-tip="Mossbell Hollow (you)"><img src="/static/icons/terrain/town.svg" alt=""></span>'
        in map_view
    )
    assert "Glowcap Ring" in map_view
    assert "Spore Bridge" not in map_view

    panel = page[page.index('aria-label="Map"') : page.index('id="panel-')]
    assert "map-marker" in panel
    assert "Map placeholder" not in page


def test_map_centers_on_the_player_character_and_says_you_are_here(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "create_location",
        {
            "name": "Far Shore",
            "description": "Across the ravine.",
            "discovered": True,
            "x": 3,
            "y": -2,
        },
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Far Shore"},
    )
    view = world.map_view(_state(app, campaign_id))
    markers = {m.name: m for m in view.markers}
    assert (markers["Far Shore"].left, markers["Far Shore"].top) == (50.0, 50.0)
    assert markers["Far Shore"].here is True
    assert view.where["Far Shore"] == "You are here"
    assert view.origin == (40.91, 43.94)


def test_map_distance_labels_are_measured_from_the_player_character(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "create_location",
        {
            "name": "Far Shore",
            "description": "Across the ravine.",
            "discovered": True,
            "x": 3,
            "y": -2,
        },
    )
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ridge", "description": "A ridge.", "discovered": True, "x": 5, "y": 1},
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Far Shore"},
    )
    view = world.map_view(_state(app, campaign_id))
    assert view.where["Ridge"] == "2 km east, 3 km north"
    assert view.where["Mossbell Hollow"] == "3 km west, 2 km north"
    markers = {m.name: m for m in view.markers}
    assert (markers["Ridge"].left, markers["Ridge"].top) == (56.06, 40.91)


def test_map_centers_on_the_nearest_placed_ancestor(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    _give_the_map_ground(app, campaign_id)
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Ashen Ford", "description": "A ford.", "discovered": True, "x": 2, "y": 3},
    )
    _tool(
        app,
        campaign_id,
        "create_location",
        {
            "name": "Ferry Post",
            "description": "A post.",
            "discovered": True,
            "parent": "Ashen Ford",
        },
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Ferry Post"},
    )
    view = world.map_view(_state(app, campaign_id))
    assert view.where["Ferry Post"] == "Inside Ashen Ford"
    assert view.where["Ashen Ford"] == "You are here"
    assert view.where["Mossbell Hollow"] == "2 km west, 3 km south"
    assert "0 km east, 0 km north" not in view.where.values()
    markers = {m.name: m for m in view.markers}
    assert (markers["Ashen Ford"].left, markers["Ashen Ford"].top) == (50.0, 50.0)
    # The player is inside the building, so the place on the map stands for them.
    assert markers["Ashen Ford"].here is True
    assert markers["Mossbell Hollow"].here is False


def test_map_without_a_placed_ancestor_has_no_distance_labels(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Hollow Dell", "description": "A dell.", "discovered": True},
    )
    _tool(
        app,
        campaign_id,
        "move_actor",
        {"mode": "in_settlement", "name": "Ada", "location": "Hollow Dell"},
    )
    view = world.map_view(_state(app, campaign_id))
    assert view.origin == (50.0, 50.0)
    assert view.where == {"Hollow Dell": "You are here"}
    assert [m.name for m in view.markers] == ["Mossbell Hollow"]
    assert (view.markers[0].left, view.markers[0].top) == (50.0, 50.0)


def test_framed_map_has_a_recenter_button_inside_the_frame(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _described_campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    button = '<button type="button" class="secondary map-recenter">Recenter</button></div>'
    assert button in page
    frame = page.index('<div class="map-frame">')
    assert frame < page.index(button) < page.index('class="muted map-scale"', frame)
