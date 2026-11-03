"""The neutral contract rule set, exercised through the public loader and services.

These tests are the guardrail for a rule set that reuses the generic engine: the fixture has no
Cairn or Worlds Without Number content, so a failure here means a generic capability stopped
working rather than one source book drifting. The setup goes through the app's own routes, via
``tests.factories``, and the behavior checks use the same services the tools call.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.content.character import Granted
from cygnusforge.content.library import DamageStep
from cygnusforge.llm.base import ToolRequest
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.conditions import add_condition, end_condition
from cygnusforge.services.counters import adjust_counter
from cygnusforge.services.events import Character
from cygnusforge.services.tools import Ask, RollAnswer, ToolRun, run_tool
from tests.conftest import content_library
from tests.factories import create_campaign, create_character, open_campaign, signed_in_client
from tests.rolls import social_roll

ROLLTEST = content_library().rule_sets["rolltest"]


def _pc(app: FastAPI, campaign_id: int) -> Character:
    pc = open_campaign(app, campaign_id).pc()
    assert pc is not None
    return pc


def test_the_contract_ruleset_loads_its_generic_content():
    """Save, damage, check, condition, counter, feature, and template data all load without a
    source-book module."""
    combat = ROLLTEST.combat
    assert combat is not None
    assert (combat.check_die, combat.check_sides) == ("1d20", 20)
    assert combat.damage == (DamageStep(kind="harm", stat="resolve"),)
    assert combat.initiative_stat == "resolve"
    assert combat.defeat == {"player_character": "out of action", "npc": "out of action"}
    assert combat.save.mode == "roll_under"
    assert (combat.save.auto_success, combat.save.auto_fail) == (1, 20)
    assert combat.save_succeeds(1, 0)
    assert combat.save_succeeds(10, 10)
    assert not combat.save_succeeds(11, 10)
    assert not combat.save_succeeds(20, 20)

    check = ROLLTEST.checks["task"]
    assert (check.label, check.dice) == ("Task Check", "1d10")
    assert (check.unskilled, check.default_difficulty) == (0, 6)
    assert (check.minimum_difficulty, check.maximum_difficulty) == (1, 10)

    condition = ROLLTEST.conditions["shaken"]
    assert (condition.label, condition.ends) == ("Shaken", ("rest", "scene"))
    assert condition.modifies == {"check": -2}
    assert condition.derived is None

    counter = ROLLTEST.counters["focus"]
    assert (counter.label, counter.minimum, counter.maximum, counter.initial) == (
        "Focus",
        0,
        3,
        1,
    )

    feature = ROLLTEST.character.features["steady-nerves"]
    assert feature.feature_type == "trait"
    assert feature.effects == ({"op": "counter", "id": "focus", "delta": 1},)

    template = ROLLTEST.character.templates["test-role"]
    assert (template.kind, template.name) == ("role", "Test Role")
    assert template.features == (Granted(feature="steady-nerves", quantity="1"),)
    assert ROLLTEST.optional_templates == ("role",)


def test_the_factories_create_a_campaign_and_a_templated_character(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, label="Contract")
    created = create_character(
        client,
        campaign_id,
        name="Ada",
        fields={"appearance": "A traveller in a patched cloak."},
        templates={"role": "test-role"},
    )
    assert created.status_code == 303, created.text

    pc = _pc(client.app, campaign_id)
    assert (pc.name, pc.pronouns) == ("Ada", "she/her")
    assert pc.stats["resolve"] == 12
    assert pc.templates == {"role": "test-role"}
    assert pc.features == {"steady-nerves": 1}
    assert pc.counters["focus"] == 2
    assert pc.fields["appearance"] == "A traveller in a patched cloak."


def test_a_condition_is_added_and_ends_only_by_a_configured_trigger(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client)
    assert create_character(client, campaign_id).status_code == 303
    app = client.app

    with Session(app.state.engine) as db:
        added = add_condition(
            db,
            campaign_id,
            "Ada",
            "shaken",
            "a test",
            ROLLTEST.conditions,
            ROLLTEST.combat,
        )
    assert added == {"character": "Ada", "condition": "shaken"}
    assert _pc(app, campaign_id).conditions == ["shaken"]

    with Session(app.state.engine) as db, pytest.raises(InvalidInput, match="ends by rest, scene"):
        end_condition(
            db,
            campaign_id,
            "Ada",
            "shaken",
            "aid",
            ROLLTEST.combat,
            ROLLTEST.conditions,
            random.Random(0),
            plus=ROLLTEST.plus,
            spec=ROLLTEST.character,
        )
    assert _pc(app, campaign_id).conditions == ["shaken"]

    with Session(app.state.engine) as db:
        ended = end_condition(
            db,
            campaign_id,
            "Ada",
            "shaken",
            "rest",
            ROLLTEST.combat,
            ROLLTEST.conditions,
            random.Random(0),
            plus=ROLLTEST.plus,
            spec=ROLLTEST.character,
        )
    assert ended == {"character": "Ada", "condition": "shaken", "ended_by": "rest"}
    assert _pc(app, campaign_id).conditions == []


def test_a_counter_change_is_clamped_to_its_bounds(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client)
    assert create_character(client, campaign_id).status_code == 303
    app = client.app

    def adjust(change: int) -> dict[str, object]:
        with Session(app.state.engine) as db:
            return adjust_counter(
                db,
                campaign_id,
                "Ada",
                "focus",
                change,
                counters=ROLLTEST.counters,
                conditions=ROLLTEST.conditions,
                combat=ROLLTEST.combat,
                rng=random.Random(0),
                plus=ROLLTEST.plus,
                spec=ROLLTEST.character,
            )

    assert adjust(1) == {"character": "Ada", "counter": "Focus", "value": 2}
    assert adjust(10) == {"character": "Ada", "counter": "Focus", "value": 3}
    assert adjust(-10) == {"character": "Ada", "counter": "Focus", "value": 0}
    assert _pc(app, campaign_id).counters["focus"] == 0
    assert adjust(-1) == {"character": "Ada", "counter": "Focus", "value": 0}


def test_the_social_roll_tool_asks_for_the_players_roll_and_resolves_it():
    """The fixture's JSON binding loads the Python function, and the tool follows the generic
    player-input path: an ``Ask`` first, then the roll plus both modifiers."""
    tool = ROLLTEST.tool_calls["social_roll"]
    request = ToolRequest(
        id="call_1",
        name="social_roll",
        arguments=json.dumps(
            {"prompt": "Roll to persuade.", "relationship": 2, "circumstance": -1}
        ),
    )

    ask = asyncio.run(run_tool(ROLLTEST.tool_calls, request))
    assert ask == Ask(
        text="Roll to persuade.",
        dice="1d20",
        details="Modifiers: +2 relationship, -1 circumstance.",
    )

    run = asyncio.run(run_tool(ROLLTEST.tool_calls, request, RollAnswer(14)))
    assert isinstance(run, ToolRun)
    assert json.loads(run.output) == {
        "die": 14,
        "relationship": 2,
        "circumstance": -1,
        "total": 15,
    }
    assert run.activity == (
        "You rolled 14 on the d20, with +2 relationship and -1 circumstance, for 15."
    )
    assert tool.function is social_roll
