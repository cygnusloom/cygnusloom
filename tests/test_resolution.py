"""Roll resolution against a score or a difficulty. Saves, checks, morale, initiative, and retreat
share these rules: equality passes under either score mode, automatic rolls decide before the
comparison, and a check total adds its bonuses before it meets the difficulty."""

import random
from dataclasses import FrozenInstanceError

import pytest

from cygnusforge.services.resolution import (
    ROLL_OVER,
    ROLL_UNDER,
    resolve_roll,
    roll_check,
    roll_die,
    roll_score,
)


def test_roll_under_passes_at_or_under_the_score() -> None:
    assert roll_score(_Scripted(9), "1d20", 10).passed
    assert roll_score(_Scripted(10), "1d20", 10).passed
    assert not roll_score(_Scripted(11), "1d20", 10).passed


def test_roll_over_passes_at_or_over_the_score() -> None:
    assert roll_score(_Scripted(11), "1d20", 10, mode=ROLL_OVER).passed
    assert roll_score(_Scripted(10), "1d20", 10, mode=ROLL_OVER).passed
    assert not roll_score(_Scripted(9), "1d20", 10, mode=ROLL_OVER).passed


def test_equal_rolls_pass_under_both_modes() -> None:
    under = roll_score(_Scripted(12), "1d20", 12, mode=ROLL_UNDER)
    over = roll_score(_Scripted(12), "1d20", 12, mode=ROLL_OVER)
    assert under.passed
    assert over.passed
    assert under.roll == over.roll == 12


def test_supplied_rolls_follow_the_same_rules() -> None:
    assert resolve_roll(10, 10).passed
    assert resolve_roll(10, 10, mode=ROLL_OVER).passed
    assert not resolve_roll(11, 10).passed
    assert not resolve_roll(9, 10, mode=ROLL_OVER).passed


def test_auto_success_and_auto_fail_decide_whatever_the_mode() -> None:
    passed = roll_score(_Scripted(20), "1d20", 1, auto_success=20, auto_fail=1)
    failed = roll_score(_Scripted(1), "1d20", 20, auto_success=20, auto_fail=1)
    assert passed.passed
    assert passed.automatic
    assert not failed.passed
    assert failed.automatic


def test_auto_success_wins_when_both_flags_name_the_same_roll() -> None:
    result = resolve_roll(7, 7, auto_success=7, auto_fail=7)
    assert result.passed
    assert result.automatic


def test_a_comparison_is_not_automatic() -> None:
    result = roll_score(_Scripted(10), "1d20", 10)
    assert result.passed
    assert not result.automatic


def test_highest_keeps_the_best_term_and_flat_bonuses_still_add() -> None:
    result = roll_score(_Scripted(3, 7), "1d8+1d8+2", 10, mode=ROLL_OVER, plus="highest")
    assert result.roll == 9
    assert not result.passed


def test_sum_adds_terms_and_flat_bonuses() -> None:
    assert roll_die(_Scripted(3, 4), "1d6+1d6+2", plus="sum") == 9


def test_check_adds_negative_levels_and_modifiers() -> None:
    result = roll_check(
        _Scripted(12), "1d20", skill_level=-1, stat_modifier=-2, difficulty=10, modifier=-4
    )
    assert result.roll == 12
    assert result.total == 5
    assert not result.passed
    assert result.margin == -5


def test_check_passes_when_the_total_equals_the_difficulty() -> None:
    result = roll_check(_Scripted(10), "1d20", skill_level=2, stat_modifier=-1, difficulty=11)
    assert result.total == 11
    assert result.passed
    assert result.margin == 0


def test_check_difficulty_is_not_clamped() -> None:
    assert roll_check(_Scripted(2), "1d20", skill_level=0, stat_modifier=0, difficulty=0).passed
    result = roll_check(_Scripted(20), "1d20", skill_level=0, stat_modifier=0, difficulty=99)
    assert not result.passed
    assert result.margin == -79


def test_same_seed_makes_the_same_rolls() -> None:
    first = roll_score(random.Random(1234), "1d20+1d6", 12, plus="sum")
    second = roll_score(random.Random(1234), "1d20+1d6", 12, plus="sum")
    assert first == second


def test_results_are_immutable() -> None:
    result = roll_score(_Scripted(10), "1d20", 10)
    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown roll mode"):
        roll_score(_Scripted(10), "1d20", 10, mode="roll_sideways")


@pytest.mark.parametrize("die", ["", "d20", "1d0", "1d20+"])
def test_bad_die_is_rejected(die: str) -> None:
    with pytest.raises(ValueError, match="dice formula"):
        roll_score(_Scripted(10), die, 10)


def test_unknown_plus_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        roll_score(_Scripted(1, 1), "1d6+1d6", 10, plus="median")


def test_non_integer_arguments_are_rejected() -> None:
    with pytest.raises(TypeError, match="score"):
        roll_score(_Scripted(10), "1d20", 10.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="skill_level"):
        roll_check(
            _Scripted(10),
            "1d20",
            skill_level=True,  # type: ignore[arg-type]
            stat_modifier=0,
            difficulty=10,
        )


class _Scripted(random.Random):
    def __init__(self, *values: int) -> None:
        super().__init__(0)
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        return self.values.pop(0)
