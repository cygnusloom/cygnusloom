"""Structured choices on features: a chosen skill is trained, a chosen stat gets its modifier,
and a choice-bound reaction follows the skill the player picked."""

from __future__ import annotations

import random
from dataclasses import asdict

from sqlmodel import Session

from cygnusloom.content.character import resolve
from cygnusloom.services import reactions
from cygnusloom.services.events import PC_KIND, Character, load_state, name_key
from tests.conftest import content_library
from tests.test_wwn_combat import _wwn_campaign

WWN = content_library().rule_sets["worldswithoutnumber"]


def _character_from_sheet(sheet) -> Character:
    return Character(
        kind=PC_KIND,
        name="Wren",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
        **asdict(sheet),
    )


def test_a_valiant_defender_choice_trains_the_chosen_skill(client_factory):
    client, campaign_id, _tools = _wwn_campaign(
        client_factory,
        choices={
            "pick-feature-class_focus_warrior-0": "focus_valiant_defender",
            "feature-choice-focus_valiant_defender-skill-0": "punch",
        },
    )
    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).characters[name_key("Wren")]
    assert pc.features["focus_valiant_defender"] == 1
    assert pc.feature_choices["focus_valiant_defender:skill"] == ["punch"]
    assert pc.skills["punch"] == WWN.character.skill_progression.first_level
    assert WWN.character.skill_trained(pc.skills, "punch")


def test_an_elf_reroll_follows_the_skill_the_player_chose():
    sheet = resolve(
        WWN.character,
        {
            "features": ["focus_origin_elf"],
            "feature_choices": {"focus_origin_elf:skill": ["notice"]},
        },
        random.Random(1),
        plus=WWN.plus,
    )
    character = _character_from_sheet(sheet)
    check = next(iter(WWN.checks.values()))

    assert reactions.match_check(
        character, WWN.character, None, check, "notice", "int", total=2, success=False
    )
    assert (
        reactions.match_check(
            character, WWN.character, None, check, "convince", "cha", total=2, success=False
        )
        is None
    )


def test_specialist_turns_checks_with_its_chosen_skill_into_best_of_three():
    sheet = resolve(
        WWN.character,
        {
            "features": ["focus_specialist"],
            "feature_choices": {"focus_specialist:skill": ["notice"]},
        },
        random.Random(1),
        plus=WWN.plus,
    )
    character = _character_from_sheet(sheet)

    assert reactions.check_best(character, WWN.character, None, "notice") == (3, 2)
    assert reactions.check_best(character, WWN.character, None, "convince") is None
