from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import SpellCast, current, decode, events_since
from tests.test_combat import (
    PC,
    Rolls,
    _character,
    _declarative,
    _fight,
    _set_condition,
    _set_stat,
    _tools,
    save_succeeds,
)
from tests.test_world import _db

SPELL = "Magic Missile"
TEXT = "Fires a bolt of force that never misses."


def _used(app: FastAPI, campaign_id: int) -> int:
    return sum(item.slots for item in _character(app, campaign_id, PC).items)


def _named(app: FastAPI, campaign_id: int, name: str) -> list[Any]:
    return [i for i in _character(app, campaign_id, PC).items if i.name == name]


def _fatigue(app: FastAPI, campaign_id: int) -> list[Any]:
    return [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]


def test_a_spellbook_holds_one_spell_for_one_slot_with_its_text(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    before = _used(app, campaign_id)
    result = tools.give_spell_item(PC, SPELL, TEXT, role="reusable")
    assert result["slots_used"] == before + 1
    book = _named(app, campaign_id, SPELL)
    assert [(i.kind, i.size, i.slots, i.text) for i in book] == [("spellbook", "normal", 1, TEXT)]


def test_casting_from_a_spellbook_adds_exactly_one_fatigue(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_spell_item(PC, SPELL, TEXT, role="reusable")
    assert not _fatigue(app, campaign_id)
    result = tools.cast_spell(PC, SPELL)
    assert result["source"] == "spellbook"
    assert result["penalty"] is True
    assert len(_fatigue(app, campaign_id)) == 1
    assert _named(app, campaign_id, SPELL), "a spellbook is kept after casting"


def test_a_spellbook_is_matched_by_spell_name_regardless_of_case(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_spell_item(PC, SPELL, TEXT, role="reusable")
    result = tools.cast_spell(PC, SPELL.upper())
    assert result["source"] == "spellbook"
    assert len(_fatigue(app, campaign_id)) == 1


def test_casting_a_spell_not_held_is_refused_and_names_the_spell(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="Fireball"):
        tools.cast_spell(PC, "Fireball")
    assert not _fatigue(app, campaign_id)


def test_a_scroll_is_petty_and_takes_no_slots(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    before = _used(app, campaign_id)
    result = tools.give_spell_item(PC, SPELL, TEXT, role="single_use")
    assert result["slots_used"] == before
    scroll = _named(app, campaign_id, SPELL)
    assert [(i.kind, i.size, i.slots) for i in scroll] == [("scroll", "petty", 0)]


def test_casting_a_scroll_uses_it_up_and_adds_no_fatigue(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_spell_item(PC, SPELL, TEXT, role="single_use")
    result = tools.cast_spell(PC, SPELL)
    assert result["source"] == "scroll"
    assert result["penalty"] is False
    assert not _named(app, campaign_id, SPELL)
    assert not _fatigue(app, campaign_id)


def test_a_scroll_cannot_be_cast_twice(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    tools.give_spell_item(PC, SPELL, TEXT, role="single_use")
    tools.cast_spell(PC, SPELL)
    with pytest.raises(InvalidInput, match=SPELL):
        tools.cast_spell(PC, SPELL)


def test_a_spellbook_is_used_before_a_scroll_of_the_same_spell(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_spell_item(PC, SPELL, TEXT, role="reusable")
    tools.give_spell_item(PC, SPELL, TEXT, role="single_use")
    result = tools.cast_spell(PC, SPELL)
    assert result["source"] == "spellbook"
    assert _named(app, campaign_id, SPELL), "the scroll is still held"
    assert len(_fatigue(app, campaign_id)) == 1


@pytest.mark.parametrize(("role", "source"), [("reusable", "grimoire"), ("single_use", "sigil")])
def test_spell_item_kinds_determine_cast_results_and_events(
    client_factory, role: Literal["reusable", "single_use"], source: str
):
    app, campaign_id, tools = _fight(client_factory)
    assert tools.magic is not None
    rules = replace(
        tools.magic,
        reusable=replace(tools.magic.reusable, kind="grimoire"),
        single_use=replace(tools.magic.single_use, kind="sigil"),
    )
    tools = replace(tools, magic=rules)
    tools.give_spell_item(PC, SPELL, TEXT, role=role)

    result = tools.cast_spell(PC, SPELL)

    assert result["source"] == source
    assert bool(_named(app, campaign_id, SPELL)) is (role == "reusable")
    with _db(app) as db:
        casts = [decode(row) for row in events_since(db, campaign_id) if row.kind == SpellCast.kind]
    assert len(casts) == 1
    assert isinstance(casts[0], SpellCast)
    assert casts[0].source == source
    assert casts[0].render() == f"{PC} casts {SPELL} from a {source}."


def test_a_deprived_character_can_still_cast_from_a_spellbook(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    _set_condition(tools, PC, True, cause="no food")
    tools.give_spell_item(PC, SPELL, TEXT, role="reusable")
    assert tools.cast_spell(PC, SPELL)["penalty"] is True


def test_a_relic_starts_at_its_uses_and_records_its_recharge_rule(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_charged_item(PC, "Ring of Sight", uses=2, recharge="Recharges at dawn.")
    relic = _named(app, campaign_id, "Ring of Sight")
    assert [(i.kind, i.uses, i.max_uses, i.text) for i in relic] == [
        ("relic", 2, 2, "Recharges at dawn.")
    ]


def test_using_a_relic_decrements_its_uses_and_adds_no_fatigue(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_charged_item(PC, "Ring of Sight", uses=2, recharge="Recharges at dawn.")
    result = tools.use_charge(PC, "Ring of Sight")
    assert result == {"name": PC, "item": "Ring of Sight", "uses_left": 1}
    assert [i.uses for i in _named(app, campaign_id, "Ring of Sight")] == [1]
    assert not _fatigue(app, campaign_id)


def test_a_relic_with_no_uses_left_must_be_recharged_before_use(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    tools.give_charged_item(PC, "Ring of Sight", uses=1, recharge="Recharges at dawn.")
    tools.use_charge(PC, "Ring of Sight")
    with pytest.raises(InvalidInput, match="recharg"):
        tools.use_charge(PC, "Ring of Sight")


def test_recharging_a_relic_restores_its_maximum_uses(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_charged_item(PC, "Ring of Sight", uses=3, recharge="Recharges at dawn.")
    tools.use_charge(PC, "Ring of Sight")
    tools.use_charge(PC, "Ring of Sight")
    tools.advance_time(days=1)
    result = tools.recharge_charged_item(PC, "Ring of Sight")
    assert result == {"name": PC, "item": "Ring of Sight", "uses": 3}
    assert [i.uses for i in _named(app, campaign_id, "Ring of Sight")] == [3]


def test_a_relic_cannot_recharge_until_time_passes_after_its_last_use(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_charged_item(PC, "Ring of Sight", uses=2, recharge="Recharges at dawn.")
    tools.use_charge(PC, "Ring of Sight")
    with pytest.raises(InvalidInput, match="until time passes"):
        tools.recharge_charged_item(PC, "Ring of Sight")
    assert [i.uses for i in _named(app, campaign_id, "Ring of Sight")] == [1]


def test_recharging_a_relic_that_is_already_full_is_refused(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_charged_item(PC, "Ring of Sight", uses=2, recharge="Recharges at dawn.")
    with pytest.raises(InvalidInput, match="fully charged"):
        tools.recharge_charged_item(PC, "Ring of Sight")
    assert [i.uses for i in _named(app, campaign_id, "Ring of Sight")] == [2]


def test_a_relic_with_no_uses_is_refused_when_given(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    with pytest.raises(InvalidInput):
        tools.give_charged_item(PC, "Dud Ring", uses=0, recharge="Never.")
    assert not _named(app, campaign_id, "Dud Ring")


def _strength_max(app: FastAPI, campaign_id: int) -> int:
    return _character(app, campaign_id, PC).stats["str"]


def test_a_save_on_a_stat_passes_when_the_roll_is_at_or_under_the_score(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    score = _strength_max(app, campaign_id)
    assert current(_character(app, campaign_id, PC), "str") == score
    passing = _tools(app, campaign_id, rng=Rolls(score)).roll_save(PC, "str", "resist")
    failing = _tools(app, campaign_id, rng=Rolls(score + 1)).roll_save(PC, "str", "resist")
    assert passing["passed"] is True
    assert failing["passed"] is False


def test_a_save_result_reports_the_roll_stat_and_current_score(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    score = _strength_max(app, campaign_id)
    result = _tools(app, campaign_id, rng=Rolls(4)).roll_save(PC, "str", "resist the spell")
    assert result == {"name": PC, "stat": "str", "roll": 4, "score": score, "passed": True}


def test_a_roll_of_one_always_passes_a_save_and_a_twenty_always_fails(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    _set_stat(app, campaign_id, PC, "str", 0)
    lucky = _tools(app, campaign_id, rng=Rolls(1)).roll_save(PC, "str", "resist")
    unlucky = _tools(app, campaign_id, rng=Rolls(20)).roll_save(PC, "str", "resist")
    assert lucky["passed"] is True
    assert unlucky["passed"] is False


def test_player_save_bounds_and_engine_rolls_follow_a_non_d20_die(client_factory):
    _, _, tools = _fight(client_factory)
    tools = replace(tools, combat=_declarative(check_die="1d12"))

    with pytest.raises(InvalidInput, match="1 to 12"):
        tools.roll_save(PC, "str", "resist", natural=13)
    assert tools.roll_save(PC, "str", "resist", natural=12)["roll"] == 12
    rolled = replace(tools, rng=Rolls(12)).roll_save(PC, "str", "resist")
    assert rolled["roll"] == 12


def test_a_save_on_an_unknown_stat_is_refused(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    with pytest.raises(InvalidInput):
        tools.roll_save(PC, "zzz", "resist")


def test_a_save_rolls_a_d20_and_applies_the_save_rule(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    result = tools.roll_save(PC, "str", "resist")
    assert 1 <= result["roll"] <= 20
    assert 0 <= result["score"] <= 20
    assert result["passed"] is save_succeeds(result["roll"], result["score"])
