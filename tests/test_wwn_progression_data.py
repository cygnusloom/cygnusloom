from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from cygnusforge.content.character import (
    CharacterSpec,
    CounterRef,
    SkillProgression,
    Stat,
    Template,
)
from cygnusforge.content.definitions import ContentError
from cygnusforge.rules.wwn_progression import (
    CastingRow,
    ClassTable,
    WWNProgression,
    configure_progression,
    parse_wwn_progression,
    require_wwn_progression,
)

PROGRESSION_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "cygnusforge"
    / "data"
    / "rulesets"
    / "worldswithoutnumber"
    / "progression.json"
)
CLASSES_PATH = PROGRESSION_PATH.with_name("classes.json")


@pytest.fixture
def progression_definition() -> dict[str, Any]:
    definitions = json.loads(PROGRESSION_PATH.read_text(encoding="utf-8"))
    assert isinstance(definitions, list)
    assert len(definitions) == 2
    modules = [item for item in definitions if item.get("type") == "python_module"]
    assert modules == [
        {
            "type": "python_module",
            "id": "wwn_progression",
            "module": "cygnusforge.rules.wwn_progression",
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
    definitions = json.loads(CLASSES_PATH.read_text(encoding="utf-8"))
    templates = {
        definition["id"]: Template(
            id=definition["id"],
            kind=definition["kind"],
            name=definition["name"],
            description=definition.get("description", ""),
            features=(),
            tables=(),
            names=(),
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
    return CharacterSpec(
        stats=stats,
        templates=templates,
        features={},
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


def test_shipped_progression_matches_existing_wwn_values(
    progression_definition: dict[str, Any],
) -> None:
    progression = parse_wwn_progression(
        MappingProxyType(progression_definition), "worldswithoutnumber/progression.json[0]"
    )

    assert progression_definition["type"] == "rule_data"
    assert progression_definition["id"] == "progression"
    assert progression_definition["python_module"] == "wwn_progression"
    assert progression_definition["configure"] == "configure_progression"
    assert progression.max_level == 10
    assert progression.max_skill_level == 4
    assert progression.xp_fast == (0, 3, 6, 12, 18, 27, 39, 54, 72, 93)
    assert progression.xp_slow == (0, 6, 15, 24, 36, 51, 69, 87, 105, 139)
    assert dict(progression.skill_level_minimums) == {0: 1, 1: 1, 2: 3, 3: 6, 4: 9}
    assert set(progression.class_tables) == {
        "warrior",
        "expert",
        "mage",
        "healer",
        "vowed",
        "adventurer_expert_warrior",
        "adventurer_expert_mage",
        "adventurer_warrior_mage",
        "adventurer_mage_mage",
        "adventurer_partial_expert_partial_warrior",
        "adventurer_partial_expert_partial_mage",
        "adventurer_partial_mage_partial_warrior",
        "adventurer_partial_mage_partial_mage",
    }

    assert progression.class_tables["warrior"] == ClassTable(
        6, 2, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10), frozenset({2, 5, 7, 10}), False
    )
    assert progression.class_tables["expert"] == ClassTable(
        6, 0, (0, 1, 1, 2, 2, 3, 3, 4, 4, 5), frozenset({2, 5, 7, 10}), True
    )
    assert progression.class_tables["mage"].hp_constant == -1
    assert progression.class_tables["adventurer_expert_warrior"].attack == (
        1,
        2,
        2,
        3,
        4,
        5,
        5,
        6,
        6,
        7,
    )
    assert (
        progression.class_tables["adventurer_partial_mage_partial_warrior"].quick_learner is False
    )

    assert progression.full_mage_classes == frozenset({"mage"})
    assert progression.partial_mage_classes == frozenset(
        {
            "adventurer_expert_mage",
            "adventurer_warrior_mage",
            "adventurer_partial_expert_partial_mage",
            "adventurer_partial_mage_partial_warrior",
        }
    )
    assert progression.dual_mage_classes == frozenset(
        {"adventurer_mage_mage", "adventurer_partial_mage_partial_mage"}
    )
    assert progression.casting_tables["full_mage"][1] == CastingRow(1, 1, 3)
    assert progression.casting_tables["full_mage"][10] == CastingRow(5, 6, 12)
    assert progression.casting_tables["partial_mage"][5] == CastingRow(2, 2, 5)
    assert progression.casting_tables["partial_mage"][10] == CastingRow(3, 4, 9)
    assert progression.casting_tables["dual_mage"][4] == CastingRow(2, 2, 6)
    assert progression.casting_tables["dual_mage"][10] == CastingRow(4, 5, 15)
    assert all(len(chart) == progression.max_level for chart in progression.casting_tables.values())

    assert dict(progression.tradition_tags) == {
        "no_tradition": "no_tradition",
        "high": "high",
        "high_mage": "high",
        "high_mage_partial": "high",
        "high_mage_partial_2": "high",
        "necromancer": "necromancer",
        "necromancer_partial": "necromancer",
        "necromancer_partial_2": "necromancer",
        "elementalist": "elementalist",
        "elementalist_partial": "elementalist",
        "elementalist_partial_2": "elementalist",
        "healer": "healer",
        "vowed": "vowed",
    }
    assert dict(progression.tradition_effort_stats) == {
        "high": "effort_high",
        "necromancer": "effort_necromancer",
        "elementalist": "effort_elemental",
        "healer": "effort_healer",
        "vowed": "effort_vowed",
    }
    assert dict(progression.art_levels_full) == {
        "high": frozenset({2, 4, 6, 8, 10}),
        "necromancer": frozenset({2, 4, 6, 8, 10}),
        "elementalist": frozenset({2, 4, 6, 8, 10}),
    }
    assert dict(progression.art_levels_partial) == {
        "high": frozenset({2, 3, 6, 9}),
        "necromancer": frozenset({3, 6, 9}),
        "elementalist": frozenset({3, 6, 9}),
        "healer": frozenset({2, 4, 5, 6, 8, 10}),
        "vowed": frozenset({2, 4, 5, 6, 8, 10}),
    }


def test_parser_uses_the_supplied_mapping_and_returns_immutable_data(
    progression_definition: dict[str, Any],
) -> None:
    source = copy.deepcopy(progression_definition)
    original = parse_wwn_progression(source, "synthetic/original")
    altered_data = copy.deepcopy(source)
    altered_data["xp"]["fast"][1] = 4
    altered_data["classes"][0]["attack"][0] = 8
    altered = parse_wwn_progression(altered_data, "synthetic/altered")

    assert original.xp_fast[1] == 3
    assert altered.xp_fast[1] == 4
    assert altered.class_tables["warrior"].attack[0] == 8
    with pytest.raises(TypeError):
        altered.class_tables["warrior"] = altered.class_tables["expert"]
    with pytest.raises(TypeError):
        altered.casting_tables["full_mage"][1] = altered.casting_tables["full_mage"][2]


def test_loader_adapter_delegates_to_the_pure_parser(
    progression_definition: dict[str, Any],
    character_spec: CharacterSpec,
) -> None:
    configured = configure_progression(progression_definition, character_spec, "synthetic/adapter")

    assert isinstance(configured, WWNProgression)
    assert configured.xp_fast == (0, 3, 6, 12, 18, 27, 39, 54, 72, 93)


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


@pytest.fixture
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


def _drop_xp(data: dict[str, Any]) -> None:
    data.pop("xp")


def _set_bool_hit_die(data: dict[str, Any]) -> None:
    data["classes"][0]["hit_die"] = True


def _shorten_attack(data: dict[str, Any]) -> None:
    data["classes"][0]["attack"].pop()


def _shorten_xp_track(data: dict[str, Any]) -> None:
    data["xp"]["slow"].pop()


def _misnumber_casting_row(data: dict[str, Any]) -> None:
    data["casting"]["full_mage"][0]["level"] = 2


def _reference_unknown_class(data: dict[str, Any]) -> None:
    data["casting_classes"]["missing_class"] = "partial_mage"


def _reference_unknown_casting_table(data: dict[str, Any]) -> None:
    data["casting_classes"]["mage"] = "missing_table"


def _reference_unknown_tradition(data: dict[str, Any]) -> None:
    data["traditions"]["tags"]["high_mage"] = "missing_tradition"


def _add_unknown_art_tag(data: dict[str, Any]) -> None:
    data["art_levels"]["partial"]["missing_tradition"] = [1]


def _set_bool_skill_prerequisite(data: dict[str, Any]) -> None:
    data["skill_level_minimums"]["4"] = True


def _add_unknown_definition_field(data: dict[str, Any]) -> None:
    data["xp_tracks"] = {}


def _add_unknown_class_field(data: dict[str, Any]) -> None:
    data["classes"][0]["attack_bonus"] = []


def _add_unknown_casting_field(data: dict[str, Any]) -> None:
    data["casting"]["full_mage"][0]["casts_per_day"] = 1


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
