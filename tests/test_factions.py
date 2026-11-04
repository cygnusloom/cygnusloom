"""Generic campaign factions, such as a magic school's colleges, and their cup points.

A faction's points live in a dynamic tally, so the ordinary award, repeat-award, and period-end
paths keep the score without a second store.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.content.library import TallyRules
from cygnusloom.db.models import Event
from cygnusloom.services import factions as factions_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.compendium import compendium_view
from cygnusloom.services.events import TallyReset, decode
from cygnusloom.services.views import world_state_text
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library
from tests.factories import (
    choose_campaign_options,
    create_campaign,
    create_character,
    creation_fields,
    open_campaign,
    pinned_stats,
    rules_for,
    signed_in_client,
    tools_for,
)
from tests.factories import (
    db as db_session,
)

RULE_SET = "academia-arcana"
WORLD = "unwritten-academy"
FACTIONS = (
    ("Ember College", "Fire"),
    ("Tide College", "Water"),
    ("Thorn College", "Thorns"),
)
# The creation page's stat picker can show a negative assigned value, which the shared
# `creation_fields` parser does not read, so pin a legal roll of its own.
STATS = {
    stat.id: 1 for stat in rules_for(RULE_SET).character.stats.values() if stat.derived is None
}


def _academy(
    client_factory: Callable[[], TestClient],
) -> tuple[FastAPI, int, WorldTools, TestClient]:
    """An Academy Arcana campaign with a character, built through the app's own routes."""
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template=WORLD, rule_set=RULE_SET)
    choose_campaign_options(client, campaign_id, RULE_SET)
    with pinned_stats(STATS):
        created = create_character(client, campaign_id, fields=creation_fields(client, campaign_id))
    assert created.status_code == 303, created.text
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    return client.app, campaign_id, tools, client


def _create_factions(tools: WorldTools) -> None:
    for name, trait in FACTIONS:
        tools.create_faction(name, f"{name} trains its students in its own art.", trait)


def _points(tools: WorldTools) -> dict[str, int]:
    return {faction["name"]: faction["points"] for faction in tools.list_factions()}


def test_factions_are_created_listed_updated_and_awarded(
    client_factory: Callable[[], TestClient],
):
    _app, _campaign_id, tools, _client = _academy(client_factory)

    for name, trait in FACTIONS:
        created = tools.create_faction(name, f"{name} trains its students.", trait)
        assert created["name"] == name
        assert created["trait"] == trait
        assert created["points"] == 0
        assert created["cup"] == "College Cup"

    listed = tools.list_factions()
    assert [faction["name"] for faction in listed] == sorted(
        (name for name, _ in FACTIONS), key=str.casefold
    )
    assert all(faction["points"] == 0 for faction in listed)
    assert {faction["tally"] for faction in listed} == {"college-cup"}

    edited = tools.update_faction(
        "Ember College",
        description="The oldest college, and the proudest.",
        trait="Ashes",
    )
    assert edited["trait"] == "Ashes"
    assert edited["description"] == "The oldest college, and the proudest."
    updated = next(
        faction for faction in tools.list_factions() if faction["name"] == "Ember College"
    )
    assert updated["trait"] == "Ashes"
    assert updated["description"] == "The oldest college, and the proudest."

    awarded = tools.adjust_faction_points(
        "Ember College", 3, reason="Won the debate", award="debate-1"
    )
    assert awarded["faction"] == "Ember College"
    assert awarded["before"] == 0
    assert awarded["value"] == 3
    assert _points(tools)["Ember College"] == 3

    with pytest.raises(InvalidInput, match="already paid the award"):
        tools.adjust_faction_points(
            "Ember College", 2, reason="The same debate again", award="debate-1"
        )
    assert _points(tools)["Ember College"] == 3


def test_ending_the_period_resets_the_cup_and_logs_the_final_totals(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools, _client = _academy(client_factory)
    _create_factions(tools)
    tools.adjust_faction_points("Ember College", 4, award="midterm")
    tools.adjust_faction_points("Tide College", 2, award="regatta")
    assert _points(tools) == {
        "Ember College": 4,
        "Thorn College": 0,
        "Tide College": 2,
    }

    ended = tools.end_period("school-year")
    assert ended["period"] == "school year"

    state = open_campaign(app, campaign_id)
    assert {faction.name for faction in state.factions.values()} == {name for name, _ in FACTIONS}
    assert state.tallies.get("college-cup", {}) == {}
    assert _points(tools) == {
        "Ember College": 0,
        "Thorn College": 0,
        "Tide College": 0,
    }

    with Session(app.state.engine) as session:
        rows = list(
            session.exec(
                select(Event).where(
                    Event.campaign_id == campaign_id,
                    Event.kind == TallyReset.kind,
                )
            ).all()
        )
    assert len(rows) == 1
    reset = decode(rows[0])
    assert isinstance(reset, TallyReset)
    assert reset.label == "College Cup"
    assert reset.final == {
        "ember-college": 4,
        "tide-college": 2,
        "thorn-college": 0,
    }
    assert reset.labels["ember-college"] == "Ember College"
    assert reset.labels["thorn-college"] == "Thorn College"


def test_a_non_dynamic_tally_refuses_create_faction(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, _tools, _client = _academy(client_factory)
    static = TallyRules(
        id="static-cup",
        label="Static Cup",
        entries={"one": "One"},
        dynamic=False,
    )

    with (
        db_session(app) as session,
        pytest.raises(InvalidInput, match="does not take entries from the campaign"),
    ):
        factions_service.create_faction(
            session,
            campaign_id,
            {"static-cup": static},
            "Static College",
            "A college that cannot join a dynamic cup.",
            "Still",
            tally="static-cup",
        )

    assert open_campaign(app, campaign_id).factions == {}


def test_the_world_state_lists_factions_with_traits_and_points(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools, _client = _academy(client_factory)
    tools.create_faction("Ember College", "The fire college.", "Fire")
    tools.create_faction("Tide College", "The water college.", "Water")
    tools.adjust_faction_points("Tide College", 5, award="tide-1")

    state = open_campaign(app, campaign_id)
    rules = content_library().rule_sets[RULE_SET]
    text = world_state_text(state, rules.campaign, tallies=rules.tallies)

    assert (
        "Factions: Ember College (Fire): 0 College Cup; "
        "Tide College (Water): 5 College Cup." in text
    )


def test_the_compendium_lists_factions_with_their_current_points(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools, _client = _academy(client_factory)
    tools.create_faction("Ember College", "The fire college.", "Fire")
    tools.adjust_faction_points("Ember College", 3, award="debate-1")

    state = open_campaign(app, campaign_id)
    compendium = compendium_view(content_library(), RULE_SET, WORLD, state)
    section = next(section for section in compendium.sections if section.id == "factions")
    entry = next(entry for entry in section.entries if entry.name == "Ember College")

    assert entry.source == "campaign"
    assert entry.group == "Factions"
    assert entry.text == "The fire college.\nTrait: Fire\n3 College Cup"


def test_the_campaign_page_shows_the_factions_tab_once_factions_exist(
    client_factory: Callable[[], TestClient],
):
    _app, campaign_id, tools, client = _academy(client_factory)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'id="tab-factions"' not in page

    tools.create_faction("Ember College", "The fire college.", "Fire")

    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'id="tab-factions"' in page
    assert 'id="panel-factions"' in page
    assert "Ember College" in page
    assert "0 College Cup" in page


def test_a_rule_set_without_a_faction_tally_creates_tally_less_factions(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    tools = tools_for(client.app, campaign_id, rule_set="worldswithoutnumber")

    for index in range(6):
        created = tools.create_faction(
            f"Border League {index}", "A league of border captains.", "Militarist"
        )
        assert created["tally"] == ""
        assert created["entry"] == ""
        assert created["points"] == 0
        assert created["cup"] == ""

    assert len(tools.list_factions()) == 6
