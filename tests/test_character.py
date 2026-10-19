from __future__ import annotations

import random

import pytest

from cygnusforge.content.character import (
    NO_CHARACTER,
    CharacterError,
    CharacterSpec,
    Sheet,
    character_schema,
    check_swaps,
    describe,
    resolve,
    roll_stats,
)
from cygnusforge.content.library import ContentLibrary
from cygnusforge.services.tools import check_arguments

LIBRARY = ContentLibrary.load()
SPEC = LIBRARY.rule_sets["cairn2e"].character


def _template_with(spec: CharacterSpec, attr: str):
    return next(t for t in spec.templates.values() if getattr(t, attr))


def test_roll_stats_stay_within_their_range():
    rolled = roll_stats(SPEC, random.Random(1))
    assert set(rolled) == set(SPEC.stats)
    for stat in SPEC.stats.values():
        assert stat.low <= rolled[stat.id] <= stat.high


def test_one_swap_within_a_group_is_allowed():
    rolled = {"str": 10, "dex": 12, "wil": 8, "hp": 3}
    chosen = {"str": 12, "dex": 10, "wil": 8, "hp": 3}
    check_swaps(SPEC, rolled, chosen)


def test_two_swaps_within_a_group_are_rejected():
    rolled = {"str": 10, "dex": 12, "wil": 8, "hp": 3}
    chosen = {"str": 8, "dex": 10, "wil": 12, "hp": 3}
    with pytest.raises(CharacterError, match="one swap"):
        check_swaps(SPEC, rolled, chosen)


def test_group_values_must_be_the_rolled_values():
    rolled = {"str": 10, "dex": 12, "wil": 8, "hp": 3}
    chosen = {"str": 11, "dex": 12, "wil": 8, "hp": 3}
    with pytest.raises(CharacterError, match="reordered"):
        check_swaps(SPEC, rolled, chosen)


def test_stats_outside_a_group_cannot_change():
    rolled = {"str": 10, "dex": 12, "wil": 8, "hp": 3}
    chosen = {"str": 10, "dex": 12, "wil": 8, "hp": 4}
    with pytest.raises(CharacterError, match="cannot be changed"):
        check_swaps(SPEC, rolled, chosen)


def test_resolve_rolls_missing_stats():
    sheet = resolve(SPEC, {}, random.Random(0))
    for stat in SPEC.stats.values():
        assert stat.low <= sheet.stats[stat.id] <= stat.high


def test_resolve_keeps_given_stats():
    given = {"str": 10, "dex": 11, "wil": 12, "hp": 3}
    sheet = resolve(SPEC, {"stats": given}, random.Random(0))
    assert sheet.stats == given


def test_resolve_adds_the_features_of_a_template():
    template = _template_with(SPEC, "features")
    sheet = resolve(SPEC, {"templates": {template.kind: template.id}}, random.Random(0))
    assert sheet.templates == {template.kind: template.id}
    for granted in template.features:
        assert granted.feature in sheet.features


def test_resolve_rolls_template_tables_and_blanks_text_fields():
    template = _template_with(SPEC, "tables")
    sheet = resolve(SPEC, {"templates": {template.kind: template.id}}, random.Random(0))
    for table in template.tables:
        assert sheet.fields[table] in SPEC.tables[table]
    for field in SPEC.fields.values():
        if field.table is None:
            assert sheet.fields[field.id] == ""


def test_resolve_keeps_a_chosen_table_value():
    field = next(f for f in SPEC.fields.values() if f.table)
    choice = SPEC.tables[field.table][-1]
    sheet = resolve(SPEC, {"fields": {field.id: choice}}, random.Random(0))
    assert sheet.fields[field.id] == choice


def test_schema_omits_groups_the_rule_set_does_not_define():
    fields_only = CharacterSpec(
        stats={},
        templates={},
        features={},
        fields=SPEC.fields,
        tables=SPEC.tables,
        table_names=SPEC.table_names,
    )
    schema = character_schema(fields_only)
    assert set(schema["properties"]) == {"fields"}
    assert schema["additionalProperties"] is False


def test_schema_accepts_an_empty_character():
    assert check_arguments(character_schema(SPEC), {}) is None


def test_schema_checks_nested_values():
    schema = character_schema(SPEC)
    assert check_arguments(schema, {"stats": {"str": 99, "dex": 10, "wil": 10, "hp": 3}})
    assert check_arguments(schema, {"stats": {"nope": 5}})
    assert check_arguments(schema, {"templates": {"no-such-kind": "x"}})


def test_no_character_schema_is_empty():
    schema = character_schema(NO_CHARACTER)
    assert schema["properties"] == {
        "fields": {"type": "object", "properties": {}, "additionalProperties": False}
    }


def test_describe_lists_each_part():
    template = _template_with(SPEC, "features")
    feature_id = template.features[0].feature
    sheet = Sheet(
        stats={"str": 10, "dex": 11, "wil": 12, "hp": 3},
        templates={template.kind: template.id},
        features={feature_id: 2},
        fields={},
    )
    text = describe(SPEC, sheet)
    assert "Strength 10" in text
    assert f"{template.kind.capitalize()}: {template.name}" in text
    assert f"{SPEC.features[feature_id].name} x2" in text


def test_describe_skips_blank_fields():
    field = next(f for f in SPEC.fields.values() if f.table is None)
    sheet = Sheet(stats={}, templates={}, features={}, fields={field.id: ""})
    assert field.name not in describe(SPEC, sheet)
