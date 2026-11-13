from __future__ import annotations

from dataclasses import replace

import pytest

from cygnusloom.services import world
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import current
from tests.factories import (
    DEFAULT_PC,
    Rolls,
    open_campaign,
    scene,
)
from tests.factories import (
    character_named as _character,
)
from tests.test_combat import _fight

ADA = DEFAULT_PC
# The Cairn campaign that `_fight` sets up names its player character this.
PC = "Mara"


def test_a_night_rest_ends_walloped_and_adds_a_die_to_max_hp(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.add_condition(PC, "walloped", "a test")
    before = _character(app, campaign_id, PC).stats["hp"]
    rest_tools = replace(tools, rng=Rolls(4))

    result = rest_tools.rest("night", reason="sleep")

    assert PC not in result["held_back"]
    character = _character(app, campaign_id, PC)
    assert character.conditions == []
    assert character.stats["hp"] == before + 4


def test_a_week_of_rest_ends_hamstrung_by_extended_rest(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.add_condition(PC, "hamstrung", "a test")

    result = tools.rest("week", reason="recover")

    assert PC not in result["held_back"]
    assert _character(app, campaign_id, PC).conditions == []


def test_a_condition_names_only_its_own_triggers(client_factory):
    app, campaign_id, tools = scene(client_factory)
    tools.add_condition(ADA, "shaken", "a test")

    with pytest.raises(InvalidInput, match="not a condition-ending trigger"):
        tools.end_condition(ADA, "shaken", "magic")
    assert _character(app, campaign_id, ADA).conditions == ["shaken"]


def test_a_gm_trigger_ends_a_condition_the_rule_set_does_not_name(client_factory):
    app, campaign_id, tools = scene(client_factory)
    tools.add_condition(ADA, "shaken", "a test")

    tools.end_condition(ADA, "shaken", "gm")

    assert _character(app, campaign_id, ADA).conditions == []


def test_ending_a_condition_the_character_does_not_have_is_refused(client_factory):
    _, _, tools = scene(client_factory)

    with pytest.raises(InvalidInput, match="does not have the condition"):
        tools.end_condition(ADA, "shaken", "rest")


def test_ending_an_unknown_condition_is_refused(client_factory):
    _, _, tools = scene(client_factory)

    with pytest.raises(InvalidInput, match="there is no condition named"):
        tools.end_condition(ADA, "not_a_condition", "rest")


def test_ending_a_condition_for_an_unknown_character_is_refused(client_factory):
    _, _, tools = scene(client_factory)

    with pytest.raises(InvalidInput, match="there is no character named"):
        tools.end_condition("Nobody", "shaken", "rest")


def test_a_derived_condition_cannot_be_ended(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    dex = current(_character(app, campaign_id, PC), "dex")
    tools.harm_character(PC, "dex", dex, "a fall")

    with pytest.raises(InvalidInput, match="cannot be ended"):
        tools.end_condition(PC, "paralyzed", "rest")


def test_dex_harmed_to_zero_leaves_the_character_paralyzed(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    dex = current(_character(app, campaign_id, PC), "dex")
    tools.harm_character(PC, "dex", dex, "a fall")

    assert _character(app, campaign_id, PC).conditions == []
    assert "Conditions: Paralyzed" in tools.get_statblock(PC)["statblock"]


def test_wil_harmed_to_zero_leaves_the_character_delirious(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    wil = current(_character(app, campaign_id, PC), "wil")
    tools.harm_character(PC, "wil", wil, "a fright")

    assert "Conditions: Delirious" in tools.get_statblock(PC)["statblock"]


def test_outside_harm_is_capped_at_the_stat_that_remains(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    dex = current(_character(app, campaign_id, PC), "dex")
    result = tools.harm_character(PC, "dex", dex + 5, "a fall")

    assert result["taken"] == dex
    assert result["current"] == 0
    assert result["defeated"] is False


def test_harm_taking_strength_to_zero_defeats_the_character(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    strength = current(_character(app, campaign_id, PC), "str")
    result = tools.harm_character(PC, "str", strength, "a fall")

    assert result["defeated"] is True
    assert _character(app, campaign_id, PC).defeated


def test_harm_taking_dexterity_to_zero_does_not_defeat_the_character(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    dex = current(_character(app, campaign_id, PC), "dex")
    result = tools.harm_character(PC, "dex", dex, "a fall")

    assert result["defeated"] is False
    assert not _character(app, campaign_id, PC).defeated


def test_hit_points_cannot_take_outside_harm(client_factory):
    _, _, tools = _fight(client_factory)

    with pytest.raises(InvalidInput, match="can only take"):
        tools.harm_character(PC, "hp", 1, "a fall")


def test_outside_harm_must_be_at_least_one(client_factory):
    _, _, tools = _fight(client_factory)

    with pytest.raises(InvalidInput, match="amount must be a whole number"):
        tools.harm_character(PC, "dex", 0, "a fall")


def test_a_stat_already_at_zero_cannot_lose_more(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    dex = current(_character(app, campaign_id, PC), "dex")
    tools.harm_character(PC, "dex", dex, "a fall")

    with pytest.raises(InvalidInput, match="has no DEX left to lose"):
        tools.harm_character(PC, "dex", 1, "a fall")


def test_outside_harm_to_a_defeated_character_is_refused(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    strength = current(_character(app, campaign_id, PC), "str")
    tools.harm_character(PC, "str", strength, "a fall")

    with pytest.raises(InvalidInput, match=f"{PC} is in critical damage"):
        tools.harm_character(PC, "dex", 1, "a fall")


def test_add_condition_leaves_a_held_condition_as_it_is(client_factory):
    app, campaign_id, tools = scene(client_factory)
    tools.add_condition(ADA, "shaken", "a thunderclap")

    tools.add_condition(ADA, "shaken", "another thunderclap")

    assert _character(app, campaign_id, ADA).conditions == ["shaken"]


def test_the_player_sheet_shows_active_conditions(client_factory):
    app, campaign_id, tools = scene(client_factory)
    tools.add_condition(ADA, "shaken", "a thunderclap")

    view = world.scene_view(
        open_campaign(app, campaign_id), tools.character, conditions=tools.conditions
    )

    assert "Conditions: Shaken" in view.sheet


def test_the_player_sheet_has_no_conditions_line_without_conditions_rules(client_factory):
    app, campaign_id, tools = scene(client_factory)
    tools.add_condition(ADA, "shaken", "a thunderclap")

    view = world.scene_view(open_campaign(app, campaign_id), tools.character)

    assert not any(line.startswith("Conditions:") for line in view.sheet)


def test_add_condition_refuses_a_derived_condition(client_factory):
    app, campaign_id, tools = _fight(client_factory)

    with pytest.raises(InvalidInput, match="cannot be added"):
        tools.add_condition(PC, "paralyzed", "a spell")
    assert _character(app, campaign_id, PC).conditions == []


def test_add_condition_refuses_an_unknown_condition_or_character(client_factory):
    _, _, tools = scene(client_factory)

    with pytest.raises(InvalidInput, match="no condition named"):
        tools.add_condition(ADA, "not_a_condition", "a test")
    with pytest.raises(InvalidInput, match="no character named"):
        tools.add_condition("Nobody", "shaken", "a test")
