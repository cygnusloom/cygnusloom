from __future__ import annotations

import json
import random

import pytest

from cygnusforge.content.character import character_schema, describe, resolve
from tests.conftest import content_library

HOGWARTS = content_library().rule_sets["hogwarts-rpg"]
SPEC = HOGWARTS.character

HOUSES = {
    "gryffindor": "bravery",
    "ravenclaw": "intellect",
    "hufflepuff": "loyalty",
    "slytherin": "cunning",
}
BLOOD = {"muggle-born": 3, "half-blood": 2, "pure-blood": 1}
EVEN_STATS = {"bravery": 0, "cunning": 0, "intellect": 0, "loyalty": 0, "magic": 0}


def _values(table: str) -> set[str]:
    return {option.value for option in SPEC.tables[table]}


@pytest.mark.parametrize("house,trait", HOUSES.items())
def test_each_house_adds_one_point_to_its_trait(house, trait):
    sheet = resolve(
        SPEC,
        {"stats": EVEN_STATS, "templates": {"house": house}},
        random.Random(1),
        plus=HOGWARTS.plus,
    )
    assert sheet.stats[trait] == 1
    assert sum(sheet.stats.values()) == 1


@pytest.mark.parametrize("blood,experience", BLOOD.items())
def test_each_blood_status_starts_with_its_experience(blood, experience):
    sheet = resolve(
        SPEC,
        {"stats": EVEN_STATS, "templates": {"blood": blood}},
        random.Random(1),
        plus=HOGWARTS.plus,
    )
    assert sheet.counters == {"experience": experience}


def test_a_house_and_a_blood_status_combine():
    sheet = resolve(
        SPEC,
        {"stats": EVEN_STATS, "templates": {"house": "ravenclaw", "blood": "half-blood"}},
        random.Random(1),
        plus=HOGWARTS.plus,
    )
    assert sheet.stats["intellect"] == 1
    assert sheet.counters == {"experience": 2}
    assert sheet.templates == {"house": "ravenclaw", "blood": "half-blood"}


def test_the_creation_schema_offers_every_house_and_blood_status():
    schema = json.dumps(character_schema(SPEC))
    for template in [*HOUSES, *BLOOD]:
        assert f'"{template}"' in schema


def test_an_unrolled_character_takes_a_random_value_from_each_creation_table():
    sheet = resolve(SPEC, {}, random.Random(7), plus=HOGWARTS.plus)
    for field, table in [
        ("year", "year"),
        ("complexion", "complexion"),
        ("hair", "hair"),
        ("look", "build"),
        ("wand-look", "wand-look"),
        ("wand-core", "wand-core"),
        ("ambition", "ambition"),
        ("pet", "pet"),
        ("favourite-subject", "favourite-subject"),
        ("patronus", "patronus"),
        ("surname", "surnames"),
    ]:
        assert sheet.fields[field] in _values(table), field


def test_a_player_choice_of_a_table_field_is_kept():
    sheet = resolve(
        SPEC,
        {"fields": {"year": "7th Year", "patronus": "Owl", "pet": "Cat"}},
        random.Random(7),
        plus=HOGWARTS.plus,
    )
    assert sheet.fields["year"] == "7th Year"
    assert sheet.fields["patronus"] == "Owl"
    assert sheet.fields["pet"] == "Cat"


def test_text_fields_are_blank_until_the_player_writes_them():
    sheet = resolve(SPEC, {}, random.Random(7), plus=HOGWARTS.plus)
    assert sheet.fields["friends-and-rivals"] == ""
    assert sheet.fields["scars"] == ""


def test_the_given_names_pool_has_sixty_names_and_the_surnames_thirty():
    names = SPEC.templates["gryffindor"].names
    assert len(names) == 60
    assert "Dev" in names and "Wilhelmina" in names
    assert len(_values("surnames")) == 30


def test_the_statblock_shows_the_house_blood_and_creation_fields():
    sheet = resolve(
        SPEC,
        {"stats": EVEN_STATS, "templates": {"house": "slytherin", "blood": "muggle-born"}},
        random.Random(3),
        plus=HOGWARTS.plus,
    )
    text = describe(SPEC, sheet)
    assert "House: Slytherin" in text
    assert "Blood: Muggle-born" in text
    assert "Experience: 3" in text
    assert "Surname:" in text


def test_every_spell_and_potion_is_a_spell_feature_with_its_year():
    spells = {
        feature_id: feature
        for feature_id, feature in SPEC.features.items()
        if feature.feature_type == "spell"
    }
    assert len(spells) == 126
    assert "(Year 1)" in spells["lumos"].description
    assert "Potion (Year 6)" in spells["veritaserum"].description
    assert "(Year 5)" in spells["expecto-patronum"].description


def test_unforgivable_curses_cannot_be_chosen_at_creation():
    schema = json.dumps(character_schema(SPEC))
    for name in ("avada-kedavra", "crucio", "imperio"):
        assert f'"{name}"' not in schema
