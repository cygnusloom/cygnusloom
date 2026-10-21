"""Dice formulas: single terms for ranges, and sums of terms for rolls such as "d8+d8"."""

import random

import pytest

from cygnusforge.rules.dice import FORMULA, is_formula, roll_total


@pytest.mark.parametrize("text", ["1d20", "3d6", "2d8", "1d8+1d8", "2d6+1d4"])
def test_accepts_roll_formulas(text: str) -> None:
    assert is_formula(text)


@pytest.mark.parametrize("text", ["", "d6", "0d6", "1d0", "1d8+", "+1d8", "1d8 + 1d8", None, 6])
def test_rejects_non_formulas(text: object) -> None:
    assert not is_formula(text)


def test_stat_ranges_take_a_single_term() -> None:
    assert FORMULA.fullmatch("3d6") is not None
    assert FORMULA.fullmatch("1d8+1d8") is None


def test_roll_total_sums_every_term() -> None:
    rng = random.Random(7)
    total = roll_total("1d8+1d8", rng)
    assert 2 <= total <= 16
    assert total == roll_total("2d8", random.Random(7))


def test_roll_total_rejects_bad_formula() -> None:
    with pytest.raises(ValueError):
        roll_total("d8+")
