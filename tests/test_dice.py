"""Dice formulas. A formula is one or more terms joined by "+": single terms such as "3d6" for ranges,
and sums such as "1d8+1d8" for rule sets that combine several terms. What "+" means between terms
is the rule set's `plus` mode: "sum" adds every term, and "highest" rolls each term and keeps the
higher total (Cairn 2e's two weapons). The old "|" keep-highest grammar is gone."""

import random

import pytest

from cygnusforge.rules.dice import FORMULA, is_formula, roll_total


@pytest.mark.parametrize("text", ["1d20", "3d6", "2d8", "1d8+1d8", "2d6+1d4", "2d6+1d4+1d6"])
def test_accepts_roll_formulas(text: str) -> None:
    assert is_formula(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "d6",
        "0d6",
        "1d0",
        "1d8+",
        "+1d8",
        "1d8 + 1d8",
        "1d6|1d6",
        "1d6|",
        None,
        6,
    ],
)
def test_rejects_non_formulas(text: object) -> None:
    assert not is_formula(text)


def test_stat_ranges_take_a_single_term() -> None:
    assert FORMULA.fullmatch("3d6") is not None
    assert FORMULA.fullmatch("1d8+1d8") is None


def test_single_term_ignores_plus() -> None:
    expected = roll_total("2d8", random.Random(7))
    assert roll_total("2d8", random.Random(7), plus="highest") == expected
    assert roll_total("2d8", random.Random(7), plus="sum") == expected


def test_plus_sum_adds_the_terms() -> None:
    assert roll_total("1d8+1d8", _Scripted(3, 5), plus="sum") == 8


def test_plus_highest_keeps_the_higher_term() -> None:
    assert roll_total("1d8+1d8", _Scripted(3, 7), plus="highest") == 7
    assert roll_total("1d8+1d8", _Scripted(6, 2), plus="highest") == 6


def test_several_terms_need_a_plus_mode() -> None:
    with pytest.raises(ValueError):
        roll_total("1d8+1d8")


def test_unknown_plus_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        roll_total("1d8+1d8", plus="median")


def test_roll_total_rejects_bad_formula() -> None:
    with pytest.raises(ValueError):
        roll_total("d8+")


class _Scripted(random.Random):
    def __init__(self, *values: int):
        super().__init__(0)
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        return self.values.pop(0)
