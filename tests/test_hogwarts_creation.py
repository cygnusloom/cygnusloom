from __future__ import annotations

import json
import random

import pytest

from cygnusforge.content.character import (
    CharacterError,
    TableOption,
    build_character,
    character_schema,
    check_choices,
    choice_options,
    describe,
    resolve,
)
from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import _load_learn, _load_periods
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


def _knowledge_line(year: str) -> str:
    sheet = resolve(
        SPEC,
        {"stats": EVEN_STATS, "fields": {"year": year}},
        random.Random(3),
        plus=HOGWARTS.plus,
    )
    return next((line for line in describe(SPEC, sheet).splitlines() if "Knows" in line), "")


def test_a_second_year_knows_the_first_year_spells():
    assert _knowledge_line("2nd Year") == (
        "Knows every spell tagged with an earlier Year: 1st Year"
    )


def test_a_graduate_knows_the_spells_of_every_year():
    line = _knowledge_line("Graduate")
    assert line.startswith("Knows every spell tagged with an earlier Year: 1st Year, ")
    assert line.endswith("6th Year, 7th Year")


def test_a_first_year_knows_no_spells_yet():
    assert _knowledge_line("1st Year") == ""


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


CORE = {option.value for option in SPEC.tables["favourite-subject"] if option.group == "core"}
ELECTIVE = {
    option.value for option in SPEC.tables["favourite-subject"] if option.group == "elective"
}


@pytest.mark.parametrize("year", ["1st Year", "2nd Year"])
def test_first_and_second_years_roll_a_core_favourite_subject(year):
    for seed in range(60):
        sheet = resolve(
            SPEC,
            {"fields": {"year": year}},
            random.Random(seed),
            plus=HOGWARTS.plus,
        )
        assert sheet.fields["favourite-subject"] in CORE


@pytest.mark.parametrize("year", ["3rd Year", "7th Year", "Graduate"])
def test_later_years_roll_core_and_elective_favourite_subjects(year):
    picks = {
        resolve(SPEC, {"fields": {"year": year}}, random.Random(seed), plus=HOGWARTS.plus).fields[
            "favourite-subject"
        ]
        for seed in range(200)
    }
    assert picks & CORE and picks & ELECTIVE


def test_a_group_roll_picks_core_and_elective_equally_often():
    cores = sum(
        resolve(
            SPEC, {"fields": {"year": "3rd Year"}}, random.Random(seed), plus=HOGWARTS.plus
        ).fields["favourite-subject"]
        in CORE
        for seed in range(2000)
    )
    assert 900 <= cores <= 1100


def test_a_first_year_cannot_choose_an_elective_favourite_subject():
    with pytest.raises(CharacterError, match="cannot be 'favourite-subject'"):
        check_choices(SPEC, {"fields": {"year": "1st Year", "favourite-subject": "Astronomy"}})


def test_a_year_is_needed_to_rule_out_an_elective_favourite_subject():
    with pytest.raises(CharacterError, match="cannot be 'favourite-subject'"):
        check_choices(SPEC, {"fields": {"favourite-subject": "Astronomy"}})


def test_a_core_favourite_subject_is_allowed_whatever_the_year():
    check_choices(SPEC, {"fields": {"favourite-subject": "Potions"}})
    check_choices(SPEC, {"fields": {"year": "1st Year", "favourite-subject": "Potions"}})


def test_a_later_year_may_choose_an_elective_favourite_subject():
    check_choices(SPEC, {"fields": {"year": "3rd Year", "favourite-subject": "Quidditch"}})


def test_the_favourite_subject_choices_say_which_years_are_core_only():
    listing = choice_options(SPEC, "favourite-subject")
    assert {entry["group"] for entry in listing["options"]} == {"core", "elective"}
    assert "'1st Year'" in listing["note"] and "'core'" in listing["note"]
    assert "note" not in choice_options(SPEC, "year")


def test_a_restriction_that_names_no_field_is_refused_at_load():
    definitions = [
        Definition(
            "character_field",
            "subject",
            {
                "type": "character_field",
                "id": "subject",
                "name": "Subject",
                "table": "subjects",
                "restricted_by": {"field": "missing", "values": ["A"], "groups": ["core"]},
            },
        ),
    ]
    tables = {"subjects": (TableOption("A", "a", "core"),)}
    with pytest.raises(ContentError, match="needs a table field named 'missing'"):
        build_character(definitions, tables, {"subjects": "Subjects"}, "test")


SPELL_BLOOD = {"muggle-born": 1, "half-blood": 2, "pure-blood": 3}


def _spells_of(sheet) -> set[str]:
    return {feature for feature in sheet.features if SPEC.features[feature].feature_type == "spell"}


def _spell_for(year: str, skip: set[str] = frozenset()) -> str:
    return next(
        feature.id
        for feature in SPEC.features.values()
        if feature.feature_type == "spell" and feature.tag == year and feature.id not in skip
    )


@pytest.mark.parametrize("blood,count", SPELL_BLOOD.items())
def test_each_blood_status_rolls_its_spells_from_the_year(blood, count):
    for seed in range(20):
        sheet = resolve(
            SPEC,
            {"templates": {"blood": blood}, "fields": {"year": "3rd Year"}},
            random.Random(seed),
            plus=HOGWARTS.plus,
        )
        spells = _spells_of(sheet)
        assert len(spells) == count
        assert {SPEC.features[spell].tag for spell in spells} == {"3rd Year"}


def test_given_spell_picks_are_kept_and_the_rest_are_rolled():
    kept = _spell_for("3rd Year")
    sheet = resolve(
        SPEC,
        {
            "templates": {"blood": "pure-blood"},
            "fields": {"year": "3rd Year"},
            "features": [kept],
        },
        random.Random(3),
        plus=HOGWARTS.plus,
    )
    spells = _spells_of(sheet)
    assert len(spells) == 3 and kept in spells
    assert {SPEC.features[spell].tag for spell in spells} == {"3rd Year"}


def test_a_spell_from_another_year_is_refused():
    with pytest.raises(CharacterError, match="is not a spell for '3rd Year'"):
        check_choices(
            SPEC,
            {
                "templates": {"blood": "half-blood"},
                "fields": {"year": "3rd Year"},
                "features": [_spell_for("1st Year")],
            },
        )


def test_too_many_spell_picks_are_refused():
    picks = []
    while len(picks) < 3:
        picks.append(_spell_for("3rd Year", skip=set(picks)))
    with pytest.raises(CharacterError, match="picks 2 spells, not 3"):
        check_choices(
            SPEC,
            {
                "templates": {"blood": "half-blood"},
                "fields": {"year": "3rd Year"},
                "features": picks,
            },
        )


def test_spell_picks_without_a_year_are_refused():
    with pytest.raises(CharacterError, match="give 'year' to pick the spells"):
        check_choices(
            SPEC,
            {
                "templates": {"blood": "half-blood"},
                "features": [_spell_for("3rd Year")],
            },
        )


def test_a_graduate_with_a_blood_status_picks_no_spells():
    submitted = {"templates": {"blood": "pure-blood"}, "fields": {"year": "Graduate"}}
    check_choices(SPEC, submitted)
    sheet = resolve(SPEC, submitted, random.Random(5), plus=HOGWARTS.plus)
    assert _spells_of(sheet) == set()


def test_a_pick_tag_field_that_is_not_a_table_field_is_refused_at_load():
    definitions = [
        Definition(
            "template",
            "pure-blood",
            {
                "type": "template",
                "id": "pure-blood",
                "kind": "blood",
                "name": "Pure-blood",
                "description": "A test blood status.",
                "choose": {"count": 1, "feature_type": "spell", "tag_field": "missing"},
            },
        ),
    ]
    with pytest.raises(ContentError, match="needs a table field named 'missing'"):
        build_character(definitions, {}, {}, "test")


def test_a_knows_field_without_a_table_is_refused_at_load():
    definitions = [
        Definition(
            "character_field",
            "year",
            {"type": "character_field", "id": "year", "name": "Year", "knows": "spell"},
        ),
    ]
    with pytest.raises(ContentError, match="'knows' needs a table"):
        build_character(definitions, {}, {}, "test")


def test_a_knows_field_naming_no_feature_type_is_refused_at_load():
    definitions = [
        Definition(
            "character_field",
            "year",
            {
                "type": "character_field",
                "id": "year",
                "name": "Year",
                "table": "year",
                "knows": "potion",
            },
        ),
    ]
    tables = {"year": (TableOption("1st Year", "1st Year"),)}
    with pytest.raises(ContentError, match="knows 'potion', which is no feature type"):
        build_character(definitions, tables, {"year": "Year"}, "test")


def test_a_learn_rule_is_checked_against_the_feature_types_and_fields():
    with pytest.raises(ContentError, match="names no feature type"):
        _load_learn({"feature_type": "potion", "tag_field": "year"}, SPEC, "test")
    with pytest.raises(ContentError, match="needs a table field named 'missing'"):
        _load_learn({"feature_type": "spell", "tag_field": "missing"}, SPEC, "test")
    with pytest.raises(ContentError, match="'learn' must be an object"):
        _load_learn("spell", SPEC, "test")
    learn = _load_learn({"feature_type": "spell", "tag_field": "year"}, SPEC, "test")
    assert learn.feature_type == "spell" and learn.tag_field == "year"


def test_a_period_that_resets_an_unknown_tally_is_refused_at_load():
    definitions = [
        Definition(
            "period",
            "school-year",
            {
                "type": "period",
                "id": "school-year",
                "label": "school year",
                "tallies": ["nope"],
                "effects": [{"op": "note", "text": "The year ends."}],
            },
        ),
    ]
    with pytest.raises(ContentError, match="names no tally"):
        _load_periods(definitions, SPEC, HOGWARTS.counters, frozenset(), {}, "test")
