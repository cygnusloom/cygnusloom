from __future__ import annotations

import random

import pytest

from cygnusforge.content.character import (
    CharacterError,
    Sheet,
    character_schema,
    describe,
    feature_pick_options,
    resolve,
)
from cygnusforge.services.tools import check_arguments
from tests.conftest import content_library


def _expert(first: str, second: str, *, repeats: bool = False):
    spec = content_library().rule_sets["worldswithoutnumber"].character
    submitted = {
        "templates": {"class": "expert"},
        "skill_picks": {"class_free_skill": ["notice"]},
        "feature_picks": {"class_focus_any": [first], "class_focus_expert": [second]},
    }
    if repeats:
        submitted["feature_repeats"] = {"class_focus_expert": [second]}
    return spec, submitted


@pytest.mark.parametrize("focus", ["focus_unique_gift", "focus_xenoblooded"])
def test_one_rank_focus_cannot_be_chosen_twice(focus):
    spec, submitted = _expert(focus, focus)
    with pytest.raises(CharacterError, match="maximum rank"):
        resolve(spec, submitted, random.Random(1), plus="sum")


def test_developed_attribute_can_be_taken_again_for_another_attribute():
    spec, submitted = _expert("focus_developed_attribute", "focus_developed_attribute")
    sheet = resolve(spec, submitted, random.Random(1), plus="sum")
    assert sheet.features["focus_developed_attribute"] == 2


@pytest.mark.parametrize("repeat_as_new", [False, True])
def test_specialist_can_advance_or_be_taken_for_another_skill(repeat_as_new):
    spec, submitted = _expert("focus_specialist", "focus_specialist", repeats=repeat_as_new)
    assert check_arguments(character_schema(spec), submitted) is None
    sheet = resolve(spec, submitted, random.Random(1), plus="sum")
    assert sheet.features.get("focus_specialist") == (2 if repeat_as_new else 1)
    assert sheet.features.get("focus_specialist_2", 0) == (0 if repeat_as_new else 1)


def test_only_selected_explicitly_repeatable_features_can_repeat_as_new():
    spec, submitted = _expert("focus_artisan", "focus_artisan", repeats=True)
    with pytest.raises(CharacterError, match="not a selected repeatable feature"):
        resolve(spec, submitted, random.Random(1), plus="sum")


def test_repeat_as_new_requires_the_first_rank_to_be_owned():
    spec, submitted = _expert("focus_connected", "focus_specialist", repeats=True)
    with pytest.raises(CharacterError, match="cannot be repeated as a new choice"):
        resolve(spec, submitted, random.Random(1), plus="sum")


@pytest.mark.parametrize("focus", ["focus_developed_attribute", "focus_nullifier"])
def test_partial_mage_expert_pick_excludes_forbidden_foci(focus):
    spec = content_library().rule_sets["worldswithoutnumber"].character
    picks = spec.templates["adventurer_expert_mage"].feat_picks
    assert all(
        focus not in {feature.id for feature in feature_pick_options(spec, pick)} for pick in picks
    )


@pytest.mark.parametrize("class_id", ["healer", "vowed"])
def test_standalone_partial_mage_classes_receive_a_general_focus(class_id):
    spec = content_library().rule_sets["worldswithoutnumber"].character
    pick = next(
        pick for pick in spec.templates[class_id].feat_picks if pick.id == "class_focus_any"
    )
    options = {feature.id for feature in feature_pick_options(spec, pick)}
    assert "focus_armored_magic" in options
    assert not options.intersection(
        {"focus_developed_attribute", "focus_nullifier", "focus_polymath"}
    )
    sheet = resolve(
        spec,
        {
            "templates": {"class": class_id},
            "feature_picks": {"class_focus_any": ["focus_unique_gift"]},
        },
        random.Random(1),
        plus="sum",
    )
    assert sheet.features["focus_unique_gift"] == 1


def test_rank_two_focus_requires_rank_one_even_from_raw_features():
    spec = content_library().rule_sets["worldswithoutnumber"].character
    with pytest.raises(CharacterError, match="requires its previous rank"):
        resolve(spec, {"features": ["focus_die_hard_2"]}, random.Random(1), plus="sum")
    sheet = resolve(
        spec,
        {"features": ["focus_die_hard", "focus_die_hard_2"]},
        random.Random(1),
        plus="sum",
    )
    assert set(sheet.features) >= {"focus_die_hard", "focus_die_hard_2"}


def test_character_description_distinguishes_ordered_ranks_and_repeated_choices():
    spec = content_library().rule_sets["worldswithoutnumber"].character
    sheet = Sheet(
        stats={},
        templates={},
        features={"focus_spirit_familiar": 1, "focus_spirit_familiar_2": 2},
        fields={},
    )
    description = describe(spec, sheet)
    assert "Spirit Familiar (rank 1)" in description
    assert "Spirit Familiar (rank 2) x2" in description
