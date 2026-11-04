from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from sqlmodel import Session

from cygnusforge.content.character import (
    CharacterError,
    Feature,
    SkillProgression,
    TableOption,
    build_character,
    derived_stats,
    describe,
    resolve,
)
from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import ContentLibrary, _load_moves
from cygnusforge.rules import wwn_magic
from cygnusforge.services import moves
from cygnusforge.services.combat import _player_bonuses
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    Character,
    CheckMade,
    NpcAdded,
    SkillChanged,
    load_state,
    record,
)
from tests.factories import Rolls
from tests.test_wwn_advancement import _ctx
from tests.test_wwn_combat import PC, _events, _wwn_campaign

LEVELS = {"untrained": -2, "first_level": -2, "creation_maximum": 1, "maximum": 7}


def _definitions() -> list[Definition]:
    return [
        Definition("skill", "lore", {"type": "skill", "id": "lore", "name": "Lore"}),
        Definition(
            "template",
            "scholar",
            {
                "type": "template",
                "id": "scholar",
                "kind": "role",
                "name": "Scholar",
                "description": "A scholar.",
                "skills": {"lore": 1},
            },
        ),
        Definition(
            "template",
            "apprentice",
            {
                "type": "template",
                "id": "apprentice",
                "kind": "role",
                "name": "Apprentice",
                "description": "An apprentice.",
                "skill_picks": [{"id": "practice", "count": 6, "table": "skills"}],
            },
        ),
    ]


def _spec(levels: object = LEVELS):
    return build_character(
        _definitions(),
        {"skills": (TableOption(name=None, description="lore"),)},
        {"skills": "Skills"},
        "test",
        skill_progression=levels,
    )


def test_skill_levels_and_creation_cap_come_from_rule_data():
    spec = _spec()
    scholar = resolve(spec, {"templates": {"role": "scholar"}}, random.Random(0), plus="sum")
    apprentice = resolve(spec, {"templates": {"role": "apprentice"}}, random.Random(0), plus="sum")

    assert spec.skill_level({}, "lore") == -2
    assert scholar.skills == {"lore": -2}
    assert not spec.skill_trained({}, "lore")
    assert spec.skill_trained(scholar.skills, "lore")
    assert apprentice.skills == {"lore": 1}
    assert spec.templates["apprentice"].skill_picks[0].max_level == 1
    assert spec.templates["apprentice"].skill_picks[0].label == "Skills"


@pytest.mark.parametrize("label", [None, "", " ", 3])
def test_skill_pick_label_must_be_nonempty_text(label: object):
    definitions = _definitions()
    apprentice = definitions[2]
    definitions[2] = replace(
        apprentice,
        data={
            **apprentice.data,
            "skill_picks": [{"id": "practice", "count": 1, "table": "skills", "label": label}],
        },
    )
    with pytest.raises(ContentError, match="'label' must be nonempty text"):
        build_character(
            definitions,
            {"skills": (TableOption(name=None, description="lore"),)},
            {"skills": "Skills"},
            "test",
            skill_progression=LEVELS,
        )


def _derived_pick_definitions(*, count: int = 1, pick_id: str = "practice") -> list[Definition]:
    definitions = _definitions()
    apprentice = definitions[2]
    definitions[2] = replace(
        apprentice,
        data={
            **apprentice.data,
            "skill_picks": [
                {"id": "practice", "label": "Practice skill", "count": count, "table": "skills"}
            ],
        },
    )
    definitions.append(
        Definition(
            "stat",
            "insight",
            {
                "type": "stat",
                "id": "insight",
                "name": "Insight",
                "derived": {"base": 3, "plus_skill_picks": [pick_id]},
            },
        )
    )
    return definitions


def _derived_pick_spec(definitions: list[Definition]):
    return build_character(
        definitions,
        {"skills": (TableOption(name=None, description="lore"),)},
        {"skills": "Skills"},
        "test",
        skill_progression={"untrained": -1, "first_level": 0, "creation_maximum": 1, "maximum": 4},
    )


@pytest.mark.parametrize("chosen", [False, True])
def test_derived_stat_remembers_and_displays_a_selected_or_rolled_skill_pick(chosen: bool):
    spec = _derived_pick_spec(_derived_pick_definitions())
    submitted = {"templates": {"role": "apprentice"}}
    if chosen:
        submitted["skill_picks"] = {"practice": ["lore"]}
    sheet = resolve(spec, submitted, random.Random(0), plus="sum")

    assert sheet.fields["practice"] == "lore"
    assert sheet.stats["insight"] == 3
    assert "Practice skill: Lore" in describe(spec, sheet)
    assert (
        derived_stats(spec, {}, {}, {"lore": 1}, sheet.templates, fields=sheet.fields)["insight"]
        == 4
    )
    assert derived_stats(spec, {}, {}, {"lore": 1}, {"role": "scholar"})["insight"] == 3


def test_derived_stat_rejects_missing_invalid_or_inapplicable_saved_skill_picks():
    spec = _derived_pick_spec(_derived_pick_definitions())
    with pytest.raises(CharacterError, match="has no recorded choice"):
        derived_stats(spec, {}, {}, {}, {"role": "apprentice"}, fields={})
    with pytest.raises(CharacterError, match="not offered"):
        derived_stats(spec, {}, {}, {}, {"role": "apprentice"}, fields={"practice": "unknown"})
    with pytest.raises(CharacterError, match="not available"):
        derived_stats(spec, {}, {}, {}, {"role": "scholar"}, fields={"practice": "lore"})


def test_derived_skill_pick_cannot_silently_leave_a_roll_unfilled():
    definitions = _derived_pick_definitions()
    apprentice = definitions[2]
    definitions[2] = replace(apprentice, data={**apprentice.data, "skills": {"lore": 2}})
    spec = _derived_pick_spec(definitions)
    with pytest.raises(CharacterError, match="needs one recorded choice"):
        resolve(spec, {"templates": {"role": "apprentice"}}, random.Random(0), plus="sum")


def test_derived_skill_pick_remembers_a_capped_roll_not_its_replacement():
    definitions = _derived_pick_definitions()
    definitions.append(Definition("skill", "craft", {"name": "Craft"}))
    apprentice = definitions[2]
    definitions[2] = replace(apprentice, data={**apprentice.data, "skills": {"lore": 2}})
    spec = build_character(
        definitions,
        {"skills": (TableOption(name=None, description="lore"),)},
        {"skills": "Skills"},
        "test",
        skill_progression={
            "untrained": -1,
            "first_level": 0,
            "creation_maximum": 1,
            "maximum": 4,
            "creation_replacements": {"feature_exclude": []},
        },
    )
    sheet = resolve(
        spec,
        {
            "templates": {"role": "apprentice"},
            "skill_replacements": {"roll": ["craft"]},
        },
        random.Random(0),
        plus="sum",
    )

    assert sheet.fields["practice"] == "lore"
    assert sheet.skills == {"lore": 1, "craft": 0}
    assert sheet.stats["insight"] == 4


@pytest.mark.parametrize(
    ("count", "pick_id", "error"),
    [(1, "missing", "names no skill pick"), (2, "practice", "must choose one skill")],
)
def test_derived_skill_pick_references_are_validated(count: int, pick_id: str, error: str):
    with pytest.raises(ContentError, match=error):
        _derived_pick_spec(_derived_pick_definitions(count=count, pick_id=pick_id))


def test_derived_skill_pick_cannot_collide_with_a_character_field():
    definitions = _derived_pick_definitions()
    definitions.append(
        Definition(
            "character_field",
            "practice",
            {"type": "character_field", "id": "practice", "name": "Practice"},
        )
    )
    with pytest.raises(ContentError, match="clashes with a field"):
        _derived_pick_spec(definitions)


def test_derived_skill_pick_cannot_span_template_kinds():
    definitions = _derived_pick_definitions()
    apprentice = definitions[2]
    definitions.append(
        replace(apprentice, id="other", data={**apprentice.data, "id": "other", "kind": "origin"})
    )
    with pytest.raises(ContentError, match="spans template kinds"):
        _derived_pick_spec(definitions)


def test_submitted_and_random_skill_picks_obey_the_same_pick_cap():
    definitions = _definitions()
    apprentice = definitions[2]
    definitions[2] = replace(
        apprentice,
        data={
            **apprentice.data,
            "skill_picks": [{"id": "practice", "count": 6, "table": "skills", "max_level": -1}],
        },
    )
    spec = build_character(
        definitions,
        {"skills": (TableOption(name=None, description="lore"),)},
        {"skills": "Skills"},
        "test",
        skill_progression=LEVELS,
    )
    submitted = {"templates": {"role": "apprentice"}}

    rolled = resolve(spec, submitted, random.Random(0), plus="sum")
    assert rolled.skills == {"lore": -1}
    with pytest.raises(CharacterError, match="cannot raise 'lore' above level -1"):
        resolve(
            spec,
            {**submitted, "skill_picks": {"practice": ["lore", "lore", "lore"]}},
            random.Random(0),
            plus="sum",
        )


def _mixed_pick_spec(
    *, early_count: int, early_max: int, later_max: int, granted: int, skills: tuple[str, ...]
):
    definitions = [
        Definition("skill", skill, {"type": "skill", "id": skill, "name": skill.title()})
        for skill in skills
    ]
    definitions += [
        Definition(
            "template",
            "background",
            {
                "type": "template",
                "id": "background",
                "kind": "origin",
                "name": "Background",
                "description": "Training.",
                "skills": {"lore": granted} if granted else {},
                "skill_picks": [
                    {"id": "early", "count": early_count, "table": "skills", "max_level": early_max}
                ],
            },
        ),
        Definition(
            "template",
            "profession",
            {
                "type": "template",
                "id": "profession",
                "kind": "role",
                "name": "Profession",
                "description": "Further training.",
                "skill_picks": [
                    {"id": "later", "count": 1, "table": "skills", "max_level": later_max}
                ],
            },
        ),
    ]
    return build_character(
        definitions,
        {"skills": tuple(TableOption(name=None, description=skill) for skill in skills)},
        {"skills": "Skills"},
        "mixed-picks",
        skill_progression={
            "untrained": -1,
            "first_level": 0,
            "creation_maximum": 1,
            "maximum": 4,
        },
    )


class FirstChoice(random.Random):
    def choice(self, seq):
        return seq[0]


def test_zero_skill_grants_are_rejected_instead_of_silently_discarded():
    definitions = _definitions()
    scholar = definitions[1]
    definitions[1] = replace(scholar, data={**scholar.data, "skills": {"lore": 0}})
    with pytest.raises(ContentError, match="skills.*at least 1"):
        build_character(definitions, {}, {}, "test", skill_progression=LEVELS)


def test_random_skill_picks_preserve_later_manual_choices():
    spec = _mixed_pick_spec(
        early_count=2, early_max=1, later_max=1, granted=1, skills=("lore", "craft")
    )
    sheet = resolve(
        spec,
        {
            "templates": {"origin": "background", "role": "profession"},
            "skill_picks": {"later": ["lore"]},
        },
        FirstChoice(),
        plus="sum",
    )

    assert sheet.skills == {"lore": 1, "craft": 1}


def test_earlier_low_cap_roll_can_train_a_skill_before_a_later_higher_cap_choice():
    spec = _mixed_pick_spec(early_count=1, early_max=0, later_max=1, granted=0, skills=("lore",))
    sheet = resolve(
        spec,
        {
            "templates": {"origin": "background", "role": "profession"},
            "skill_picks": {"later": ["lore"]},
        },
        FirstChoice(),
        plus="sum",
    )

    assert sheet.skills == {"lore": 1}


def test_blank_slots_in_a_partially_chosen_skill_pick_are_rolled():
    spec = _mixed_pick_spec(
        early_count=2, early_max=0, later_max=1, granted=0, skills=("lore", "craft")
    )
    sheet = resolve(
        spec,
        {"templates": {"origin": "background"}, "skill_picks": {"early": ["lore"]}},
        FirstChoice(),
        plus="sum",
    )

    assert sheet.skills == {"lore": 0, "craft": 0}


def test_invalid_skill_caps_are_reported_for_npcs_and_hirelings(client_factory):
    _, campaign_id, tools = _wwn_campaign(client_factory)
    tools.create_location("Yard", "A quiet yard.")
    tools.move_actor(PC, "Yard")
    invalid = {
        "templates": {"background": "hunter", "class": "warrior", "tradition": "no_tradition"},
        "skill_picks": {
            "background_learning": ["notice", "notice"],
            "class_free_skill": ["notice"],
        },
    }

    with pytest.raises(InvalidInput, match="cannot raise 'notice' above level 1"):
        tools.create_npc("Scout", "Yard", "A scout.", character=invalid)
    with pytest.raises(InvalidInput, match="cannot raise 'notice' above level 1"):
        tools.hire_hireling("Guide", PC, 1, "A guide.", character=invalid)

    with Session(tools.engine) as db:
        record(
            db,
            campaign_id,
            NpcAdded(
                name="Tale",
                location="Yard",
                description="A traveller.",
                personality="",
                accent="",
                quirks=[],
                story_only=True,
            ),
        )
        db.commit()
    with pytest.raises(InvalidInput, match="cannot raise 'notice' above level 1"):
        tools.update_npc("Tale", character=invalid)

    with Session(tools.engine) as db:
        state = load_state(db, campaign_id)
        assert state.named("Scout") is None
        assert state.named("Guide") is None
        tale = state.named("Tale")
        assert isinstance(tale, Character) and tale.story_only


@pytest.mark.parametrize(
    "levels",
    [
        None,
        {},
        {"untrained": True, "first_level": 0, "creation_maximum": 1, "maximum": 7},
        {"untrained": -2, "first_level": 2, "creation_maximum": 1, "maximum": 7},
        {"untrained": -2, "first_level": 0, "creation_maximum": 8, "maximum": 7},
        {**LEVELS, "unexpected": 3},
    ],
)
def test_invalid_skill_progression_is_rejected(levels: Any):
    with pytest.raises(ContentError):
        _spec(levels)


def test_bundled_skill_progression_is_defined_by_its_ruleset():
    progression = ContentLibrary.load().rule_sets["worldswithoutnumber"].character.skill_progression
    assert progression is not None
    assert (
        progression.untrained,
        progression.first_level,
        progression.creation_maximum,
        progression.maximum,
    ) == (-1, 0, 1, 4)


def test_combat_distinguishes_training_from_a_negative_skill_level():
    spec = _spec()
    combat = ContentLibrary.load().rule_sets["worldswithoutnumber"].combat
    assert combat is not None
    combat = replace(combat, attack=replace(combat.attack, unskilled_attack_penalty=3))
    feature = Feature(
        id="wand",
        name="Wand",
        feature_type="item",
        description="A wand.",
        size="normal",
        uses=0,
        armor=0,
        skill="lore",
    )
    character = Character("pc", "Rin", None, "", "", "", [])

    assert _player_bonuses(combat, character, "Wand", spec, feature).to_hit == -3
    character.skills["lore"] = -2
    assert _player_bonuses(combat, character, "Wand", spec, feature).to_hit == -2


def test_checks_and_advancement_use_configured_skill_limits(client_factory):
    client, campaign_id, original_tools = _wwn_campaign(client_factory)
    app = client.app
    assert isinstance(app, FastAPI)
    spec = replace(
        original_tools.character,
        skill_progression=SkillProgression(**LEVELS),
    )
    tools = replace(original_tools, character=spec)
    with Session(tools.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    skill = next(ident for ident in spec.skills if ident not in pc.skills)

    assert tools.roll_check("skill", PC, skill=skill)["bonus"] == -1
    (check,) = _events(app, campaign_id, CheckMade.kind)
    assert (check.skill_level, check.skill_bonus) == (-2, -1)
    assert f"{spec.skills[skill].name} -1" in check.render()
    tools.raise_skill(PC, skill, -2, cause="training")
    (changed,) = _events(app, campaign_id, SkillChanged.kind)
    assert "becomes trained" in changed.render()
    assert tools.roll_check("skill", PC, skill=skill)["bonus"] == -2
    tools.raise_skill(PC, skill, 7, cause="training")
    assert tools.roll_check("skill", PC, skill=skill)["bonus"] == 7
    with pytest.raises(InvalidInput, match="from -2 to 7"):
        tools.raise_skill(PC, skill, 8, cause="training")


def test_a_check_adds_its_skill_stat_and_situational_modifier(client_factory):
    _, campaign_id, original_tools = _wwn_campaign(client_factory)
    spec = replace(original_tools.character, skill_progression=SkillProgression(**LEVELS))
    tools = replace(original_tools, character=spec, rng=Rolls(3, 4))
    with Session(tools.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    skill = next(ident for ident in spec.skills if ident not in pc.skills)
    stat_bonus = spec.modifier(pc.stats, "dex")

    result = tools.roll_check("skill", PC, skill=skill, stat="dex", difficulty=8, modifier=2)

    assert result["roll"] == 7
    assert result["skill_level"] == -2
    assert result["bonus"] == -1 + stat_bonus + 2
    assert result["total"] == 7 + result["bonus"]
    assert result["difficulty"] == 8
    assert result["success"] is (result["total"] >= 8)

    clamped = replace(tools, rng=Rolls(1, 1)).roll_check(
        "skill", PC, skill=skill, stat="dex", difficulty=99
    )
    assert clamped["difficulty"] == 30


def test_first_aid_uses_a_trained_negative_skill_level(client_factory):
    client, campaign_id, original_tools = _wwn_campaign(client_factory)
    app = client.app
    assert isinstance(app, FastAPI)
    spec = replace(original_tools.character, skill_progression=SkillProgression(**LEVELS))
    tools = replace(original_tools, character=spec)
    with Session(tools.engine) as db:
        pc = load_state(db, campaign_id).pc()
        assert pc is not None
        record(
            db,
            campaign_id,
            SkillChanged(
                name=PC,
                skill="heal",
                skill_name="Heal",
                before=spec.skill_level(pc.skills, "heal"),
                after=-2,
            ),
        )
        db.commit()
    tools.set_stat_max(PC, "hp", 20, cause="a test")
    tools.set_stat_max(PC, "strain", 10, cause="a test")
    tools.harm_character(PC, "hp", 5, "a test")

    result = wwn_magic.first_aid(PC, PC, _ctx(tools))

    assert result["bonus"] == -2
    (check,) = _events(app, campaign_id, CheckMade.kind)
    assert (check.skill_level, check.skill_bonus) == (-2, -2)


def _skill_move(floor: object):
    definition = Definition(
        "move",
        "study",
        {
            "type": "move",
            "id": "study",
            "label": "Study",
            "description": "Study a mystery.",
            "dice": "1d6",
            "skill": "lore",
            "skill_floor": floor,
            "bands": [{"label": "Result"}],
        },
    )
    return _load_moves([definition], _spec(), frozenset(), "test")["study"]


def test_move_skill_floor_is_read_and_validated():
    assert _skill_move(None).skill_floor is None
    assert _skill_move(-3).skill_floor == -3
    with pytest.raises(ContentError, match="skill_floor"):
        _skill_move(True)


def test_move_can_apply_negative_skill_levels(client_factory):
    _, campaign_id, original_tools = _wwn_campaign(client_factory)
    spec = replace(original_tools.character, skill_progression=SkillProgression(**LEVELS))
    tools = replace(original_tools, character=spec)
    with Session(tools.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    skill = next(ident for ident in spec.skills if ident not in pc.skills)
    tools.raise_skill(PC, skill, -2, cause="training")
    move = replace(_skill_move(None), skill=skill)

    def roll(configured_move):
        with Session(tools.engine) as db:
            return moves.make_move(
                db,
                campaign_id,
                PC,
                configured_move,
                random.Random(0),
                plus="sum",
                combat=None,
                conditions={},
                counters={},
                spec=spec,
            )

    assert roll(move)["bonus"] == -2
    assert roll(replace(move, skill_floor=0))["bonus"] == 0
