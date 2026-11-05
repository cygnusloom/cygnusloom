from __future__ import annotations

import random
from dataclasses import replace

import pytest
from starlette.datastructures import FormData

from cygnusloom.content.character import (
    CharacterError,
    TableOption,
    build_character,
    character_schema,
    resolve,
)
from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.services.tools import check_arguments
from cygnusloom.web.character_form import creation_view, submission_from_form


class FirstChoice(random.Random):
    def choice(self, seq):
        return seq[0]


def _spec(
    *,
    grant: int = 2,
    rolls: int = 1,
    effect: int = 0,
    skills: tuple[str, ...] = ("lore", "craft", "magic"),
    later: bool = False,
):
    definitions = [Definition("skill", skill, {"name": skill.title()}) for skill in skills]
    template = {
        "kind": "origin",
        "name": "Scholar",
        "description": "Training.",
        "skills": {"lore": grant} if grant else {},
        "skill_picks": [{"id": "early", "count": rolls, "table": "limited"}] if rolls else [],
    }
    if effect:
        template["features"] = [{"feature": "focus"}]
        definitions.append(
            Definition(
                "feature",
                "focus",
                {
                    "name": "Focus",
                    "feature_type": "feat",
                    "effects": [{"op": "skill", "id": "lore", "delta": effect}],
                },
            )
        )
    definitions.append(Definition("template", "scholar", template))
    if later:
        definitions.append(
            Definition(
                "template",
                "specialist",
                {
                    "kind": "role",
                    "name": "Specialist",
                    "description": "Further training.",
                    "skill_picks": [{"id": "later", "count": 2, "table": "all"}],
                },
            )
        )
    return build_character(
        definitions,
        {
            "limited": (TableOption(name=None, description="lore"),),
            "all": tuple(TableOption(name=None, description=skill) for skill in skills),
        },
        {},
        "replacement-test",
        skill_progression={
            "untrained": -1,
            "first_level": 0,
            "creation_maximum": 1,
            "maximum": 4,
            "creation_replacements": {
                "feature_exclude": ["magic"] if "magic" in skills else [],
            },
        },
    )


def _resolve(spec, **submitted):
    return resolve(
        spec, {"templates": {"origin": "scholar"}, **submitted}, FirstChoice(), plus="sum"
    )


def test_rolled_excess_can_choose_any_other_skill_including_magic():
    spec = _spec()
    submitted = {
        "templates": {"origin": "scholar"},
        "skill_replacements": {"roll": ["magic"]},
    }
    assert check_arguments(character_schema(spec), submitted) is None
    sheet = resolve(spec, submitted, FirstChoice(), plus="sum")
    assert sheet.skills == {"lore": 1, "magic": 0}


def test_rolled_replacement_exclusions_are_enforced():
    spec = _spec()
    progression = spec.skill_progression
    assert progression is not None and progression.creation_replacements is not None
    rules = replace(progression.creation_replacements, roll_exclude=("magic",))
    spec = replace(spec, skill_progression=replace(progression, creation_replacements=rules))
    with pytest.raises(CharacterError, match="not an eligible roll replacement"):
        _resolve(spec, skill_replacements={"roll": ["magic"]})


def test_blank_replacement_rolls_an_eligible_skill():
    sheet = _resolve(_spec(), skill_replacements={"roll": [""]})
    assert sheet.skills == {"lore": 1, "craft": 0}


def test_feature_excess_uses_ordered_player_choices_and_excludes_magic():
    spec = _spec(rolls=0, effect=2)
    sheet = _resolve(spec, skill_replacements={"feature": ["craft", "craft"]})
    assert sheet.skills == {"lore": 1, "craft": 1}
    with pytest.raises(CharacterError, match="not an eligible feature replacement"):
        _resolve(spec, skill_replacements={"feature": ["magic"]})


def test_each_copy_of_a_feature_runs_its_creation_skill_effect():
    spec = _spec(rolls=0, effect=1)
    template = spec.templates["scholar"]
    grant = replace(template.features[0], quantity="2")
    spec = replace(spec, templates={"scholar": replace(template, features=(grant,))})
    sheet = _resolve(spec, skill_replacements={"feature": ["craft", "craft"]})
    assert sheet.features["focus"] == 2
    assert sheet.skills == {"lore": 1, "craft": 1}


def test_an_excess_template_grant_is_not_silently_discarded():
    sheet = _resolve(_spec(grant=3, rolls=0), skill_replacements={"roll": ["craft"]})
    assert sheet.skills == {"lore": 1, "craft": 0}


def test_no_eligible_replacement_is_an_error():
    with pytest.raises(CharacterError, match="no eligible replacement skill"):
        _resolve(_spec(skills=("lore",)))


def test_no_eligible_feature_replacement_is_an_error_even_when_magic_is_available():
    with pytest.raises(CharacterError, match="no eligible replacement skill"):
        _resolve(_spec(rolls=0, effect=1, skills=("lore", "magic")))


def test_replacement_does_not_invalidate_later_manual_picks():
    spec = _spec(later=True)
    sheet = resolve(
        spec,
        {
            "templates": {"origin": "scholar", "role": "specialist"},
            "skill_picks": {"later": ["craft", "craft"]},
            "skill_replacements": {"roll": ["magic"]},
        },
        FirstChoice(),
        plus="sum",
    )
    assert sheet.skills == {"lore": 1, "magic": 0, "craft": 1}
    with pytest.raises(CharacterError, match="needed by a later pick"):
        resolve(
            spec,
            {
                "templates": {"origin": "scholar", "role": "specialist"},
                "skill_picks": {"later": ["craft", "craft"]},
                "skill_replacements": {"roll": ["craft"]},
            },
            FirstChoice(),
            plus="sum",
        )


def test_template_replacement_does_not_invalidate_later_manual_picks():
    spec = _spec(grant=3, rolls=0, later=True)
    sheet = resolve(
        spec,
        {
            "templates": {"origin": "scholar", "role": "specialist"},
            "skill_picks": {"later": ["craft", "craft"]},
            "skill_replacements": {"roll": ["magic"]},
        },
        FirstChoice(),
        plus="sum",
    )
    assert sheet.skills == {"lore": 1, "magic": 0, "craft": 1}


def test_replacement_cannot_choose_the_original_skill_when_a_pick_has_a_lower_cap():
    spec = _spec(grant=1)
    template = spec.templates["scholar"]
    low_pick = template.skill_picks[0]
    template = replace(template, skill_picks=(replace(low_pick, max_level=0),))
    spec = replace(spec, templates={"scholar": template})
    with pytest.raises(CharacterError, match="cannot replace"):
        _resolve(spec, skill_replacements={"roll": ["lore"]})


def test_rolled_replacement_respects_the_original_picks_cap():
    spec = _spec(grant=1)
    template = spec.templates["scholar"]
    pick = replace(template.skill_picks[0], max_level=0)
    template = replace(template, skills={"lore": 1, "craft": 1}, skill_picks=(pick,))
    spec = replace(spec, templates={"scholar": template})
    with pytest.raises(CharacterError, match="cannot replace"):
        _resolve(spec, skill_replacements={"roll": ["craft"]})
    assert _resolve(spec, skill_replacements={"roll": ["magic"]}).skills == {
        "lore": 0,
        "craft": 0,
        "magic": 0,
    }


@pytest.mark.parametrize(
    "replacement_rules",
    [{"feature_exclude": ["absent"]}, {"unexpected": []}, None],
)
def test_invalid_replacement_configuration_is_rejected(replacement_rules):
    levels = {
        "untrained": -1,
        "first_level": 0,
        "creation_maximum": 1,
        "maximum": 4,
        "creation_replacements": replacement_rules,
    }
    with pytest.raises(ContentError):
        build_character(
            [Definition("skill", "lore", {"name": "Lore"})],
            {},
            {},
            "invalid",
            skill_progression=levels,
        )


def test_creation_form_exposes_and_submits_replacement_preferences_in_order():
    spec = _spec(rolls=0, effect=2)
    view = creation_view(spec, {})
    groups = {group["kind"]: group for group in view["skill_replacements"]}
    assert {option["value"] for option in groups["roll"]["options"]} == {"lore", "craft", "magic"}
    assert {option["value"] for option in groups["feature"]["options"]} == {"lore", "craft"}
    assert groups["feature"]["excluded"] == ["Magic"]
    submitted = submission_from_form(
        spec,
        FormData(
            [
                ("template-origin", "scholar"),
                ("skill-replacement-feature", ""),
                ("skill-replacement-feature", "craft"),
            ]
        ),
    )
    assert submitted["skill_replacements"] == {"feature": ["", "craft"]}
    assert _resolve(spec, skill_replacements=submitted["skill_replacements"]).skills == {
        "lore": 1,
        "craft": 1,
    }
