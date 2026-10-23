from __future__ import annotations

import random

import pytest

from cygnusforge.content.character import (
    NO_CHARACTER,
    CharacterError,
    CharacterSpec,
    Field,
    Sheet,
    TableOption,
    character_schema,
    check_choices,
    check_swaps,
    choice_options,
    describe,
    resolve,
    roll_stats,
)
from cygnusforge.content.library import ContentLibrary
from cygnusforge.services.tools import check_arguments

LIBRARY = ContentLibrary.load()
SPEC = LIBRARY.rule_sets["cairn2e"].character
PLUS = LIBRARY.rule_sets["cairn2e"].plus


def _template_with(spec: CharacterSpec, attr: str):
    return next(t for t in spec.templates.values() if getattr(t, attr))


def test_roll_stats_stay_within_their_range():
    rolled = roll_stats(SPEC, random.Random(1), plus=PLUS)
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
    sheet = resolve(SPEC, {}, random.Random(0), plus=PLUS)
    for stat in SPEC.stats.values():
        assert stat.low <= sheet.stats[stat.id] <= stat.high


def test_resolve_keeps_given_stats():
    given = {"str": 10, "dex": 11, "wil": 12, "hp": 3}
    sheet = resolve(SPEC, {"stats": given}, random.Random(0), plus=PLUS)
    assert sheet.stats == given


def test_resolve_adds_the_features_of_a_template():
    template = _template_with(SPEC, "features")
    sheet = resolve(SPEC, {"templates": {template.kind: template.id}}, random.Random(0), plus=PLUS)
    assert sheet.templates == {template.kind: template.id}
    for granted in template.features:
        assert granted.feature in sheet.features


def test_resolve_rolls_template_tables_and_blanks_text_fields():
    template = _template_with(SPEC, "tables")
    sheet = resolve(SPEC, {"templates": {template.kind: template.id}}, random.Random(0), plus=PLUS)
    for table in template.tables:
        assert sheet.fields[table] in [option.value for option in SPEC.tables[table]]
    for field in SPEC.fields.values():
        if field.table is None:
            assert sheet.fields[field.id] == ""


def test_resolve_keeps_a_chosen_table_value():
    field = next(f for f in SPEC.fields.values() if f.table)
    choice = SPEC.tables[field.table][-1].value
    sheet = resolve(SPEC, {"fields": {field.id: choice}}, random.Random(0), plus=PLUS)
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


def _backgrounds():
    return [t for t in SPEC.templates.values() if t.kind == "background"]


def test_every_background_grants_one_backpack():
    backgrounds = _backgrounds()
    assert len(backgrounds) == 20
    for template in backgrounds:
        grants = [g.quantity for g in template.features if g.feature == "backpack"]
        assert grants == ["1"], template.id


def test_backpack_is_described_as_in_the_source():
    assert SPEC.features["backpack"].description == (
        "Can hold up to six slots of items or Fatigue."
    )


def test_petty_and_bulky_items_are_flagged():
    assert SPEC.features["badge"].size == "petty"
    assert SPEC.features["bow"].size == "bulky"
    assert SPEC.features["torch"].size == "normal"


def test_each_background_suggests_names_that_are_not_a_choice():
    for template in _backgrounds():
        names_table = f"{template.id}-names"
        assert template.names == tuple(option.value for option in SPEC.tables[names_table])
        assert names_table not in template.tables, template.id
        assert template.names, template.id


def test_suggested_names_are_not_in_the_character_schema():
    fields = character_schema(SPEC)["properties"]["fields"]["properties"]
    assert not any(field.endswith("-names") for field in fields)


def _named_work_spec(options: tuple[TableOption, ...]) -> CharacterSpec:
    return CharacterSpec(
        stats={},
        templates={},
        features={},
        fields={"work": Field(id="work", name="Work", description="What you did", table="work")},
        tables={"work": options},
        table_names={"work": "Work"},
    )


def test_schema_leaves_table_values_to_the_lookup():
    named = TableOption(name="The Wild Tongue", description="A bundle of scrolls.")
    plain = TableOption(name=None, description="Lilt")
    spec = _named_work_spec((named, plain))
    field = character_schema(spec)["properties"]["fields"]["properties"]["work"]
    assert field == {"type": "string", "description": "What you did"}


def test_choice_options_lists_table_fields_then_their_values():
    named = TableOption(name="The Wild Tongue", description="A bundle of scrolls.")
    spec = _named_work_spec((named, TableOption(name=None, description="Lilt")))
    assert choice_options(spec) == {"fields": [{"field": "work", "name": "What you did"}]}
    assert choice_options(spec, "work") == {
        "field": "work",
        "options": [
            {"value": "The Wild Tongue", "description": "A bundle of scrolls."},
            {"value": "Lilt"},
        ],
    }
    assert "error" in choice_options(spec, "nope")


def test_check_choices_accepts_listed_values_and_rejects_others():
    named = TableOption(name="The Wild Tongue", description="A bundle of scrolls.")
    spec = _named_work_spec((named, TableOption(name=None, description="Lilt")))
    check_choices(spec, {"work": "The Wild Tongue"})
    check_choices(spec, {"work": "Lilt"})
    check_choices(spec, {})
    with pytest.raises(CharacterError):
        check_choices(spec, {"work": "Nope"})
    with pytest.raises(CharacterError):
        check_choices(spec, {"work": "A bundle of scrolls."})


def test_resolve_stores_the_name_of_a_named_option():
    named = TableOption(name="The Wild Tongue", description="A bundle of scrolls.")
    spec = _named_work_spec((named,))
    sheet = resolve(spec, {"fields": {"work": "The Wild Tongue"}}, random.Random(0), plus=PLUS)
    assert sheet.fields["work"] == "The Wild Tongue"
    assert resolve(spec, {}, random.Random(0), plus=PLUS).fields["work"] == "The Wild Tongue"
