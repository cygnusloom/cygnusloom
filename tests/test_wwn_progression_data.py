from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cygnusloom.content.character import (
    CharacterSpec,
    CounterRef,
    SkillProgression,
    Stat,
    Template,
    TemplateRequirement,
)
from cygnusloom.content.definitions import ContentError, read_definitions
from cygnusloom.rules.wwn_progression import (
    ClassPairing,
    ClassTable,
    WWNProgression,
    combine_class_tables,
    configure_progression,
    parse_wwn_progression,
    require_wwn_progression,
)

PROGRESSION_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "cygnusloom"
    / "data"
    / "rulesets"
    / "worldswithoutnumber"
    / "progression.json"
)


@pytest.fixture(scope="module")
def progression_definition() -> dict[str, Any]:
    definitions = json.loads(PROGRESSION_PATH.read_text(encoding="utf-8"))
    assert isinstance(definitions, list)
    assert len(definitions) == 2
    modules = [item for item in definitions if item.get("type") == "python_module"]
    assert modules == [
        {
            "type": "python_module",
            "id": "wwn_progression",
            "module": "cygnusloom.rules.wwn_progression",
        }
    ]
    rule_data = [item for item in definitions if item.get("type") == "rule_data"]
    assert len(rule_data) == 1
    return rule_data[0]


def _character_spec(
    progression_definition: dict[str, Any],
    *,
    skill_maximum: int = 4,
    level_maximum: int = 10,
) -> CharacterSpec:
    definitions = [
        definition.data
        for definition in read_definitions(PROGRESSION_PATH.parent)
        if definition.type == "template"
    ]
    templates = {
        definition["id"]: Template(
            id=definition["id"],
            kind=definition["kind"],
            name=definition["name"],
            description=definition.get("description", ""),
            features=(),
            tables=(),
            names=(),
            stats=definition.get("stats", {}),
            compatible_with={
                kind: tuple(values)
                for kind, values in definition.get("compatible_with", {}).items()
            },
            template_requirements=tuple(
                TemplateRequirement(
                    kinds=tuple(requirement["kinds"]),
                    distinct=requirement.get("distinct", False),
                    missing_message=requirement.get("missing_message"),
                    duplicate_message=requirement.get("duplicate_message"),
                )
                for requirement in definition.get("template_requirements", [])
            ),
            choice_key=definition.get("choice_key", definition["id"]),
        )
        for definition in definitions
        if definition.get("type") == "template"
    }
    stats = {
        stat_id: Stat(
            id=stat_id,
            name=stat_id,
            formula=None,
            swap_group=None,
            low=0,
            high=0,
        )
        for stat_id in progression_definition["traditions"]["effort_stats"].values()
    }
    # The creation stat the class templates modify; its lowest value is the chart's level-1 base.
    stats["attack_bonus"] = Stat(
        id="attack_bonus", name="Attack Bonus", formula=None, swap_group=None, low=1, high=1
    )
    return CharacterSpec(
        stats=stats,
        templates=templates,
        features={},
        items={},
        fields={},
        tables={},
        table_names={},
        counters={
            "level": CounterRef(
                id="level",
                label="Level",
                initial=1,
                minimum=1,
                maximum=level_maximum,
            )
        },
        skill_progression=SkillProgression(
            untrained=-1,
            first_level=0,
            creation_maximum=1,
            maximum=skill_maximum,
        ),
    )


def test_parser_uses_the_supplied_mapping_and_returns_immutable_data(
    progression_definition: dict[str, Any],
) -> None:
    source = copy.deepcopy(progression_definition)
    original = parse_wwn_progression(source, "synthetic/original")
    altered_data = copy.deepcopy(source)
    altered_data["xp"]["fast"][1] = 4
    altered_data["classes"][0]["attack"][0] = 8
    altered = parse_wwn_progression(altered_data, "synthetic/altered")

    assert original.xp["fast"][1] == 3
    assert altered.xp["fast"][1] == 4
    assert altered.class_tables["warrior"].attack[0] == 8
    with pytest.raises(TypeError):
        altered.class_tables["warrior"] = altered.class_tables["expert"]
    with pytest.raises(TypeError):
        altered.casting_tables["full_mage"][1] = altered.casting_tables["full_mage"][2]
    with pytest.raises(TypeError):
        altered.casting_tables["full_mage"].rows[1] = altered.casting_tables["full_mage"][2]


def test_loader_adapter_delegates_to_the_pure_parser(
    progression_definition: dict[str, Any],
    character_spec: CharacterSpec,
) -> None:
    configured = configure_progression(progression_definition, character_spec, "synthetic/adapter")

    assert isinstance(configured, WWNProgression)
    assert configured.xp["fast"] == (0, 3, 6, 12, 18, 27, 39, 54, 72, 93)


def test_class_tables_match_the_ruleset_class_templates(
    progression_definition: dict[str, Any],
    character_spec: CharacterSpec,
) -> None:
    progression = parse_wwn_progression(progression_definition, "synthetic/classes")
    class_ids = {
        template.id
        for template in character_spec.templates.values()
        if template.kind in {"class", "class2"}
    }

    assert set(progression.class_tables) == class_ids


def test_class_table_without_a_class_template_is_rejected(
    progression_definition: dict[str, Any],
    character_spec: CharacterSpec,
) -> None:
    malformed = copy.deepcopy(progression_definition)
    extra = copy.deepcopy(malformed["classes"][0])
    extra["id"] = "unavailable-class"
    malformed["classes"].append(extra)

    with pytest.raises(
        ContentError, match="class tables have no class template.*unavailable-class"
    ):
        configure_progression(malformed, character_spec, "synthetic/unknown-class")


def test_runtime_resolver_requires_a_valid_progression(
    progression_definition: dict[str, Any],
) -> None:
    progression = parse_wwn_progression(progression_definition, "synthetic/runtime")
    assert (
        require_wwn_progression(SimpleNamespace(rule_data={"progression": progression}))
        is progression
    )

    with pytest.raises(ContentError, match="no rule_data mapping"):
        require_wwn_progression(object())
    with pytest.raises(ContentError, match="rule_data must be a mapping"):
        require_wwn_progression(SimpleNamespace(rule_data=[]))
    with pytest.raises(ContentError, match=r"missing rule_data\['progression'\]"):
        require_wwn_progression(SimpleNamespace(rule_data={}))
    with pytest.raises(ContentError, match=r"must be a WWNProgression"):
        require_wwn_progression(SimpleNamespace(rule_data={"progression": {}}))


@pytest.fixture(scope="module")
def character_spec(progression_definition: dict[str, Any]) -> CharacterSpec:
    return _character_spec(progression_definition)


def test_missing_second_tradition_mapping_is_rejected(
    progression_definition: dict[str, Any], character_spec: CharacterSpec
) -> None:
    malformed = copy.deepcopy(progression_definition)
    malformed["traditions"]["tags"].pop("high_mage_partial_2")

    with pytest.raises(ContentError) as error:
        configure_progression(malformed, character_spec, "synthetic/missing-tradition2")

    assert "synthetic/missing-tradition2" in str(error.value)
    assert "high_mage_partial_2" in str(error.value)


def test_unknown_effort_stat_is_rejected(
    progression_definition: dict[str, Any], character_spec: CharacterSpec
) -> None:
    malformed = copy.deepcopy(progression_definition)
    malformed["traditions"]["effort_stats"]["high"] = "missing_effort"

    with pytest.raises(ContentError) as error:
        configure_progression(malformed, character_spec, "synthetic/missing-effort")

    assert "synthetic/missing-effort" in str(error.value)
    assert "missing_effort" in str(error.value)


def test_skill_chart_must_match_character_skill_progression(
    progression_definition: dict[str, Any],
) -> None:
    character = _character_spec(progression_definition, skill_maximum=3)

    with pytest.raises(ContentError, match="skill chart bounds 0..4"):
        configure_progression(progression_definition, character, "synthetic/skill-bounds")


def test_level_chart_must_match_configured_level_counter(
    progression_definition: dict[str, Any],
) -> None:
    character = _character_spec(progression_definition, level_maximum=9)

    with pytest.raises(ContentError, match="level counter maximum 9"):
        configure_progression(progression_definition, character, "synthetic/level-bound")


def test_combining_class_charts_matches_the_shipped_adventurer_tables(
    progression_definition: dict[str, Any],
) -> None:
    progression = parse_wwn_progression(progression_definition, "synthetic/pairing")
    tables = progression.class_tables
    policy = progression.class_pairing

    for first, second, combined in (
        ("expert_partial_2", "warrior_partial_2", "adventurer_expert_warrior"),
        ("expert_partial_2", "mage_partial_2", "adventurer_expert_mage"),
        ("warrior_partial_2", "mage_partial_2", "adventurer_warrior_mage"),
        ("mage_partial_2", "mage_partial_2", "adventurer_mage_mage"),
    ):
        assert combine_class_tables([tables[first], tables[second]], policy) == tables[combined]

    healer_and_warrior = combine_class_tables(
        [tables["healer"], tables["warrior_partial_2"]], policy
    )
    assert healer_and_warrior.hp_constant == 2
    assert healer_and_warrior.attack == (1, 2, 2, 3, 4, 5, 5, 6, 6, 7)
    assert healer_and_warrior.quick_learner is False


def test_every_class_and_class2_pairing_starts_at_the_chart_attack(
    progression_definition: dict[str, Any],
    character_spec: CharacterSpec,
) -> None:
    progression = configure_progression(
        progression_definition, character_spec, "synthetic/attack-pairings"
    )
    base = character_spec.stats["attack_bonus"].low
    for class_id, table in progression.class_tables.items():
        template = character_spec.templates[class_id]
        # A second partial class only exists in a pairing, so its own chart is checked below.
        if template.kind != "class":
            continue
        assert table.attack[0] == base + template.stats.get("attack_bonus", 0), class_id

    primaries = [
        template
        for template in character_spec.templates.values()
        if template.kind == "class" and template.id in progression.class_tables
    ]
    seconds = [
        template
        for template in character_spec.templates.values()
        if template.kind == "class2" and template.id in progression.class_tables
    ]
    pairs = 0
    for primary in primaries:
        for second in seconds:
            allowed = second.compatible_with.get("class")
            if allowed is not None and primary.id not in allowed:
                continue
            required = primary.compatible_with.get("class2")
            if required is not None and second.id not in required:
                continue
            if any(
                requirement.distinct
                and set(requirement.kinds) == {"class", "class2"}
                and primary.choice_key == second.choice_key
                for requirement in (*primary.template_requirements, *second.template_requirements)
            ):
                continue
            combined = combine_class_tables(
                [progression.class_tables[primary.id], progression.class_tables[second.id]],
                progression.class_pairing,
            )
            actual = (
                base + primary.stats.get("attack_bonus", 0) + second.stats.get("attack_bonus", 0)
            )
            assert combined.attack[0] == actual, (primary.id, second.id)
            pairs += 1
    assert pairs


def test_combining_class_charts_rejects_an_unknown_policy() -> None:
    chart = ClassTable(6, 0, (0, 1), frozenset({2}), False)
    policy = ClassPairing(
        hit_die="best",
        hp_constant="best",
        attack="best",
        focus_levels="first",
        quick_learner="any",
    )

    with pytest.raises(ContentError, match="focus_levels policy 'first' is not supported"):
        combine_class_tables([chart, chart], policy)


def _drop_class_pairing(data: dict[str, Any]) -> None:
    data.pop("class_pairing")


def _add_unknown_pairing_field(data: dict[str, Any]) -> None:
    data["class_pairing"]["skills"] = "best"


def _set_unknown_pairing_policy(data: dict[str, Any]) -> None:
    data["class_pairing"]["hp_constant"] = "worst"


def _shorten_class_pairing(data: dict[str, Any]) -> None:
    data["class_pairing"].pop("attack")


@pytest.mark.parametrize(
    "change",
    [
        _drop_class_pairing,
        _add_unknown_pairing_field,
        _set_unknown_pairing_policy,
        _shorten_class_pairing,
    ],
)
def test_malformed_class_pairing_is_rejected(
    progression_definition: dict[str, Any],
    change: Callable[[dict[str, Any]], None],
) -> None:
    malformed = copy.deepcopy(progression_definition)
    change(malformed)

    with pytest.raises(ContentError, match="synthetic/progression"):
        parse_wwn_progression(malformed, "synthetic/progression")


def _drop_xp(data: dict[str, Any]) -> None:
    data.pop("xp")


def _set_bool_hit_die(data: dict[str, Any]) -> None:
    data["classes"][0]["hit_die"] = True


def _shorten_attack(data: dict[str, Any]) -> None:
    data["classes"][0]["attack"].pop()


def _shorten_xp_track(data: dict[str, Any]) -> None:
    data["xp"]["slow"].pop()


def _misnumber_casting_row(data: dict[str, Any]) -> None:
    data["casting"]["full_mage"]["rows"][0]["level"] = 2


def _reference_unknown_class(data: dict[str, Any]) -> None:
    data["casting_classes"]["missing_class"] = "partial_mage"


def _reference_unknown_casting_table(data: dict[str, Any]) -> None:
    data["casting_classes"]["mage"] = "missing_table"


def _reference_unknown_tradition(data: dict[str, Any]) -> None:
    data["traditions"]["tags"]["high_mage"] = "missing_tradition"


def _add_unknown_art_tag(data: dict[str, Any]) -> None:
    data["art_levels"]["partial"]["missing_tradition"] = [1]


def _set_bool_skill_prerequisite(data: dict[str, Any]) -> None:
    data["skill_minimums"]["4"] = True


def _add_unknown_definition_field(data: dict[str, Any]) -> None:
    data["xp_tracks"] = {}


def _add_unknown_class_field(data: dict[str, Any]) -> None:
    data["classes"][0]["attack_bonus"] = []


def _add_unknown_casting_field(data: dict[str, Any]) -> None:
    data["casting"]["full_mage"]["rows"][0]["casts_per_day"] = 1


@pytest.mark.parametrize(
    "change",
    [
        _drop_xp,
        _set_bool_hit_die,
        _shorten_attack,
        _shorten_xp_track,
        _misnumber_casting_row,
        _reference_unknown_class,
        _reference_unknown_casting_table,
        _reference_unknown_tradition,
        _add_unknown_art_tag,
        _set_bool_skill_prerequisite,
        _add_unknown_definition_field,
        _add_unknown_class_field,
        _add_unknown_casting_field,
    ],
)
def test_malformed_and_inconsistent_progression_is_rejected(
    progression_definition: dict[str, Any],
    change: Callable[[dict[str, Any]], None],
) -> None:
    malformed = copy.deepcopy(progression_definition)
    change(malformed)

    with pytest.raises(ContentError, match="synthetic/progression"):
        parse_wwn_progression(malformed, "synthetic/progression")


def test_definition_identity_and_mapping_shape_are_validated(
    progression_definition: dict[str, Any],
) -> None:
    wrong_id = copy.deepcopy(progression_definition)
    wrong_id["id"] = "combat"
    with pytest.raises(ContentError, match="id must be 'progression'"):
        parse_wwn_progression(wrong_id, "synthetic/progression")
    with pytest.raises(ContentError, match="must be an object"):
        parse_wwn_progression([], "synthetic/progression")  # type: ignore[arg-type]
