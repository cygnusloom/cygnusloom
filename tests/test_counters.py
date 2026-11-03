from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.library import ConditionRules, CounterRules, MoveBand, MoveRules
from cygnusforge.services import moves as moves_service
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    ConditionAdded,
    ConditionEnded,
    CounterChanged,
    EffectNoted,
    decode,
    events_since,
    modifier_value,
    record,
)
from tests.factories import DEFAULT_PC, Rolls, character_named, db, rules_for, scene

ROLLTEST = rules_for()
PC = DEFAULT_PC
STAT = "resolve"


def _move(stat: str | None = STAT, effects: tuple[dict[str, Any], ...] = ()) -> MoveRules:
    return MoveRules(
        id="roll",
        label="Roll",
        description="A test move.",
        dice="2d6",
        stat=stat,
        bands=(
            MoveBand(label="Strong", text="It works.", min=10, effects=effects),
            MoveBand(label="Weak", text="It fails.", min=None, effects=()),
        ),
    )


def _condition(
    id: str = "scared", modifies: dict[str, int] | None = None, on_end: tuple = ()
) -> ConditionRules:
    return ConditionRules(
        id=id,
        label=id.capitalize(),
        description="A test condition.",
        ends=(),
        on_end=on_end,
        modifies=modifies or {},
    )


def _counter(
    id: str = "experience",
    *,
    maximum: int | None = 4,
    initial: int = 0,
    full: tuple[dict[str, Any], ...] = (),
) -> CounterRules:
    return CounterRules(
        id=id,
        label=id.capitalize(),
        minimum=0,
        maximum=maximum,
        initial=initial,
        full=full,
    )


def _roll(
    app: FastAPI,
    campaign_id: int,
    move: MoveRules,
    rng: Rolls,
    *,
    conditions: dict[str, ConditionRules] | None = None,
    counters: dict[str, CounterRules] | None = None,
) -> dict[str, Any]:
    with db(app) as session:
        return moves_service.make_move(
            session,
            campaign_id,
            PC,
            move,
            rng,
            plus=ROLLTEST.plus,
            combat=ROLLTEST.combat,
            conditions=conditions or {},
            counters=counters or {},
        )


def _add_condition(app: FastAPI, campaign_id: int, condition: str) -> None:
    with db(app) as session:
        record(session, campaign_id, ConditionAdded(name=PC, condition=condition))
        session.commit()


def _events(app: FastAPI, campaign_id: int, kind: str) -> list[Any]:
    with db(app) as session:
        return [decode(row) for row in events_since(session, campaign_id) if row.kind == kind]


def _notes(app: FastAPI, campaign_id: int) -> list[str]:
    return [event.text for event in _events(app, campaign_id, EffectNoted.kind)]


def test_a_condition_adds_its_modifier_to_a_roll_on_its_stat(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, _ = scene(client_factory)
    _add_condition(app, campaign_id, "scared")
    stat = modifier_value(character_named(app, campaign_id, PC), STAT)
    conditions = {"scared": _condition(modifies={STAT: -2})}
    result = _roll(app, campaign_id, _move(), Rolls(3, 3), conditions=conditions)
    assert result["bonus"] == stat - 2
    assert result["total"] == 6 + stat - 2


def test_a_condition_for_all_stats_applies_to_a_stat_roll(client_factory):
    app, campaign_id, _ = scene(client_factory)
    _add_condition(app, campaign_id, "injured")
    stat = modifier_value(character_named(app, campaign_id, PC), STAT)
    conditions = {"injured": _condition("injured", modifies={"*": -1})}
    result = _roll(app, campaign_id, _move(), Rolls(3, 3), conditions=conditions)
    assert result["bonus"] == stat - 1


def test_a_condition_for_all_stats_applies_to_a_roll_with_no_stat(client_factory):
    app, campaign_id, _ = scene(client_factory)
    _add_condition(app, campaign_id, "injured")
    conditions = {"injured": _condition("injured", modifies={"*": -1})}
    result = _roll(app, campaign_id, _move(stat=None), Rolls(3, 3), conditions=conditions)
    assert result["bonus"] == 0


def test_a_condition_on_another_stat_does_not_apply(client_factory):
    app, campaign_id, _ = scene(client_factory)
    _add_condition(app, campaign_id, "angry")
    stat = modifier_value(character_named(app, campaign_id, PC), STAT)
    conditions = {"angry": _condition("angry", modifies={"dex": -2})}
    result = _roll(app, campaign_id, _move(), Rolls(3, 3), conditions=conditions)
    assert result["bonus"] == stat


def test_a_condition_the_character_lacks_does_not_apply(client_factory):
    app, campaign_id, _ = scene(client_factory)
    stat = modifier_value(character_named(app, campaign_id, PC), STAT)
    conditions = {"scared": _condition(modifies={STAT: -2})}
    result = _roll(app, campaign_id, _move(), Rolls(3, 3), conditions=conditions)
    assert result["bonus"] == stat


def test_a_condition_ends_and_runs_its_on_end_effects(client_factory):
    app, campaign_id, _ = scene(client_factory)
    _add_condition(app, campaign_id, "scared")
    on_end = ({"op": "note", "text": "The fear passes."},)
    move = _move(effects=(({"op": "end_condition", "id": "scared"}),))
    conditions = {"scared": _condition(on_end=on_end)}
    _roll(app, campaign_id, move, Rolls(6, 6), conditions=conditions)
    [ended] = _events(app, campaign_id, ConditionEnded.kind)
    assert (ended.name, ended.condition) == (PC, "scared")
    assert character_named(app, campaign_id, PC).conditions == []
    assert "The fear passes." in _notes(app, campaign_id)


def test_ending_a_condition_the_character_lacks_is_noted(client_factory):
    app, campaign_id, _ = scene(client_factory)
    move = _move(effects=({"op": "end_condition", "id": "scared"},))
    _roll(app, campaign_id, move, Rolls(6, 6), conditions={"scared": _condition()})
    assert _events(app, campaign_id, ConditionEnded.kind) == []
    [text] = [t for t in _notes(app, campaign_id) if "does not have" in t]
    assert "does not end" in text


def test_a_band_can_change_a_counter(client_factory):
    app, campaign_id, _ = scene(client_factory)
    move = _move(effects=(({"op": "counter", "id": "experience", "delta": 1}),))
    _roll(app, campaign_id, move, Rolls(6, 6), counters={"experience": _counter()})
    assert character_named(app, campaign_id, PC).counters["experience"] == 1
    [change] = _events(app, campaign_id, CounterChanged.kind)
    assert (change.counter, change.before, change.after) == ("experience", 0, 1)


def test_adjusting_a_counter_stays_within_its_bounds(client_factory):
    app, campaign_id, tools = scene(client_factory)
    tools = replace(tools, counters={"luck": _counter("luck", maximum=3, initial=3)})
    assert tools.adjust_counter(PC, "luck", -5)["value"] == 0
    assert tools.adjust_counter(PC, "luck", 10)["value"] == 3
    assert character_named(app, campaign_id, PC).counters["luck"] == 3


def test_a_counter_at_its_bound_stays_and_says_so(client_factory):
    app, campaign_id, tools = scene(client_factory)
    tools = replace(tools, counters={"luck": _counter("luck", maximum=3, initial=3)})
    tools.adjust_counter(PC, "luck", 2)
    assert any("stays 3" in text for text in _notes(app, campaign_id))


def test_a_counter_starts_at_its_initial_value(client_factory):
    app, campaign_id, tools = scene(client_factory)
    tools = replace(tools, counters={"luck": _counter("luck", maximum=3, initial=2)})
    assert tools.adjust_counter(PC, "luck", 1)["value"] == 3
    [change] = _events(app, campaign_id, CounterChanged.kind)
    assert (change.before, change.after) == (2, 3)


def test_filling_a_counter_runs_its_full_effects(client_factory):
    app, campaign_id, tools = scene(client_factory)
    full = (
        {"op": "note", "text": "Advancement!"},
        {"op": "counter", "id": "experience", "set": 0},
    )
    tools = replace(tools, counters={"experience": _counter(full=full)})
    result = tools.adjust_counter(PC, "experience", 4)
    assert result["value"] == 0
    assert "Advancement!" in _notes(app, campaign_id)
    changes = [(c.before, c.after) for c in _events(app, campaign_id, CounterChanged.kind)]
    assert changes == [(0, 4), (4, 0)]


def test_a_counter_already_at_its_maximum_does_not_run_full_effects_again(client_factory):
    app, campaign_id, tools = scene(client_factory)
    full = ({"op": "note", "text": "Advancement!"},)
    tools = replace(tools, counters={"experience": _counter(maximum=4, initial=4, full=full)})
    tools.adjust_counter(PC, "experience", 1)
    assert "Advancement!" not in _notes(app, campaign_id)


def test_a_move_can_fill_a_counter_that_runs_a_move_effect(client_factory):
    app, campaign_id, _ = scene(client_factory)
    full = ({"op": "note", "text": "Advancement!"},)
    move = _move(effects=({"op": "counter", "id": "experience", "delta": 1},))
    counters = {"experience": _counter(maximum=1, full=full)}
    _roll(app, campaign_id, move, Rolls(6, 6), counters=counters)
    assert "Advancement!" in _notes(app, campaign_id)


def test_adjusting_an_unknown_counter_is_refused(client_factory):
    _, _, tools = scene(client_factory)
    tools = replace(tools, counters={"luck": _counter("luck")})
    with pytest.raises(InvalidInput, match="there is no counter named 'mana'"):
        tools.adjust_counter(PC, "mana", 1)


def test_adjusting_a_counter_for_an_unknown_character_is_refused(client_factory):
    _, _, tools = scene(client_factory)
    tools = replace(tools, counters={"luck": _counter("luck")})
    with pytest.raises(InvalidInput, match="no character named 'Nobody'"):
        tools.adjust_counter("Nobody", "luck", 1)
