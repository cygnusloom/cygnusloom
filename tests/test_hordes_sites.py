"""Hordes sites: generated place types, zone-by-zone scavenging, and city navigation."""

from __future__ import annotations

import asyncio
import json

import pytest

from cygnusloom.llm.base import ToolRequest
from cygnusloom.rules.context import ToolContext
from cygnusloom.rules.hordes import navigate_city
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import load_state
from cygnusloom.services.tools import ToolRun, run_tool
from cygnusloom.web.character_form import creation_view
from tests.conftest import bundled_library
from tests.factories import character_named, db, rules_for
from tests.test_hordes import _campaign

RULE_SET = "hordes"


def test_site_types_are_loaded_with_valid_zones_loot_and_threats():
    rules = rules_for(RULE_SET)
    assert len(rules.site_types) == 20
    for site_type in rules.site_types.values():
        assert site_type.description
        assert site_type.zones
        assert site_type.find in {"common", "uncommon", "hidden", "secret"}
        for threat in site_type.threats:
            assert threat in rules.threats
        for zone in site_type.zones:
            assert zone.loot
            for entry in zone.loot:
                assert entry["item"] in rules.character.items
        for entry in site_type.loot:
            assert entry["item"] in rules.character.items

    lab = rules.site_types["cybernetics-lab"]
    assert lab.find == "hidden"
    assert "zombie-cyborg" in lab.threats
    assert "operating-room" in {zone.id for zone in lab.zones}


def test_listing_and_reading_site_types_filters_hard_to_find_places(client_factory):
    _client, _campaign_id, tools = _campaign(client_factory)

    hidden = tools.list_site_types(find="hidden")
    names = {card["name"] for card in hidden["site_types"]}
    assert "Cybernetics Lab" in names
    assert "Convenience Store" not in names

    lab = tools.lookup_site_type("cybernetics-lab")
    assert lab["site_type"]["find"] == "hidden"
    assert any(zone["id"] == "operating-room" for zone in lab["zones"])
    assert any(threat["id"] == "zombie-cyborg" for threat in lab["threats"])
    assert lab["secrets"]


def _make_room(tools) -> None:
    """Drop the starting kit's heavy gear so a search test has pack room."""
    for item in ("rifle", "ballistic vest", "helmet", "rations", "shotgun"):
        try:
            tools.drop_item("Ada", item)
        except InvalidInput:
            pass


def test_searching_a_site_carries_granular_loot_and_tracks_zones(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _make_room(tools)
    tools.create_location(
        "Corner Store",
        "A corner shop with one working sign and a stockroom nobody has opened.",
        site_type="convenience-store",
    )
    tools.move_actor("Ada", "Corner Store")

    first = tools.search_site("Corner Store", "Ada", zone="shop-floor", total=10)
    assert first["band"] == "10+"
    assert first["danger"] == 1
    assert len(first["found"]) == 2
    assert all(entry["taken"] for entry in first["found"])
    assert first["remaining_zones"]

    carried = {entry.name for entry in character_named(client.app, campaign_id, "Ada").items}
    for entry in first["found"]:
        assert entry["name"] in carried

    with pytest.raises(InvalidInput, match="already been searched"):
        tools.search_site("Corner Store", "Ada", zone="shop-floor", total=10)

    second = tools.search_site("Corner Store", "Ada", total=7)
    assert second["band"] == "7-9"
    assert len(second["found"]) == 1
    assert second["zone"] != "shop-floor"

    with db(client.app) as session:
        place = load_state(session, campaign_id).places["corner store"]
    assert place.site_type == "convenience-store"
    assert place.site_danger == 2
    assert len(place.searched_zones) == 2


def test_a_poor_condition_cost_marks_the_found_loot_worn(client_factory):
    _client, _campaign_id, tools = _campaign(client_factory)
    tools.create_location(
        "Worn Store",
        "A shop whose stock has seen better days.",
        site_type="convenience-store",
    )
    tools.move_actor("Ada", "Worn Store")

    result = tools.search_site(
        "Worn Store", "Ada", zone="shop-floor", total=8, condition="worn"
    )
    assert result["found"]
    assert all(entry["condition"] == "worn" for entry in result["found"])


def test_site_danger_raises_a_complication_and_the_visible_clock(client_factory):
    _client, _campaign_id, tools = _campaign(client_factory)
    tools.create_location(
        "Quiet Pharmacy",
        "A pharmacy with the shutter half down.",
        site_type="pharmacy",
    )
    tools.move_actor("Ada", "Quiet Pharmacy")

    tools.search_site("Quiet Pharmacy", "Ada", zone="counter", total=10)
    tools.search_site("Quiet Pharmacy", "Ada", zone="dispensary", total=8)
    third = tools.search_site("Quiet Pharmacy", "Ada", zone="stockroom", total=10)

    assert third["danger"] == 3
    assert third["complication"]
    clocks = {clock["id"]: clock for clock in tools.list_clocks()["clocks"]}
    assert clocks["sitedanger"]["filled"] == 3
    assert clocks["sitedanger"]["visible"] is True


def test_a_site_search_event_has_log_text(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    tools.create_location(
        "Log House",
        "A house with one searchable room.",
        site_type="house",
    )
    tools.move_actor("Ada", "Log House")
    tools.search_site("Log House", "Ada", zone="entry-hall", total=10)

    response = client.get(f"/campaigns/{campaign_id}")
    assert response.status_code == 200
    assert "Ada searches the entry-hall at Log House." in response.text


def test_city_navigation_moves_the_party_raises_alert_and_rolls_a_street_event(
    client_factory,
):
    client, campaign_id, tools = _campaign(client_factory)
    tools.create_location("Ada's Block", "A street of houses with one working lamp.")
    tools.create_location(
        "Corner Store",
        "A corner shop with one working sign.",
        site_type="convenience-store",
    )
    tools.move_actor("Ada", "Ada's Block")
    rules = rules_for(RULE_SET)

    async def speak(_npc: str, _profile: str, _intent: str, _replace: bool) -> None:
        return None

    ctx = ToolContext(
        world=tools,
        speak=speak,
        ooc=lambda _narrate: None,
        rules=rules.rules,
        lookup_tools={},
    )
    result = navigate_city("Ada", "Corner Store", "main-street", ctx, "fetch water")

    assert result["route"] == "main-street"
    assert result["moved"]["location"] == "Corner Store"
    assert result["street"]["table"] == "street-event"
    clocks = {clock["id"]: clock for clock in tools.list_clocks()["clocks"]}
    assert clocks["cityalert"]["filled"] == 2
    assert character_named(client.app, campaign_id, "Ada").location == "Corner Store"


def test_a_full_pack_leaves_site_loot_behind(client_factory):
    _client, _campaign_id, tools = _campaign(client_factory)
    tools.create_location(
        "Heavy House",
        "A house full of heavy things.",
        site_type="house",
    )
    tools.move_actor("Ada", "Heavy House")
    for _ in range(30):
        try:
            tools.give_item("Ada", "parts")
        except InvalidInput:
            break

    result = tools.search_site("Heavy House", "Ada", zone="garage", total=10)
    assert result["found"]
    assert result["left_behind"]
    assert any(entry["left_because"] for entry in result["left_behind"])


def test_every_hordes_flavor_field_offers_choices_and_a_write_in():
    spec = rules_for(RULE_SET).character
    rolled = {
        stat.id: (stat.values[0] if stat.values else stat.low)
        for stat in spec.stats.values()
        if stat.derived is None
    }
    view = creation_view(spec, rolled)
    choice_ids = {field["id"] for field in view["choice_fields"]}
    assert {
        "drive",
        "burden",
        "scar",
        "look",
        "background",
        "shelter_duty",
        "personal_item",
    } <= choice_ids
    assert all(field["flavor"] for field in view["choice_fields"])
    assert all(field["flavor"] for field in view["text_fields"])


def test_the_bundled_content_still_credits_the_apocalypse_tables():
    library = bundled_library()
    hordes = library.rule_sets[RULE_SET]
    site_tables = [
        table for table in hordes.dice_tables.values() if table.creative and table.role == "site"
    ]
    assert {table.id for table in site_tables} == {
        "site-detail",
        "site-secret",
        "site-occupant",
        "site-sign",
    }
    assert all(table.genres == ("apocalypse",) for table in site_tables)


def test_the_create_site_tool_is_bound_and_stores_the_site_type(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    rules = rules_for(RULE_SET)

    async def speak(_npc: str, _profile: str, _intent: str, _replace: bool) -> None:
        return None

    ctx = ToolContext(
        world=tools,
        speak=speak,
        ooc=lambda _narrate: None,
        rules=rules.rules,
        lookup_tools={},
    )
    tool = rules.tool_calls["create_site"]
    request = ToolRequest(
        id="call_1",
        name=tool.name,
        arguments=json.dumps(
            {
                "name": "The Corner Store",
                "description": "A shop with one light and a smell of old sugar.",
                "site_type": "convenience-store",
            }
        ),
    )
    run = asyncio.run(run_tool({tool.id: tool}, request, None, ctx))
    assert isinstance(run, ToolRun)
    payload = json.loads(run.output)
    assert payload["site_type"] == "convenience-store"
    with db(client.app) as session:
        place = load_state(session, campaign_id).places["the corner store"]
    assert place.site_type == "convenience-store"
