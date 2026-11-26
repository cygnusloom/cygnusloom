"""Threats in play: a single monster is a statblock, and a scale threat stays under the map.

Hordes does not roll for its monsters, so a threat's statblock carries a harm capacity, armor,
tags, and moves rather than hit points and attack dice. The player reads it in the same dialog
a character or monster opens, and only a threat big enough to be a scale of its own, such as a
horde, keeps a line under the map.
"""

from __future__ import annotations

from cygnusloom.services import threats as threats_service
from cygnusloom.services.events import load_state
from cygnusloom.services.views import map_view
from tests.factories import db, rules_for
from tests.test_hordes import _campaign

RULE_SET = "hordes"
BASIN = [[-10.0, -8.0], [10.0, -8.0], [10.0, 8.0], [-10.0, 8.0]]


def _two_places(tools) -> None:
    """Ground, the player's place, and a second place six kilometers east."""
    tools.create_region("The Basin", "farmland", BASIN, discovered=True)
    tools.create_location("North Gate", "A gate in the chain-link fence.", discovered=True, x=0, y=0)
    tools.create_location("The Mill", "A mill by the river.", discovered=True, x=6, y=0)
    tools.move_actor("Ada", "North Gate")


def test_a_single_monster_is_a_statblock_in_the_location(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_threat("walker", name="the walker", location="North Gate")

    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-statblock-name="the walker"' in page
    assert "zombie" in page
    # The separate threats panel is gone.
    assert 'id="panel-threats"' not in page
    assert 'id="tab-threats"' not in page

    block = client.get(f"/campaigns/{campaign_id}/statblock", params={"name": "the walker"})
    assert block.status_code == 200
    body = block.text
    assert "Zombie" in body
    assert "Harm" in body and "2 / 2" in body
    assert "Tags: shambling, relentless, infection-risk" in body
    assert "Moves: grab; bite; moan" in body
    assert "Attack 2" in body


def test_a_monster_at_another_place_is_not_in_the_roster(client_factory):
    _client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_threat("walker", name="the walker", location="The Mill")

    page = _client.get(f"/campaigns/{campaign_id}").text
    assert 'data-statblock-name="the walker"' not in page


def test_a_horde_is_a_line_under_the_map_and_not_a_roster_monster(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_threat("horde", name="the tide", magnitude=2, location="The Mill")

    page = client.get(f"/campaigns/{campaign_id}").text
    # One line under the map panel and one under the full Map view.
    assert page.count('class="threat-watch"') == 2
    assert "the tide" in page
    assert 'data-statblock-name="the tide"' not in page

    rules = rules_for(RULE_SET)
    with db(client.app) as session:
        state = load_state(session, campaign_id)
    view = map_view(state, rules.world_terrain, roads=rules.roads)
    assert [watch.name for watch in view.watches] == ["the tide"]
    assert "east" in view.watches[0].where


def test_a_horde_at_the_players_place_says_here(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_threat("horde", name="the tide", magnitude=3, location="North Gate")

    rules = rules_for(RULE_SET)
    with db(client.app) as session:
        state = load_state(session, campaign_id)
    view = map_view(state, rules.world_terrain, roads=rules.roads)
    assert [(watch.name, watch.where) for watch in view.watches] == [("the tide", "here")]
    assert 'data-statblock-name="the tide"' not in client.get(f"/campaigns/{campaign_id}").text


def test_the_nearest_scale_threat_comes_first(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_location("Far Ridge", "A ridge above the valley.", discovered=True, x=-9, y=0)
    tools.create_threat("horde", name="the far tide", magnitude=1, location="Far Ridge")
    tools.create_threat("horde", name="the near tide", magnitude=2, location="The Mill")

    rules = rules_for(RULE_SET)
    with db(client.app) as session:
        state = load_state(session, campaign_id)
    view = map_view(state, rules.world_terrain, roads=rules.roads)
    assert [watch.name for watch in view.watches] == ["the near tide", "the far tide"]


def test_a_hidden_threat_stays_out_of_the_player_view(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    rules = rules_for(RULE_SET)
    with db(client.app) as session:
        threats_service.create_threat(
            session,
            campaign_id,
            "walker",
            rules.threats,
            name="the lurker",
            location="North Gate",
            visible=False,
        )

    page = client.get(f"/campaigns/{campaign_id}").text
    # The hidden log line may still be in the markup for the GM, but nothing player-facing
    # offers the threat: no roster entry and no line under the map.
    assert 'data-statblock-name="the lurker"' not in page
    assert 'class="threat-watch"' not in page
    block = client.get(f"/campaigns/{campaign_id}/statblock", params={"name": "the lurker"})
    assert block.status_code == 404
