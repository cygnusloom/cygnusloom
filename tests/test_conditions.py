from __future__ import annotations

from dataclasses import replace

import pytest

from cygnusforge.services.common import InvalidInput
from cygnusforge.services.conditions import end_condition
from cygnusforge.services.events import ConditionAdded, current, record
from tests.test_combat import PC, Rolls, _character, _fight
from tests.test_world import _db


def _add_condition(app, campaign_id: int, condition: str) -> None:
    with _db(app) as db:
        record(db, campaign_id, ConditionAdded(name=PC, condition=condition, cause="a test"))
        db.commit()


def test_a_stored_condition_ends_by_one_of_its_triggers(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition(app, campaign_id, "deafened")
    tools.end_condition(PC, "deafened", "aid")

    assert _character(app, campaign_id, PC).conditions == []


def test_walloped_ends_at_rest_and_its_on_end_raises_max_hp(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition(app, campaign_id, "walloped")
    before = _character(app, campaign_id, PC).stats["hp"]
    rest_tools = replace(tools, rng=Rolls(4))
    rest_tools.end_condition(PC, "walloped", "rest")

    character = _character(app, campaign_id, PC)
    assert character.conditions == []
    assert character.stats["hp"] == before + 4


def test_a_night_rest_ends_walloped_and_adds_a_die_to_max_hp(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition(app, campaign_id, "walloped")
    before = _character(app, campaign_id, PC).stats["hp"]
    rest_tools = replace(tools, rng=Rolls(4))

    result = rest_tools.rest("night", reason="sleep")

    assert PC not in result["held_back"]
    character = _character(app, campaign_id, PC)
    assert character.conditions == []
    assert character.stats["hp"] == before + 4


def test_a_week_of_rest_ends_hamstrung_by_extended_rest(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition(app, campaign_id, "hamstrung")

    result = tools.rest("week", reason="recover")

    assert PC not in result["held_back"]
    assert _character(app, campaign_id, PC).conditions == []


def test_a_condition_names_only_its_own_triggers(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition(app, campaign_id, "walloped")

    with pytest.raises(InvalidInput, match="ends by"):
        tools.end_condition(PC, "walloped", "aid")
    assert _character(app, campaign_id, PC).conditions == ["walloped"]


def test_an_unlisted_way_of_ending_a_condition_is_refused(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition(app, campaign_id, "walloped")

    with pytest.raises(InvalidInput, match="ends by rest, not by magic"):
        tools.end_condition(PC, "walloped", "magic")


def test_a_trigger_outside_the_rule_sets_vocabulary_is_refused(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition(app, campaign_id, "walloped")

    with _db(app) as db, pytest.raises(InvalidInput, match="not a condition-ending trigger"):
        end_condition(
            db,
            campaign_id,
            PC,
            "walloped",
            "banana",
            tools.combat,
            tools.conditions,
            tools.rng,
            plus=tools.plus,
            spec=tools.character,
            condition_ends=("rest", "extended_rest", "aid", "time"),
        )
    assert _character(app, campaign_id, PC).conditions == ["walloped"]


def test_a_gm_trigger_ends_a_condition_the_rule_set_leaves_to_the_gm(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition(app, campaign_id, "diseased")
    tools.end_condition(PC, "diseased", "gm")

    assert _character(app, campaign_id, PC).conditions == []


def test_ending_a_condition_the_character_does_not_have_is_refused(client_factory):
    _, _, tools = _fight(client_factory)

    with pytest.raises(InvalidInput, match="does not have the condition"):
        tools.end_condition(PC, "walloped", "rest")


def test_ending_an_unknown_condition_is_refused(client_factory):
    _, _, tools = _fight(client_factory)

    with pytest.raises(InvalidInput, match="there is no condition named"):
        tools.end_condition(PC, "not_a_condition", "rest")


def test_ending_a_condition_for_an_unknown_character_is_refused(client_factory):
    _, _, tools = _fight(client_factory)

    with pytest.raises(InvalidInput, match="there is no character named"):
        tools.end_condition("Nobody", "walloped", "rest")


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


def test_add_condition_gives_a_stored_condition_with_its_cause(client_factory):
    app, campaign_id, tools = _fight(client_factory)

    result = tools.add_condition(PC, "deafened", "a thunderclap")

    assert result == {"character": PC, "condition": "deafened"}
    assert _character(app, campaign_id, PC).conditions == ["deafened"]


def test_add_condition_leaves_a_held_condition_as_it_is(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.add_condition(PC, "deafened", "a thunderclap")

    tools.add_condition(PC, "deafened", "another thunderclap")

    assert _character(app, campaign_id, PC).conditions == ["deafened"]


def test_add_condition_refuses_a_derived_condition(client_factory):
    app, campaign_id, tools = _fight(client_factory)

    with pytest.raises(InvalidInput, match="cannot be added"):
        tools.add_condition(PC, "paralyzed", "a spell")
    assert _character(app, campaign_id, PC).conditions == []


def test_add_condition_refuses_an_unknown_condition_or_character(client_factory):
    _, _, tools = _fight(client_factory)

    with pytest.raises(InvalidInput, match="no condition named"):
        tools.add_condition(PC, "not_a_condition", "a test")
    with pytest.raises(InvalidInput, match="no character named"):
        tools.add_condition("Nobody", "deafened", "a test")
