from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from cygnusforge.content.character import CharacterError, build_character, check_choices, resolve
from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import ContentLibrary
from tests.test_content import _core, _world


def _template(ident: str, kind: str, **data: Any) -> Definition:
    return Definition(
        "template",
        ident,
        {
            "type": "template",
            "id": ident,
            "kind": kind,
            "name": ident,
            "description": ident,
            **data,
        },
    )


def _templates() -> list[Definition]:
    return [
        _template(
            "dual",
            "role",
            template_requirements=[
                {
                    "kinds": ["first_domain", "second_domain"],
                    "distinct": True,
                    "missing_message": "choose two domains",
                    "duplicate_message": "choose different domains",
                }
            ],
        ),
        _template("single", "role"),
        _template(
            "fire_first",
            "first_domain",
            choice_key="fire",
            compatible_with={"role": ["dual"]},
        ),
        _template(
            "fire_second",
            "second_domain",
            choice_key="fire",
            compatible_with={"role": ["dual"]},
        ),
        _template(
            "water_second",
            "second_domain",
            choice_key="water",
            compatible_with={"role": ["dual"]},
        ),
    ]


def test_template_constraints_work_for_arbitrary_kinds():
    spec = build_character(_templates(), {}, {}, "test")
    check_choices(
        spec,
        {
            "templates": {
                "role": "dual",
                "first_domain": "fire_first",
                "second_domain": "water_second",
            }
        },
    )
    check_choices(spec, {"templates": {"role": "single"}})

    with pytest.raises(CharacterError, match="choose two domains"):
        check_choices(spec, {"templates": {"role": "dual", "first_domain": "fire_first"}})
    with pytest.raises(CharacterError, match="choose different domains"):
        check_choices(
            spec,
            {
                "templates": {
                    "role": "dual",
                    "first_domain": "fire_first",
                    "second_domain": "fire_second",
                }
            },
        )
    with pytest.raises(CharacterError, match="cannot be chosen"):
        check_choices(spec, {"templates": {"role": "single", "first_domain": "fire_first"}})


@pytest.mark.parametrize(
    "overrides",
    [
        {"compatible_with": {"missing": ["dual"]}},
        {"compatible_with": {"role": ["unknown"]}},
        {"compatible_with": {"role": ["fire_first"]}},
        {"compatible_with": {"role": []}},
        {"template_requirements": [{"kinds": ["first_domain", "missing"]}]},
        {"template_requirements": [{"kinds": ["first_domain", "first_domain"]}]},
        {"template_requirements": [{"kinds": ["first_domain"], "distinct": True}]},
        {"template_requirements": [{"kinds": ["first_domain"], "distinct": "yes"}]},
        {"choice_key": ""},
        {"classes": ["dual"]},
    ],
)
def test_malformed_template_constraints_are_rejected(overrides: dict[str, Any]):
    definitions = _templates()
    definitions.append(_template("broken", "extra", **overrides))
    with pytest.raises(ContentError):
        build_character(definitions, {}, {}, "test")


def test_same_worlds_without_number_tradition_cannot_be_chosen_twice():
    spec = ContentLibrary.load().rule_sets["worldswithoutnumber"].character
    with pytest.raises(CharacterError, match="two different traditions"):
        check_choices(
            spec,
            {
                "templates": {
                    "class": "adventurer_mage_mage",
                    "tradition": "high_mage_partial",
                    "tradition2": "high_mage_partial_2",
                }
            },
        )


def test_unknown_template_is_reported_before_its_picks_are_checked():
    spec = build_character(_templates(), {}, {}, "test")
    with pytest.raises(CharacterError, match="there is no template"):
        check_choices(spec, {"templates": {"role": "missing"}})


def test_new_ruleset_uses_json_constraints_without_engine_changes(tmp_path: Path):
    files = {
        "worlds/testland/world.json": _world(),
        "rulesets/core/rule_set.json": _core(),
        "rulesets/other/rule_set.json": [
            {
                "type": "rule_set",
                "id": "other",
                "name": "Other",
                "description": "A different game.",
                "skill_progression": {
                    "untrained": -3,
                    "first_level": -1,
                    "creation_maximum": 2,
                    "maximum": 8,
                },
            }
        ],
        "rulesets/other/templates.json": [definition.data for definition in _templates()],
        "rulesets/other/skills.json": [{"type": "skill", "id": "craft", "name": "Craft"}],
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")

    spec = ContentLibrary.load(tmp_path).rule_sets["other"].character
    choices = {
        "templates": {
            "role": "dual",
            "first_domain": "fire_first",
            "second_domain": "water_second",
        }
    }
    check_choices(spec, choices)
    sheet = resolve(spec, choices, random.Random(0), plus="sum")
    assert sheet.templates == choices["templates"]
    assert spec.skill_level(sheet.skills, "craft") == -3
