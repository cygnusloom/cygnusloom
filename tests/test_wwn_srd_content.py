"""The blank Worlds Without Number setting and the tools that write it down.

The rule set carries no setting of its own; the GM invents one during the opening and records
each fact on the world state. These tests cover that recording loop.
"""

from __future__ import annotations

import json

import pytest

from cygnusloom.content.library import ContentLibrary
from cygnusloom.services import views
from cygnusloom.services.common import InvalidInput
from tests import factories
from tests.conftest import content_library
from tests.test_wwn_advancement import NAME, _campaign, _world_tools


def _wwn() -> ContentLibrary:
    return content_library()


def test_recording_a_setting_fact_reaches_the_world_state(
    client_factory,
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    first = tools.record_setting("The starting region", "A river valley of oath-bound clans.")
    assert first["recorded"] == "The starting region"
    assert first["facts"] == 1

    second = tools.record_setting("the starting region", "A highland of herders and old stone.")
    assert second["replaced"] == "A river valley of oath-bound clans."
    assert second["facts"] == 1

    state = factories.open_campaign(app, campaign_id)
    assert state.settings == {"The starting region": "A highland of herders and old stone."}

    rules = _wwn().rule_sets["worldswithoutnumber"]
    text = views.world_state_text(state, rules.campaign)
    assert "A highland of herders and old stone." in text


def test_recording_several_setting_facts_takes_one_call(client_factory) -> None:
    """The opening writes six or more facts; one round per fact is the difference between a
    three-minute opening and a long one."""
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    result = tools.record_settings(
        [
            {"topic": "The world", "detail": "A river kingdom."},
            {"topic": "Magic", "detail": "Learned from old books."},
            {"topic": "The world", "detail": "A river kingdom of oath-bound clans."},
        ]
    )

    assert result["recorded"] == ["The world", "Magic", "The world"]
    assert result["facts"] == 2
    state = factories.open_campaign(app, campaign_id)
    assert state.settings == {
        "The world": "A river kingdom of oath-bound clans.",
        "Magic": "Learned from old books.",
    }
    with pytest.raises(InvalidInput):
        tools.record_settings([])


def test_setting_facts_do_not_leak_between_campaigns(client_factory) -> None:
    client = factories.signed_in_client(client_factory)
    first_id = factories.create_campaign(
        client, label="First", world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    second_id = factories.create_campaign(
        client, label="Second", world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    tools = factories.tools_for(client.app, first_id, rule_set="worldswithoutnumber")
    tools.record_setting("Magic", "Sorcery is learned from old books and costs the reader sleep.")

    first = factories.open_campaign(client.app, first_id)
    second = factories.open_campaign(client.app, second_id)
    assert "Magic" in first.settings
    assert second.settings == {}


def test_a_gm_turn_invents_the_setting_with_the_tools(client_factory) -> None:
    """The whole loop: the GM rolls the idea tables, writes the world down, and then sees it."""
    app, campaign_id = _campaign(client_factory)
    # A second client for the same signed-in player, so the campaign the helper built is reachable.
    client = factories.signed_in_client(client_factory)
    # The opening turn is pending until the player answers it, as in any new campaign.
    factories.answer(client, campaign_id, "continue")
    tools = _world_tools(app, campaign_id)
    tools.create_location(name="Crossroads", description="A road junction with an old well.")
    tools.move_actor(NAME, "Crossroads")

    script = "__FAKE__:" + json.dumps(
        {
            "turns": [
                {
                    "rounds": [
                        [
                            {"name": "roll_creative", "arguments": {}},
                            {
                                "name": "record_setting",
                                "arguments": {
                                    "topic": "The world",
                                    "detail": "A land of river kingdoms built on older stone.",
                                },
                            },
                        ]
                    ],
                    "text": "The world takes shape around the crossroads.",
                }
            ]
        }
    )
    response = factories.send_message(client, campaign_id, script)
    assert response.status_code in (200, 303), response.text

    state = factories.open_campaign(client.app, campaign_id)
    assert state.settings["The world"] == "A land of river kingdoms built on older stone."
    rules = _wwn().rule_sets["worldswithoutnumber"]
    text = views.world_state_text(state, rules.campaign)
    assert "A land of river kingdoms built on older stone." in text
