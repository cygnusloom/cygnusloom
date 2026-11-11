"""The bundled feature reactions the engine matches during checks, moves, and reaction rolls."""

from __future__ import annotations

from cygnusloom.content.character import Reaction
from cygnusloom.content.library import RuleSet
from tests.conftest import content_library


def _reactions(rule_set: RuleSet, feature_id: str) -> tuple[Reaction, ...]:
    return rule_set.character.features[feature_id].reactions


def test_every_limited_bundled_reaction_uses_a_reset_the_engine_fires() -> None:
    for rule_set in content_library().rule_sets.values():
        resets = {"day", "week", "rest"}
        for period in rule_set.periods.values():
            resets.update(period.resets)
        for feature in rule_set.character.features.values():
            for reaction in feature.reactions:
                assert reaction.reset != "session", (rule_set.id, feature.id)
                if reaction.uses:
                    assert reaction.reset in resets, (rule_set.id, feature.id, reaction.id)
                else:
                    assert reaction.reset == "" or reaction.reset in resets


def test_cultured_rerolls_a_failed_social_check_once_per_rest() -> None:
    cultured = _reactions(content_library().rule_sets["worldswithoutnumber"], "focus_cultured_2")

    assert len(cultured) == 1
    reaction = cultured[0]
    assert reaction.trigger == "check"
    assert dict(reaction.when) == {
        "skills": ("connect", "convince", "lead", "perform"),
        "success": False,
    }
    assert dict(reaction.effect) == {"op": "reroll", "keep": "better"}
    assert (reaction.uses, reaction.reset) == (1, "rest")


def test_well_met_modifies_and_upgrades_reaction_rolls() -> None:
    rule_set = content_library().rule_sets["worldswithoutnumber"]

    first = _reactions(rule_set, "focus_well_met")[0]
    assert (first.trigger, first.uses, first.reset) == ("reaction", 0, "")
    assert dict(first.effect) == {"op": "add_total", "amount": 1}

    second = _reactions(rule_set, "focus_well_met_2")[0]
    assert (second.trigger, second.uses, second.reset) == ("reaction", 1, "week")
    assert dict(second.when) == {"max_total": 11}
    assert second.effect["op"] == "set_reaction"


def test_academia_weekly_spell_reactions_raise_a_six_minus_to_seven_to_nine() -> None:
    rule_set = content_library().rule_sets["academia-arcana"]

    for feature_id, when in (
        ("talent-prodigy", {"move_tags": ("spell",), "bands": ("6-",)}),
        ("signature-spell", {"moves": ("signature_spell",), "bands": ("6-",)}),
    ):
        reaction = _reactions(rule_set, feature_id)[0]
        assert reaction.trigger == "move"
        assert dict(reaction.when) == when
        assert dict(reaction.effect) == {"op": "raise_band", "to": "7-9"}
        assert (reaction.uses, reaction.reset) == (1, "week")
