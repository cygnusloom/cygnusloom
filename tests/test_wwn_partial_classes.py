"""Worlds Without Number partial-class pairings: Healer and Vowed as Adventurers."""

from __future__ import annotations

import random

import pytest

from cygnusloom.content.character import CharacterError, check_choices, resolve
from cygnusloom.content.library import ContentLibrary

STATS = {
    "str": 10,
    "dex": 10,
    "con": 10,
    "int": 10,
    "wis": 10,
    "cha": 10,
    "hp": 6,
    "attack_bonus": 1,
    "castings": 1,
    "morale": 7,
}


def _spec():
    return ContentLibrary.load().rule_sets["worldswithoutnumber"].character


def _choices(**templates: str) -> dict[str, object]:
    return {"templates": templates, "stats": dict(STATS)}


@pytest.mark.parametrize(
    ("class_id", "second_class", "tradition"),
    [
        ("healer", "vowed_partial_2", "no_tradition"),
        ("vowed", "healer_partial_2", "no_tradition"),
        ("healer", "warrior_partial_2", "no_tradition"),
        ("vowed", "warrior_partial_2", "no_tradition"),
        ("healer", "expert_partial_2", "no_tradition"),
        ("vowed", "expert_partial_2", "no_tradition"),
        ("healer", "mage_partial_2", "high_mage_partial"),
        ("vowed", "mage_partial_2", "elementalist_partial"),
    ],
)
def test_source_valid_dual_partial_pairs_are_allowed(
    class_id: str, second_class: str, tradition: str
) -> None:
    spec = _spec()
    check_choices(
        spec,
        _choices(**{"class": class_id, "class2": second_class, "tradition": tradition}),
    )


@pytest.mark.parametrize("class_id", ["healer", "vowed"])
def test_a_partial_class_cannot_be_taken_without_a_pair(class_id: str) -> None:
    spec = _spec()
    with pytest.raises(CharacterError, match="must pair with another partial class"):
        check_choices(spec, _choices(**{"class": class_id, "tradition": "no_tradition"}))


@pytest.mark.parametrize(
    ("class_id", "second_class", "message"),
    [
        ("healer", "healer_partial_2", "must pair with a different partial class"),
        ("vowed", "vowed_partial_2", "must pair with a different partial class"),
    ],
)
def test_two_identical_partial_classes_are_refused(
    class_id: str, second_class: str, message: str
) -> None:
    spec = _spec()
    with pytest.raises(CharacterError, match=message):
        check_choices(
            spec,
            _choices(**{"class": class_id, "class2": second_class, "tradition": "no_tradition"}),
        )


@pytest.mark.parametrize(
    ("templates", "message"),
    [
        (
            {"class": "warrior", "class2": "healer_partial_2", "tradition": "no_tradition"},
            "cannot be chosen by a Warrior",
        ),
        (
            {
                "class": "adventurer_expert_warrior",
                "class2": "warrior_partial_2",
                "tradition": "no_tradition",
            },
            "cannot be chosen by a Adventurer",
        ),
        (
            {
                "class": "healer",
                "class2": "warrior_partial_2",
                "tradition": "high_mage_partial",
            },
            "cannot be chosen by a High Mage, Partial",
        ),
        (
            {"class": "healer", "class2": "mage_partial_2", "tradition": "no_tradition"},
            "cannot be chosen by a No Tradition",
        ),
        ({"class": "healer", "class2": "mage_partial_2"}, "requires a tradition choice"),
    ],
)
def test_invalid_partial_class_pairings_are_refused(
    templates: dict[str, str], message: str
) -> None:
    spec = _spec()
    with pytest.raises(CharacterError, match=message):
        check_choices(spec, _choices(**templates))


@pytest.mark.parametrize(
    ("class_id", "tradition"),
    [
        ("warrior", "no_tradition"),
        ("expert", "no_tradition"),
        ("mage", "high_mage"),
        ("adventurer_expert_warrior", "no_tradition"),
        ("adventurer_warrior_mage", "high_mage_partial"),
        ("adventurer_mage_mage", "high_mage_partial"),
    ],
)
def test_full_classes_still_work(class_id: str, tradition: str) -> None:
    spec = _spec()
    templates = {"class": class_id, "tradition": tradition}
    if class_id == "adventurer_mage_mage":
        templates["tradition2"] = "necromancer_partial_2"
    check_choices(spec, _choices(**templates))
    sheet = resolve(
        spec,
        _choices(**templates),
        random.Random(4),
        plus="sum",
    )
    assert "class2" not in sheet.templates


def test_a_healer_and_vowed_pair_combines_both_sets_of_arts() -> None:
    spec = _spec()
    submitted = _choices(
        **{"class": "healer", "class2": "vowed_partial_2", "tradition": "no_tradition"}
    )
    submitted["skill_picks"] = {"second_order_skill": ["trade"]}
    sheet = resolve(spec, submitted, random.Random(5), plus="sum")

    assert sheet.templates == {
        "class": "healer",
        "class2": "vowed_partial_2",
        "tradition": "no_tradition",
    }
    assert sheet.features["art_healing_touch"] == 1
    for art in ("art_martial_style", "art_unarmed_might", "art_unarmored_defense"):
        assert sheet.features[art] == 1
    assert "heal" in sheet.skills
    assert "trade" in sheet.skills
    assert sheet.stats["hp"] == STATS["hp"] - 1
    assert sheet.stats["attack_bonus"] == STATS["attack_bonus"] - 1


def test_a_vowed_and_healer_pair_is_the_same_second_partial_class() -> None:
    spec = _spec()
    submitted = _choices(
        **{"class": "vowed", "class2": "healer_partial_2", "tradition": "no_tradition"}
    )
    sheet = resolve(spec, submitted, random.Random(6), plus="sum")

    assert sheet.templates["class2"] == "healer_partial_2"
    assert "heal" in sheet.skills
    assert sheet.fields["order_skill"] in sheet.skills
    for art in ("art_martial_style", "art_unarmed_might", "art_unarmored_defense"):
        assert sheet.features[art] == 1
