from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.definitions import ContentError
from cygnusforge.content.library import ContentLibrary, CounterRules, MoveBand, MoveRules
from cygnusforge.services import moves as moves_service
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    Character,
    EffectNoted,
    MoveMade,
    decode,
    events_since,
    modifier_value,
)
from tests.test_combat import CAIRN, PC, Rolls, _character, _fight, _tools
from tests.test_content import _core, _rule_set, _world, _write
from tests.test_world import _db

CAIRN_STAT = "str"


def _band(label: str, minimum: int | None, text: str = "", effects: tuple = ()) -> MoveBand:
    return MoveBand(label=label, text=text, min=minimum, effects=effects)


STANDARD = (
    _band("Strong", 10, "It works."),
    _band("Mixed", 7, "It works, at a cost."),
    _band("Weak", None, "It fails."),
)


def _move(bands: tuple[MoveBand, ...] = STANDARD, stat: str | None = None) -> MoveRules:
    return MoveRules(
        id="roll",
        label="Roll",
        description="A test move.",
        dice="2d6",
        stat=stat,
        bands=bands,
    )


@pytest.mark.parametrize(
    "total, label",
    [(14, "Strong"), (10, "Strong"), (9, "Mixed"), (7, "Mixed"), (6, "Weak"), (-3, "Weak")],
)
def test_band_for_takes_the_first_band_the_total_reaches(total: int, label: str):
    assert moves_service.band_for(_move(), total).label == label


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _world(),
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": _rule_set(),
        },
    )
    return tmp_path


def _move_json(**overrides: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "type": "move",
        "id": "roll",
        "label": "Roll",
        "description": "A test move.",
        "dice": "2d6",
        "bands": [
            {"label": "Strong", "min": 10, "text": "It works."},
            {"label": "Weak", "text": "It fails."},
        ],
    }
    move.update(overrides)
    return move


def _load_move(tree: Path, **overrides: Any) -> MoveRules:
    _write(
        tree,
        {
            "rulesets/testset/moves.json": [_move_json(**overrides)],
            "rulesets/testset/stats.json": [
                {"type": "stat", "id": "bravery", "name": "Bravery", "formula": "3d6"}
            ],
        },
    )
    return ContentLibrary.load(tree).rule_sets["testset"].moves["roll"]


def test_move_is_read_from_its_definition(tree: Path):
    move = _load_move(tree)
    assert move == MoveRules(
        id="roll",
        label="Roll",
        description="A test move.",
        dice="2d6",
        stat=None,
        bands=(
            MoveBand(label="Strong", text="It works.", min=10, effects=()),
            MoveBand(label="Weak", text="It fails.", min=None, effects=()),
        ),
    )


def test_move_can_add_a_stat(tree: Path):
    assert _load_move(tree, stat="bravery").stat == "bravery"


def test_move_can_let_the_player_choose_its_stat(tree: Path):
    move = _load_move(tree, stat_choice=["bravery"])
    assert move.stat is None
    assert move.stat_choice == ("bravery",)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"stat": "nerve"}, re.escape("'stat' names no stat ('nerve')")),
        (
            {"stat": "bravery", "stat_choice": ["bravery"]},
            "'stat_choice' cannot be given with a fixed 'stat'",
        ),
        ({"stat_choice": []}, "'stat_choice' must be a non-empty list of stats"),
        ({"stat_choice": "bravery"}, "'stat_choice' must be a non-empty list of stats"),
        ({"stat_choice": ["nerve"]}, re.escape("'stat_choice' names no stat ('nerve')")),
        ({"stat_choice": ["bravery", "bravery"]}, "'stat_choice' repeats a stat"),
        ({"dice": "d6"}, "'dice' is not a formula"),
        ({"bands": []}, "'bands' must be a non-empty list"),
        ({"bands": ["Strong"]}, "each band must be an object"),
        (
            {
                "bands": [
                    {"label": "Weak", "min": 2},
                    {"label": "Strong", "min": 10},
                    {"label": "Fail"},
                ]
            },
            "bands must run from the highest 'min' down",
        ),
        (
            {
                "bands": [
                    {"label": "Strong", "min": 10},
                    {"label": "Weak", "min": 10},
                    {"label": "Fail"},
                ]
            },
            "bands must run from the highest 'min' down",
        ),
        (
            {"bands": [{"label": "Strong"}, {"label": "Weak"}]},
            "every band but the last needs an integer 'min'",
        ),
        (
            {"bands": [{"label": "Strong", "min": 10}, {"label": "Weak", "min": 2}]},
            "the last band must have no 'min'",
        ),
        (
            {"bands": [{"label": "Strong", "min": 10, "text": 3}, {"label": "Weak"}]},
            "band 'text' must be text",
        ),
    ],
)
def test_bad_move_is_rejected(tree: Path, overrides: dict[str, Any], message: str):
    with pytest.raises(ContentError, match=message):
        _load_move(tree, **overrides)


def test_move_needs_a_label(tree: Path):
    with pytest.raises(ContentError, match="'label' must be a non-empty string"):
        _load_move(tree, label="")


def _move_for_fight(*, stat: str | None = None, effects: tuple = ()) -> MoveRules:
    return MoveRules(
        id="roll",
        label="Roll",
        description="A test move.",
        dice="2d6",
        stat=stat,
        bands=(
            _band("Strong", 10, "It works.", effects),
            _band("Weak", None, "It fails."),
        ),
    )


def _move_events(app: FastAPI, campaign_id: int) -> list[MoveMade]:
    with _db(app) as db:
        return [decode(row) for row in events_since(db, campaign_id) if row.kind == MoveMade.kind]


def _roll(app: FastAPI, campaign_id: int, move: MoveRules, rng: Rolls, **kwargs: Any) -> dict:
    with _db(app) as db:
        return moves_service.make_move(
            db,
            campaign_id,
            PC,
            move,
            rng,
            plus=CAIRN.plus,
            combat=CAIRN.combat,
            conditions=CAIRN.conditions,
            counters=CAIRN.counters,
            **kwargs,
        )


def test_a_move_rolls_its_dice_and_records_the_band(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
    result = _roll(app, campaign_id, _move(), Rolls(6, 4))
    assert result == {
        "move": "Roll",
        "roll": 10,
        "bonus": 0,
        "total": 10,
        "band": "Strong",
        "text": "It works.",
    }
    [event] = _move_events(app, campaign_id)
    assert (event.name, event.move, event.roll, event.total, event.band) == (
        PC,
        "Roll",
        10,
        10,
        "Strong",
    )


def test_a_move_adds_the_stat_and_the_situational_modifier(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    stat = modifier_value(_character(app, campaign_id, PC), CAIRN_STAT)
    result = _roll(app, campaign_id, _move_for_fight(stat=CAIRN_STAT), Rolls(3, 3), modifier=-2)
    assert result["roll"] == 6
    assert result["bonus"] == stat - 2
    assert result["total"] == 6 + stat - 2


def test_a_move_without_a_stat_adds_only_the_modifier(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    result = _roll(app, campaign_id, _move(), Rolls(2, 2), modifier=3)
    assert (result["roll"], result["bonus"], result["total"]) == (4, 3, 7)


def test_a_trait_can_go_below_zero():
    """Hogwarts traits run from -1 up, so a bonus can make the total smaller than the roll."""
    character = _character_stub(stats={"bravery": -1}, harm={})
    assert modifier_value(character, "bravery") == -1
    harmed = _character_stub(stats={"bravery": 1}, harm={"bravery": 2})
    assert modifier_value(harmed, "bravery") == -1


def _character_stub(stats: dict[str, int], harm: dict[str, int]) -> Character:
    return Character(
        kind="player",
        name=PC,
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
        stats=stats,
        harm=harm,
    )


def test_a_band_runs_its_effects(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
    move = _move_for_fight(effects=({"op": "note", "text": "The bell rings."},))
    _roll(app, campaign_id, move, Rolls(6, 6))
    with _db(app) as db:
        notes = [
            decode(row) for row in events_since(db, campaign_id) if row.kind == EffectNoted.kind
        ]
    assert [note.text for note in notes] == ["The bell rings."]


def test_a_move_for_an_unknown_character_is_refused(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
    with _db(app) as db, pytest.raises(InvalidInput, match="no character named 'Nobody'"):
        moves_service.make_move(
            db,
            campaign_id,
            "Nobody",
            _move(),
            Rolls(6, 6),
            plus=CAIRN.plus,
            combat=CAIRN.combat,
            conditions=CAIRN.conditions,
            counters=CAIRN.counters,
        )


def test_world_tools_roll_a_move_by_id(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
    tools = replace(_tools(app, campaign_id, Rolls(5, 5)), moves={"roll": _move_for_fight()})
    result = tools.roll_move("roll", PC)
    assert result["total"] == 10
    assert result["band"] == "Strong"


def test_world_tools_refuse_an_unknown_move(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
    tools = replace(_tools(app, campaign_id), moves={"roll": _move_for_fight()})
    with pytest.raises(InvalidInput, match="there is no move named 'climb'"):
        tools.roll_move("climb", PC)


def _load_testset(tree: Path, *definitions: dict[str, Any]):
    _write(
        tree,
        {
            "rulesets/testset/definitions.json": [
                {"type": "stat", "id": "bravery", "name": "Bravery", "formula": "3d6"},
                *definitions,
            ]
        },
    )
    return ContentLibrary.load(tree).rule_sets["testset"]


def _counter_json(**overrides: Any) -> dict[str, Any]:
    """A counter definition. An override set to None leaves that key out."""
    counter: dict[str, Any] = {"type": "counter", "id": "luck", "label": "Luck", "max": 3}
    counter.update(overrides)
    return {key: value for key, value in counter.items() if value is not None}


def _condition_json(**overrides: Any) -> dict[str, Any]:
    condition: dict[str, Any] = {
        "type": "condition",
        "id": "scared",
        "label": "Scared",
        "description": "Afraid.",
        "ends": [],
    }
    condition.update(overrides)
    return condition


def test_a_counter_is_read_with_its_bounds(tree: Path):
    rules = _load_testset(tree, _counter_json(min=0, max=3, initial=2))
    assert rules.counters["luck"] == CounterRules(
        id="luck", label="Luck", minimum=0, maximum=3, initial=2, full=()
    )


def test_a_counter_starts_at_its_minimum_when_no_initial_is_given(tree: Path):
    rules = _load_testset(tree, _counter_json(min=1, max=3))
    assert rules.counters["luck"].initial == 1


def test_a_counter_without_a_maximum_is_unbounded_above(tree: Path):
    rules = _load_testset(tree, _counter_json(max=None))
    assert rules.counters["luck"].maximum is None


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"initial": 4}, "'initial' is above 'max'"),
        ({"min": "zero"}, "'min' must be a whole number"),
        (
            {"max": None, "full": [{"op": "note", "text": "Full."}]},
            "'full' needs a 'max'",
        ),
        ({"full": [{"op": "counter", "id": "mana", "delta": 1}]}, "names no counter ('mana')"),
        (
            {"full": [{"op": "counter", "id": "luck"}]},
            "needs exactly one of 'delta', 'set' or 'reset'",
        ),
        ({"full": [{"op": "end_condition", "id": "ghost"}]}, "names no stored condition"),
        ({"label": ""}, "'label' must be a non-empty string"),
    ],
)
def test_a_bad_counter_is_rejected(tree: Path, overrides: dict[str, Any], message: str):
    with pytest.raises(ContentError, match=re.escape(message)):
        _load_testset(tree, _counter_json(**overrides), _condition_json())


def test_a_counter_can_end_a_stored_condition_when_it_fills(tree: Path):
    full = [{"op": "end_condition", "id": "scared"}]
    rules = _load_testset(tree, _counter_json(full=full), _condition_json())
    assert rules.counters["luck"].full == ({"op": "end_condition", "id": "scared"},)


def test_a_counter_op_in_a_condition_is_rejected(tree: Path):
    condition = _condition_json(on_end=[{"op": "counter", "id": "luck", "delta": 1}])
    with pytest.raises(ContentError, match="only allowed in moves and counters"):
        _load_testset(tree, _counter_json(), condition)


def test_a_move_can_change_a_counter(tree: Path):
    move = _move_json(
        bands=[
            {
                "label": "Strong",
                "min": 10,
                "effects": [{"op": "counter", "id": "luck", "delta": -1}],
            },
            {"label": "Weak"},
        ]
    )
    rules = _load_testset(tree, _counter_json(), move)
    [effect] = rules.moves["roll"].bands[0].effects
    assert effect == {"op": "counter", "id": "luck", "delta": -1}


def test_a_move_can_name_an_unknown_counter_only_when_it_exists(tree: Path):
    move = _move_json(
        bands=[
            {"label": "Strong", "min": 10, "effects": [{"op": "counter", "id": "mana", "set": 0}]},
            {"label": "Weak"},
        ]
    )
    with pytest.raises(ContentError, match="names no counter"):
        _load_testset(tree, move)


def test_a_condition_modifies_its_stat_and_every_stat(tree: Path):
    rules = _load_testset(
        tree,
        _condition_json(modifies={"bravery": -2}),
        _condition_json(id="injured", label="Injured", modifies={"*": -1}),
    )
    assert rules.conditions["scared"].modifies == {"bravery": -2}
    assert rules.conditions["injured"].modifies == {"*": -1}


@pytest.mark.parametrize(
    "modifies, message",
    [
        ({"nerve": -1}, "'modifies' names no stat ('nerve')"),
        ({"bravery": 1.5}, "'modifies' 'bravery' must be a whole number"),
        ({"bravery": True}, "'modifies' 'bravery' must be a whole number"),
        ("bravery", "'modifies' must be an object"),
    ],
)
def test_a_bad_condition_modifier_is_rejected(tree: Path, modifies: Any, message: str):
    with pytest.raises(ContentError, match=re.escape(message)):
        _load_testset(tree, _condition_json(modifies=modifies))


def test_end_condition_names_only_a_stored_condition(tree: Path):
    move = _move_json(
        bands=[
            {"label": "Strong", "min": 10, "effects": [{"op": "end_condition", "id": "scared"}]},
            {"label": "Weak"},
        ]
    )
    with pytest.raises(ContentError, match="end_condition names no stored condition"):
        _load_testset(tree, move)


def test_end_condition_is_allowed_for_a_stored_condition(tree: Path):
    move = _move_json(
        bands=[
            {"label": "Strong", "min": 10, "effects": [{"op": "end_condition", "id": "scared"}]},
            {"label": "Weak"},
        ]
    )
    rules = _load_testset(tree, _condition_json(), move)
    assert rules.moves["roll"].bands[0].effects == ({"op": "end_condition", "id": "scared"},)


def test_a_move_the_player_chooses_the_stat_for_rolls_that_stat(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    move = replace(_move_for_fight(), stat_choice=("str", "dex"))
    result = _roll(app, campaign_id, move, Rolls(3, 3), stat="dex")
    assert result["bonus"] == modifier_value(_character(app, campaign_id, PC), "dex")


def test_a_move_the_player_chooses_the_stat_for_needs_a_choice(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    move = replace(_move_for_fight(), stat_choice=("str", "dex"))
    with pytest.raises(InvalidInput, match="pick one of"):
        _roll(app, campaign_id, move, Rolls(3, 3))


def test_a_move_the_player_chooses_the_stat_for_refuses_a_stat_outside_the_choice(
    client_factory,
):
    app, campaign_id, _ = _fight(client_factory)
    move = replace(_move_for_fight(), stat_choice=("str", "dex"))
    with pytest.raises(InvalidInput, match="pick one of"):
        _roll(app, campaign_id, move, Rolls(3, 3), stat="wil")


def test_a_move_gated_on_every_condition_needs_conditions_to_exist(tree: Path):
    with pytest.raises(ContentError, match="needs_all_conditions, but the rule set has no"):
        _load_move(tree, needs_all_conditions=True)


def test_a_move_is_open_to_everyone_unless_it_needs_every_condition(tree: Path):
    assert _load_move(tree).needs_all_conditions is False
