from __future__ import annotations

from dataclasses import replace

import pytest

from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.content.library import _load_rests
from cygnusloom.services.events import (
    PC_KIND,
    CampaignState,
    Character,
    ConditionAdded,
    Recovered,
    name_key,
    record,
)
from cygnusloom.services.recovery import recover
from tests.test_combat import CAIRN, PC, _character, _fight
from tests.test_world import _db


def _wounded(harm: dict[str, int]) -> Character:
    return Character(
        kind=PC_KIND,
        name="Ada",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
        stats={"hp": 10, "str": 8},
        harm=dict(harm),
    )


def test_recover_clamps_an_over_heal_to_the_harm_present():
    character = _wounded({"hp": 3})

    assert recover(character, "hp", 10) == 3
    assert "hp" not in character.harm


def test_recover_restores_nothing_for_zero_or_less():
    character = _wounded({"hp": 2})

    assert recover(character, "hp", 0) == 0
    assert recover(character, "hp", -5) == 0
    assert character.harm == {"hp": 2}


def test_recover_leaves_other_stats_alone():
    character = _wounded({"hp": 2, "str": 1})

    assert recover(character, "str", 1) == 1
    assert character.harm == {"hp": 2}


def test_recover_an_unharmed_stat_restores_nothing():
    character = _wounded({})

    assert recover(character, "hp", 5) == 0
    assert character.harm == {}


def test_the_recovered_event_applies_the_amount_it_records_and_renders_it():
    event = Recovered(name="Ada", stat="hp", amount=2, restores=["hp", "str"])
    state = CampaignState()
    character = _wounded({"hp": 5, "str": 1})
    state.characters[name_key("Ada")] = character

    event.apply(state, 1)

    assert character.harm == {"hp": 3, "str": 1}
    assert event.render() == "Ada recovers 2 HP from the rest."
    assert (
        Recovered(name="Ada", stat="hp", amount=2, cause="a healer").render()
        == "Ada recovers 2 HP (a healer)."
    )


def test_a_rest_definition_loads_its_condition_trigger_names():
    definition = Definition(
        type="rest",
        id="rest",
        data={
            "lengths": {
                "week": {
                    "days": 7,
                    "full": True,
                    "restores": [],
                    "clears": [],
                    "ends": ["rest", "extended_rest"],
                }
            }
        },
    )

    loaded = _load_rests([definition], None, "testset", None, ("rest", "extended_rest"))

    assert loaded["week"].ends == ("rest", "extended_rest")


def test_a_rest_definition_refuses_a_trigger_the_rule_set_does_not_name():
    definition = Definition(
        type="rest",
        id="rest",
        data={
            "lengths": {
                "week": {
                    "days": 7,
                    "full": True,
                    "restores": [],
                    "clears": [],
                    "ends": ["magic"],
                }
            }
        },
    )

    with pytest.raises(ContentError, match="'ends' must list only"):
        _load_rests([definition], None, "testset", None, ("rest", "extended_rest"))


def test_a_rest_ends_conditions_by_the_triggers_in_its_definition(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    with _db(app) as db:
        record(db, campaign_id, ConditionAdded(name=PC, condition="walloped", cause="a test"))
        db.commit()
    # A short rest that is neither full nor multi-day still ends the condition when its own
    # definition names the trigger, so the engine is not inferring triggers from those flags.
    tea = replace(CAIRN.rests["moments"], id="tea", ends=("rest",))
    tools = replace(tools, rests={**tools.rests, "tea": tea})

    tools.rest("tea", reason="tea")

    assert _character(app, campaign_id, PC).conditions == []


def test_a_rest_with_no_configured_trigger_ends_no_conditions(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    with _db(app) as db:
        record(db, campaign_id, ConditionAdded(name=PC, condition="walloped", cause="a test"))
        db.commit()
    tea = replace(CAIRN.rests["moments"], id="tea", ends=())
    tools = replace(tools, rests={**tools.rests, "tea": tea})

    tools.rest("tea", reason="tea")

    assert _character(app, campaign_id, PC).conditions == ["walloped"]
