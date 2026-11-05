from __future__ import annotations

from dataclasses import replace

import pytest

from cygnusloom.content.character import DefenseFloor, build_character
from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.services.combat import equipped_defense
from cygnusloom.services.events import Character
from tests.factories import rules_for

# The fixture rule set's character spec: a neutral base to hang a defense floor on. Its Resolve
# stat has no modifier bands, so a score of 1 is a +1 modifier.
SPEC = rules_for("rolltest").character
FEATURE = "steady-nerves"
COUNTER = "focus"


def _definitions(floor: object, *, counter: bool = True) -> list[Definition]:
    result = [
        Definition(
            "feature",
            "natural_defense",
            {
                "type": "feature",
                "id": "natural_defense",
                "name": "Natural Defense",
                "feature_type": "trait",
                "defense_floor": floor,
            },
        )
    ]
    if counter:
        result.append(Definition("counter", "tier", {"type": "counter", "id": "tier"}))
    return result


@pytest.mark.parametrize(
    "floor",
    [
        {"base": -1},
        {"base": True},
        {"base": 12, "counter": "tier", "divisor": 0},
        {"base": 12, "counter": "tier", "round_up": "yes"},
        {"base": 12, "divisor": 2},
        {"base": 12, "counter": "missing"},
        {"base": 12, "requires_no_armor": 1},
        {"base": 12, "requires_no_shield": "no"},
    ],
)
def test_invalid_defense_floors_are_rejected_at_load(floor: object):
    with pytest.raises(ContentError, match="defense_floor"):
        build_character(_definitions(floor), {}, {}, "test")


def test_defense_floor_requires_its_counter_even_when_no_counters_exist():
    with pytest.raises(ContentError, match="names no counter"):
        build_character(
            _definitions({"base": 12, "counter": "tier"}, counter=False), {}, {}, "test"
        )


@pytest.mark.parametrize(
    ("tier", "body", "shield", "adjustment", "round_up", "expected"),
    [
        (1, 0, 0, 0, True, 14),
        (4, 0, 0, 0, True, 15),
        (4, 0, 0, 0, False, 14),
        (4, 2, 3, 0, True, 16),
        (4, 8, 4, 2, True, 22),
    ],
)
def test_equipped_defense_uses_configured_floor_and_separate_equipment(
    tier: int, body: int, shield: int, adjustment: int, round_up: bool, expected: int
):
    natural = replace(
        SPEC.features[FEATURE],
        id="natural_defense",
        defense_floor=DefenseFloor(base=12, counter="tier", divisor=3, round_up=round_up),
    )
    tier_counter = replace(SPEC.counters[COUNTER], id="tier", initial=1)
    spec = replace(
        SPEC,
        features={**SPEC.features, natural.id: natural},
        counters={**SPEC.counters, "tier": tier_counter},
    )
    character = Character(
        kind="pc",
        name="Example",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
        stats={"resolve": 1},
        features={natural.id: 1},
        counters={"tier": tier},
        equipped_armor_value=body,
        equipped_shield_value=shield,
        defense_adjustment=adjustment,
    )

    assert equipped_defense(character, spec, 10, "resolve") == expected


def test_equipped_defense_ignores_a_zero_quantity_feature():
    character = Character(
        kind="pc",
        name="Example",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
        stats={"resolve": 1},
        features={FEATURE: 0},
        equipped_armor_value=2,
    )

    assert equipped_defense(character, SPEC, 10, "resolve") == 13


@pytest.mark.parametrize(
    ("no_armor", "no_shield", "body", "body_value", "shield", "shield_value", "expected"),
    [
        (True, False, "", 0, "Shield", 3, 17),
        (False, True, "Coat", 2, "", 0, 16),
        (True, False, "Coat", 2, "", 0, 13),
        (True, False, "Coat", 0, "", 0, 11),
        (False, True, "", 0, "Shield", 3, 14),
        (False, True, "", 0, "Shield", 0, 11),
    ],
)
def test_defense_floor_can_require_empty_equipment_slots(
    no_armor: bool,
    no_shield: bool,
    body: str,
    body_value: int,
    shield: str,
    shield_value: int,
    expected: int,
):
    natural = replace(
        SPEC.features[FEATURE],
        defense_floor=DefenseFloor(
            base=15, requires_no_armor=no_armor, requires_no_shield=no_shield
        ),
    )
    spec = replace(SPEC, features={**SPEC.features, natural.id: natural})
    character = Character(
        kind="pc",
        name="Example",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
        stats={"resolve": 1},
        features={natural.id: 1},
        equipped_armor=body,
        equipped_armor_value=body_value,
        equipped_shield=shield,
        equipped_shield_value=shield_value,
    )

    assert equipped_defense(character, spec, 10, "resolve") == expected
