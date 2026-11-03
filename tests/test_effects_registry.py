from __future__ import annotations

import re
from typing import Any

import pytest

from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import EFFECT_RULES, _check_effects, _load_injury_entries
from cygnusforge.services.effects import EFFECT_EXECUTORS
from tests.conftest import content_library

CAIRN = content_library().rule_sets["cairn2e"]


def _table(*ops: dict[str, Any]) -> Definition:
    return Definition(
        type="injury_table",
        id="injuries",
        data={
            "type": "injury_table",
            "id": "injuries",
            "entries": [
                {"roll": 1, "name": "Test", "text": "A test.", "effects": list(ops)}
            ],
        },
    )


def _load(*ops: dict[str, Any]):
    return _load_injury_entries(_table(*ops), CAIRN.character, CAIRN.conditions, "testset")


def test_every_effect_op_has_a_load_check_and_a_runtime_executor():
    assert set(EFFECT_RULES) == set(EFFECT_EXECUTORS)


def test_an_unknown_effect_op_is_rejected_at_load_time():
    with pytest.raises(ContentError, match=r"effect op 'explode' is not one of"):
        _load({"op": "explode"})


def test_an_unknown_effect_key_is_rejected_at_load_time():
    with pytest.raises(ContentError, match=r"unknown key\(s\) 'bogus'"):
        _load({"op": "note", "text": "A note.", "bogus": True})


@pytest.mark.parametrize(
    ("op", "message"),
    [
        ({"op": "note"}, "'text' must be a non-empty string"),
        (
            {"op": "set_max", "stat": "luck", "dice": "1d6", "mode": "add"},
            "set_max names no stat ('luck')",
        ),
        (
            {"op": "set_max", "stat": "hp", "dice": "nope", "mode": "add"},
            "set_max 'dice' is not a formula ('nope')",
        ),
        (
            {"op": "set_max", "stat": "hp", "dice": "1d6", "mode": "maybe"},
            "set_max 'mode' must be one of",
        ),
        (
            {"op": "set_max", "stat": "$picked", "dice": "1d6", "mode": "add"},
            "'$picked' is used before a pick_stat",
        ),
        ({"op": "condition", "id": "ghost"}, "condition op names no stored condition ('ghost')"),
        ({"op": "save", "stat": "luck"}, "save names no stat ('luck')"),
        (
            {"op": "pick_stat", "options": [], "dice": "1d6"},
            "pick_stat 'options' must be a non-empty list",
        ),
        (
            {"op": "pick_stat", "options": ["luck"], "dice": "1d6"},
            "pick_stat names no stat ('luck')",
        ),
        (
            {"op": "pick_stat", "options": ["str"], "dice": "2d6"},
            "pick_stat 'dice' must be a single die such as '1d6'",
        ),
        (
            {"op": "raise_stat", "stat": "hp", "delta": 0, "cap": 10},
            "raise_stat 'delta' must be a positive whole number",
        ),
        (
            {"op": "raise_stat", "stat": "hp", "delta": 1, "cap": 0},
            "raise_stat 'cap' must be a positive whole number",
        ),
        (
            {"op": "counter", "id": "luck", "delta": 1},
            "effect op 'counter' is only allowed in moves and counters",
        ),
        (
            {"op": "end_condition", "id": "ghost"},
            "effect op 'end_condition' is only allowed in moves and counters",
        ),
    ],
)
def test_malformed_effect_payloads_are_rejected_at_load_time(
    op: dict[str, Any], message: str
):
    with pytest.raises(ContentError, match=re.escape(message)):
        _load(op)


def test_an_end_condition_names_a_stored_condition_where_moves_allow_it():
    with pytest.raises(
        ContentError, match=re.escape("end_condition names no stored condition ('ghost')")
    ):
        _check_effects(
            [{"op": "end_condition", "id": "ghost"}],
            "testset",
            CAIRN.character,
            frozenset(CAIRN.conditions),
            False,
            counters=frozenset(CAIRN.counters),
        )
