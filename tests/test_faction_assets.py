"""Worlds Without Number faction assets and faction turns.

The book's three asset tables are content, not prose: each asset resolves its cost, hit points,
Magic, Attack, Counter, and qualities from the rule set when a faction buys it. An asset acts
once per faction turn, and taking the next turn readies it again.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.services.common import InvalidInput
from cygnusloom.services.compendium import compendium_view
from cygnusloom.services.events import load_state
from cygnusloom.services.state import PC_KIND, CampaignState, Character
from cygnusloom.services.views import world_state_text
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library
from tests.factories import (
    create_campaign,
    open_campaign,
    signed_in_client,
    tools_for,
)
from tests.factories import (
    db as db_session,
)

RULE_SET = "worldswithoutnumber"
WORLD = "unwritten-world"
FACTION = "The Iron League"
TRAIT = "Militarist"


def _wwn(
    client_factory: Callable[[], TestClient],
) -> tuple[FastAPI, int, WorldTools, TestClient]:
    """A Worlds Without Number campaign. Factions need no character or opening turn."""
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template=WORLD, rule_set=RULE_SET)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    return client.app, campaign_id, tools, client


def _league(tools: WorldTools) -> dict[str, object]:
    return tools.create_faction(FACTION, "A league of border captains.", TRAIT)


def _with_pc(state: CampaignState) -> CampaignState:
    """The world-state text needs a player character; faction views do not."""
    state.characters["hero"] = Character(
        kind=PC_KIND,
        name="Hero",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
    )
    return state


def test_the_shipped_wwn_asset_tables_load_for_all_three_categories() -> None:
    assets = content_library().rule_sets[RULE_SET].faction_assets

    assert len(assets) == 81
    assert Counter(asset.category for asset in assets.values()) == {
        "cunning": 27,
        "force": 27,
        "wealth": 27,
    }

    infantry = assets["infantry"]
    assert infantry.label == "Infantry"
    assert (infantry.category, infantry.rating, infantry.cost, infantry.hp) == (
        "force",
        3,
        6,
        6,
    )
    assert infantry.magic == "None"
    assert infantry.attack == "F v. F/1d8 damage"
    assert infantry.counter == "1d6 damage"
    assert infantry.qualities == ()

    useful_idiots = assets["useful_idiots"]
    assert useful_idiots.label == "Useful Idiots"
    assert (useful_idiots.rating, useful_idiots.cost, useful_idiots.hp) == (1, 1, 2)
    assert useful_idiots.qualities == ("Subtle", "Special")

    golden = assets["golden_prosperity"]
    assert golden.category == "wealth"
    assert (golden.rating, golden.cost, golden.hp, golden.magic) == (8, 40, 30, "Medium")


def test_wwn_creates_and_lists_a_tally_less_faction(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, _client = _wwn(client_factory)

    created = _league(tools)
    assert created["name"] == FACTION
    assert created["trait"] == TRAIT
    assert created["tally"] == ""
    assert created["entry"] == ""
    assert created["cup"] == ""
    assert created["points"] == 0
    assert created["turn"] == 1
    assert created["assets"] == []

    listed = tools.list_factions()
    assert [faction["name"] for faction in listed] == [FACTION]
    assert listed[0]["points"] == 0

    state = _with_pc(open_campaign(app, campaign_id))
    rules = content_library().rule_sets[RULE_SET]
    text = world_state_text(state, rules.campaign, tallies=rules.tallies)
    assert f"{FACTION} ({TRAIT})" in text
    assert "0 ." not in text


def test_an_asset_resolves_its_stats_from_the_definition(
    client_factory: Callable[[], TestClient],
) -> None:
    _app, _campaign_id, tools, _client = _wwn(client_factory)
    _league(tools)

    asset = tools.add_faction_asset(FACTION, "infantry", "First Company", "Holds the pass")
    assert asset["faction"] == FACTION
    assert asset["turn"] == 1
    assert asset["id"] == "first-company"
    assert asset["kind"] == "infantry"
    assert asset["category"] == "force"
    assert asset["hp"] == asset["hp_max"] == 6
    assert asset["magic"] == "None"
    assert asset["attack"] == "F v. F/1d8 damage"
    assert asset["counter"] == "1d6 damage"
    assert asset["qualities"] == []
    assert asset["notes"] == ["Holds the pass"]
    assert asset["acted_turn"] == 0
    assert asset["ready"] is True

    listed = tools.list_factions()[0]
    assert [entry["name"] for entry in listed["assets"]] == ["First Company"]

    updated = tools.update_faction_asset(
        FACTION,
        "First Company",
        name="Old Guard",
        notes="Veterans of the pass.",
        qualities=["Action", "Special"],
    )
    assert updated["name"] == "Old Guard"
    assert updated["qualities"] == ["Action", "Special"]
    assert updated["notes"] == ["Holds the pass", "Veterans of the pass."]
    assert updated["hp"] == 6


def test_an_asset_acts_once_per_faction_turn_and_a_new_turn_readies_it(
    client_factory: Callable[[], TestClient],
) -> None:
    _app, _campaign_id, tools, _client = _wwn(client_factory)
    _league(tools)
    tools.add_faction_asset(FACTION, "infantry")

    acted = tools.faction_asset_acted(FACTION, "Infantry", "Attacked the bridge.")
    assert acted["acted_turn"] == 1
    assert acted["ready"] is False
    assert acted["notes"] == ["Attacked the bridge."]

    with pytest.raises(InvalidInput, match="already acted this faction turn"):
        tools.faction_asset_acted(FACTION, "Infantry")

    turn = tools.take_faction_turn(FACTION, "The bridge fell.")
    assert turn["turn"] == 2
    assert {asset["name"]: asset["ready"] for asset in turn["assets"]} == {"Infantry": True}

    again = tools.faction_asset_acted(FACTION, "Infantry")
    assert again["acted_turn"] == 2
    assert again["ready"] is False


def test_damage_and_repair_clamp_at_zero_and_the_asset_maximum(
    client_factory: Callable[[], TestClient],
) -> None:
    _app, _campaign_id, tools, _client = _wwn(client_factory)
    _league(tools)
    tools.add_faction_asset(FACTION, "infantry")

    broken = tools.damage_faction_asset(FACTION, "Infantry", 99)
    assert broken["hp"] == 0

    whole = tools.repair_faction_asset(FACTION, "Infantry", 99)
    assert whole["hp"] == whole["hp_max"] == 6

    damaged = tools.damage_faction_asset(FACTION, "Infantry", 2)
    assert damaged["hp"] == 4
    repaired = tools.repair_faction_asset(FACTION, "Infantry", 1)
    assert repaired["hp"] == 5

    with pytest.raises(InvalidInput, match="damage must be a whole number"):
        tools.damage_faction_asset(FACTION, "Infantry", 0)


def test_removing_a_faction_removes_its_assets(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, _client = _wwn(client_factory)
    _league(tools)
    tools.add_faction_asset(FACTION, "smugglers")
    tools.add_faction_asset(FACTION, "local_guard")

    removed = tools.remove_faction_asset(FACTION, "Smugglers")
    assert removed == {"faction": FACTION, "asset": "Smugglers"}
    remaining = tools.list_factions()[0]
    assert [asset["name"] for asset in remaining["assets"]] == ["Local Guard"]

    tools.remove_faction(FACTION)
    state = open_campaign(app, campaign_id)
    assert state.factions == {}


def test_asset_events_replay_from_the_log(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, _client = _wwn(client_factory)
    _league(tools)
    tools.add_faction_asset(FACTION, "infantry", notes="Garrisoned at the pass.")
    tools.faction_asset_acted(FACTION, "Infantry", "Marched north.")
    tools.damage_faction_asset(FACTION, "Infantry", 2)
    tools.update_faction_asset(FACTION, "Infantry", name="The Old Guard", qualities=["Special"])
    tools.take_faction_turn(FACTION, "Winter quarters.")
    tools.repair_faction_asset(FACTION, "The Old Guard", 1)

    with db_session(app) as session:
        state = load_state(session, campaign_id)

    faction = state.factions["the-iron-league"]
    assert faction.turn == 2
    asset = faction.assets["infantry"]
    assert asset.name == "The Old Guard"
    assert asset.kind == "infantry"
    assert asset.hp == 5
    assert asset.hp_max == 6
    assert asset.qualities == ["Special"]
    assert asset.notes == ["Garrisoned at the pass.", "Marched north."]
    assert asset.acted_turn == 1
    assert faction.asset_ready(asset) is True


def test_asset_state_reaches_the_world_text_and_compendium(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, _client = _wwn(client_factory)
    _league(tools)
    tools.add_faction_asset(FACTION, "infantry")
    tools.faction_asset_acted(FACTION, "Infantry")
    tools.take_faction_turn(FACTION)

    state = _with_pc(open_campaign(app, campaign_id))
    rules = content_library().rule_sets[RULE_SET]
    text = world_state_text(state, rules.campaign, tallies=rules.tallies)
    assert f"{FACTION} ({TRAIT}) [Turn 2: Infantry 6/6 HP ready]" in text

    compendium = compendium_view(content_library(), RULE_SET, WORLD, state)
    section = next(section for section in compendium.sections if section.id == "factions")
    entry = next(entry for entry in section.entries if entry.name == FACTION)
    assert entry.text == (
        "A league of border captains.\n"
        "Trait: Militarist\n"
        "Turn 2\n"
        "Infantry (force infantry): 6/6 HP, ready"
    )


def test_an_unknown_asset_kind_is_refused(
    client_factory: Callable[[], TestClient],
) -> None:
    _app, _campaign_id, tools, _client = _wwn(client_factory)
    _league(tools)

    with pytest.raises(InvalidInput, match="no faction asset kind"):
        tools.add_faction_asset(FACTION, "war elephants")

    with pytest.raises(InvalidInput, match="no faction named"):
        tools.add_faction_asset("The Drowned Court", "infantry")


def test_the_campaign_page_renders_assets_and_omits_an_empty_cup(
    client_factory: Callable[[], TestClient],
) -> None:
    from tests.test_wwn_combat import _wwn_campaign as _wwn_campaign_with_pc

    client, campaign_id, _tools = _wwn_campaign_with_pc(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    _league(tools)
    tools.add_faction_asset(FACTION, "infantry")
    tools.faction_asset_acted(FACTION, "Infantry")
    tools.take_faction_turn(FACTION)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'id="panel-factions"' in page
    assert FACTION in page
    assert "Turn 2" in page
    assert "Infantry — 6/6 HP, force, ready" in page
    assert "0 points" not in page


def test_repeat_assets_of_one_kind_get_unique_ids(
    client_factory: Callable[[], TestClient],
) -> None:
    _app, _campaign_id, tools, _client = _wwn(client_factory)
    _league(tools)

    first = tools.add_faction_asset(FACTION, "infantry")
    second = tools.add_faction_asset(FACTION, "infantry")
    assert (first["id"], second["id"]) == ("infantry", "infantry-2")
    assert first["name"] == second["name"] == "Infantry"

    assets = tools.list_factions()[0]["assets"]
    assert [asset["id"] for asset in assets] == ["infantry", "infantry-2"]
