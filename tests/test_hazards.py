"""Hazards: a trap, fall, or poison resolved with saves, harm, and conditions."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import ConditionAdded, Harmed
from tests.conftest import bundled_library
from tests.test_combat import Rolls
from tests.test_wwn_combat import FOE, OTHER_FOE, YARD, _events, _fight, _foe


def test_a_sprung_trap_rolls_an_npc_save_and_applies_damage_and_prone(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=8)

    result = replace(tools, rng=Rolls(1, 4)).resolve_hazard(
        "hidden pit",
        [FOE],
        save="evasion",
        damage="1d6",
        condition="prone",
    )

    [resolved] = result["results"]
    assert resolved["save"] == {"stat": "save_evasion", "roll": 1, "score": 15, "passed": False}
    assert resolved["harm"] == {
        "stat": "hp",
        "rolled": 4,
        "taken": 4,
        "current": 4,
        "defeated": False,
    }
    assert resolved["condition"] == "prone"

    [saved] = _events(app, campaign_id, "character.saved")
    assert (saved.stat, saved.roll, saved.passed) == ("save_evasion", 1, False)
    assert "hidden pit" in saved.cause and "hazard" in saved.cause
    [harmed] = _events(app, campaign_id, Harmed.kind)
    assert (harmed.taken, harmed.cause) == (4, saved.cause)
    [condition] = _events(app, campaign_id, ConditionAdded.kind)
    assert (condition.name, condition.condition) == (FOE, "prone")


def test_a_passed_save_avoids_the_hazard_or_takes_half(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=8)

    avoided = replace(tools, rng=Rolls(20)).resolve_hazard(
        "hidden pit", [FOE], save="evasion", damage="1d6", condition="prone"
    )
    [resolved] = avoided["results"]
    assert resolved["save"]["passed"] is True
    assert "harm" not in resolved and "condition" not in resolved
    assert _events(app, campaign_id, Harmed.kind) == []

    halved = replace(tools, rng=Rolls(20, 5)).resolve_hazard(
        "burning oil", [FOE], save="evasion", damage="1d6", half_on_save=True
    )
    [resolved] = halved["results"]
    assert resolved["harm"] == {
        "stat": "hp",
        "rolled": 5,
        "taken": 2,
        "current": 6,
        "defeated": False,
    }


def test_a_hazard_hits_every_target_it_names(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, hp=8)
    _foe(tools, name=OTHER_FOE, hp=8)

    result = replace(tools, rng=Rolls(1, 2, 1, 3, 4)).resolve_hazard(
        "deadfall", [FOE, OTHER_FOE], save="evasion", damage="1d6"
    )

    first, second = result["results"]
    assert first["save"]["passed"] is False and first["harm"]["taken"] == 2
    assert second["save"]["passed"] is False and second["harm"]["taken"] == 3


def test_a_hazard_that_defeats_the_last_foe_ends_the_encounter(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, hp=2)

    result = tools.resolve_hazard("falling rocks", [FOE], damage="6", condition="prone")

    assert result["encounter_ended"] is True
    assert result["results"][0]["status"] == "dead"
    assert tools.all_at_location(YARD)["npcs"][0]["defeated"] is True


def test_a_hazard_refuses_a_player_character_and_names_the_player_save(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools)

    with pytest.raises(InvalidInput, match="player-save tool"):
        tools.resolve_hazard("hidden pit", ["Wren"], save="evasion", damage="1d6")
    assert _events(app, campaign_id, Harmed.kind) == []


def test_a_bad_target_refuses_the_whole_hazard_before_anything_is_recorded(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools)

    with pytest.raises(InvalidInput, match="player-save tool"):
        tools.resolve_hazard("hidden pit", [FOE, "Wren"], save="evasion", damage="1d6")
    assert _events(app, campaign_id, "character.saved") == []
    assert _events(app, campaign_id, Harmed.kind) == []


def test_a_hazard_only_takes_the_rule_sets_outside_harm_stats(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools)

    with pytest.raises(InvalidInput, match="hazard damage can only take hp"):
        tools.resolve_hazard("hidden pit", [FOE], damage="1d6", stat="str")


def test_a_skipped_target_does_not_stop_the_hazard(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, hp=2)
    _foe(tools, name=OTHER_FOE, hp=8)
    tools.resolve_hazard("first rocks", [FOE], damage="6")

    result = tools.resolve_hazard("second rocks", [FOE, OTHER_FOE], damage="4")

    skipped, hit = result["results"]
    assert "skipped" in skipped
    assert hit["harm"]["taken"] == 4


def test_wwn_ships_a_traps_rule_and_prompts_for_sprung_hazards():
    rules = bundled_library().rule_sets["worldswithoutnumber"]
    assert "Evasion" in rules.rules["traps"]
    assert "Trapmaster" in rules.rules["traps"]

    own = {fragment.id: fragment for fragment in rules.prompt_fragments}
    assert "resolve_hazard" in own["traps"].text

    core = {fragment.id: fragment for fragment in bundled_library().core.prompt_fragments}
    assert "resolve_hazard" in core["hazards"].text
    assert "prepared earlier" in core["hazards"].text
    assert "preparations" in core["plans"].text


def test_the_hazard_tool_serves_only_the_combat_group():
    from cygnusloom.services.tools import TOOL_GROUPS

    assert "resolve_hazard" in TOOL_GROUPS["combat"]
