"""Structured choices on features: a chosen skill is trained, a chosen stat gets its modifier,
and a choice-bound reaction follows the skill the player picked."""

from __future__ import annotations

import random
from dataclasses import asdict, replace

import pytest
from sqlmodel import Session

from cygnusloom.content.character import resolve
from cygnusloom.services import reactions
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    PC_KIND,
    Character,
    FeatureLearned,
    load_state,
    name_key,
    record,
)
from tests.conftest import content_library
from tests.test_combat import Rolls
from tests.test_wwn_combat import BASE_STATS, _wwn_campaign

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


def test_diplomatic_grace_rerolls_ones_on_a_social_check(client_factory):
    _client, _campaign_id, tools = _wwn_campaign(
        client_factory,
        choices={"pick-feature-class_focus_any-0": "focus_diplomatic_grace"},
    )
    tools = replace(tools, rng=Rolls(5))
    check = next(iter(WWN.checks))

    result = tools.roll_check(
        check, "Wren", skill="convince", stat="cha", difficulty=10, roll=3, dice_values=[1, 2]
    )

    assert result["roll"] == 7
    assert "Diplomatic Grace" in result["reaction"]


def test_lucky_rolls_checks_and_saves_twice(client_factory):
    _client, _campaign_id, tools = _wwn_campaign(
        client_factory,
        choices={"pick-feature-class_focus_any-0": "focus_lucky"},
    )
    tools = replace(tools, rng=Rolls(6, 6, 20))
    check = next(iter(WWN.checks))

    result = tools.roll_check(
        check, "Wren", skill="notice", stat="wis", difficulty=8, roll=2
    )
    save = tools.player_save(roll=1, stat="save_physical", cause="a trap")

    assert result["roll"] == 12
    assert "Lucky" in result["reaction"]
    assert save["passed"] is True
    assert "Lucky" in save["reaction"]


def test_unobtrusive_step_shares_one_hourly_reroll_between_its_checks():
    sheet = resolve(
        WWN.character,
        {"features": ["art_unobtrusive_step"]},
        random.Random(1),
        plus=WWN.plus,
    )
    character = _character_from_sheet(sheet)
    check = next(iter(WWN.checks.values()))

    sneak = reactions.match_check(
        character, WWN.character, None, check, "sneak", "dex", total=2, success=False
    )
    impersonate = reactions.match_check(
        character,
        WWN.character,
        None,
        check,
        "convince",
        "cha",
        total=2,
        success=False,
        cause="an impersonation",
    )
    assert sneak is not None
    assert impersonate is not None
    assert sneak.reaction.shared == impersonate.reaction.shared == "unobtrusive-step"

    character.reaction_uses["hour:art_unobtrusive_step:unobtrusive-step"] = 1
    assert (
        reactions.match_check(
            character, WWN.character, None, check, "sneak", "dex", total=2, success=False
        )
        is None
    )


def test_empowered_sorcery_rerolls_a_spell_damage_die(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    with Session(client.app.state.engine) as db:
        record(
            db,
            campaign_id,
            FeatureLearned(
                name="Wren",
                feature="art_empowered_sorcery",
                feature_name="Empowered Sorcery",
            ),
        )
        db.commit()
    tools = replace(tools, rng=Rolls(6, 6))

    result = tools.empowered_sorcery("2d6", original=3)

    assert result["original"] == 3
    assert result["rerolled"] == 12


def test_inexorable_effect_forces_an_enemy_save_reroll_once_per_scene(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    with Session(client.app.state.engine) as db:
        record(
            db,
            campaign_id,
            FeatureLearned(
                name="Wren",
                feature="art_inexorable_effect",
                feature_name="Inexorable Effect",
            ),
        )
        db.commit()
    tools.create_location("Yard", "A quiet yard.")
    tools.create_npc("Foe", "Yard", "A rival.", character={"stats": BASE_STATS}, force=True)
    tools = replace(tools, rng=Rolls(1))

    result = tools.inexorable_effect("Foe", "save_physical", "a spell")

    assert result["character"] == "Wren"
    assert result["passed"] is False
    with pytest.raises(InvalidInput, match="already used"):
        tools.inexorable_effect("Foe", "save_physical", "a spell")


def test_an_npc_with_lucky_rerolls_its_own_save(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    tools.create_location("Yard", "A quiet yard.")
    tools.create_npc("Foe", "Yard", "A lucky rival.", character={"stats": BASE_STATS}, force=True)
    with Session(client.app.state.engine) as db:
        record(
            db,
            campaign_id,
            FeatureLearned(
                name="Foe", feature="focus_lucky", feature_name="Lucky"
            ),
        )
        db.commit()
    tools = replace(tools, rng=Rolls(1, 20))

    result = tools.roll_save("Foe", "save_physical", "a spell")

    assert result["passed"] is True
    assert "Lucky" in result["reaction"]


def test_an_npc_can_use_the_art_tools_with_its_own_features(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    tools.create_location("Yard", "A quiet yard.")
    tools.create_npc("Foe", "Yard", "A rival mage.", character={"stats": BASE_STATS}, force=True)
    with Session(client.app.state.engine) as db:
        for feature, name in (
            ("art_empowered_sorcery", "Empowered Sorcery"),
            ("art_inexorable_effect", "Inexorable Effect"),
        ):
            record(
                db,
                campaign_id,
                FeatureLearned(name="Foe", feature=feature, feature_name=name),
            )
        db.commit()
    tools = replace(tools, rng=Rolls(6, 6, 1))

    sorcery = tools.empowered_sorcery("2d6", original=3, character="Foe")
    inexorable = tools.inexorable_effect(
        "Wren", "save_physical", "a spell", character="Foe"
    )

    assert sorcery["character"] == "Foe"
    assert sorcery["rerolled"] == 12
    assert inexorable["character"] == "Foe"
    assert inexorable["target"] == "Wren"


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
