from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cygnusforge.rules import wwn, wwn_magic
from cygnusforge.rules.wwn_progression import configure_progression
from cygnusforge.services.common import InvalidInput
from tests.test_wwn_advancement import NAME, WWN, _campaign, _ctx, _pc, _world_tools

PROGRESSION_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "cygnusforge"
    / "data"
    / "rulesets"
    / "worldswithoutnumber"
    / "progression.json"
)


def _progression_data():
    definitions = json.loads(PROGRESSION_PATH.read_text(encoding="utf-8"))
    return next(definition for definition in definitions if definition["type"] == "rule_data")


def test_xp_and_level_up_use_the_loaded_ruleset_data(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    data = _progression_data()
    data["xp"]["fast"][1] = 5
    data["classes"][0]["attack"][1] = 3
    progression = configure_progression(data, WWN.character, "custom progression")
    tools = replace(tools, rule_data={**tools.rule_data, "progression": progression})
    ctx = _ctx(tools)

    before = wwn.gain_xp(NAME, 3, "a test", ctx, rate="fast")
    assert before["next_threshold"] == 5
    assert not before["ready"]
    after = wwn.gain_xp(NAME, 2, "a test", ctx, rate="fast")
    assert after["ready"]

    result = wwn.level_up(NAME, ctx, rate="fast")

    assert result["level"] == 2
    assert result["attack_bonus"] == 3
    assert _pc(app, campaign_id).stats["attack_bonus"] == 3


def test_spell_preparation_and_casting_use_the_loaded_ruleset_data(client_factory):
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    tools = _world_tools(app, campaign_id)
    data = _progression_data()
    data["casting"]["full_mage"][0]["prepared"] = 1
    data["casting"]["full_mage"][1]["casts"] = 2
    progression = configure_progression(data, WWN.character, "custom progression")
    ctx = _ctx(replace(tools, rule_data={**tools.rule_data, "progression": progression}))
    known = sorted(
        feature for feature in _pc(app, campaign_id).features if feature.startswith("spell_")
    )
    assert len(known) >= 2

    with pytest.raises(InvalidInput, match="may prepare 1 spells at level 1, not 2"):
        wwn_magic.prepare_spells(NAME, known[:2], ctx)
    assert wwn_magic.prepare_spells(NAME, known[:1], ctx)["limit"] == 1
    wwn.gain_xp(NAME, 3, "a test", ctx, rate="fast")
    assert wwn.level_up(NAME, ctx, rate="fast")["castings"] == 2
    assert wwn_magic.cast_spell(NAME, known[0], ctx)["castings_left"] == 1
    assert wwn_magic.cast_spell(NAME, known[0], ctx)["castings_left"] == 0


def test_second_tradition_grants_and_can_spend_its_art_pick(client_factory):
    app, campaign_id = _campaign(
        client_factory,
        class_id="adventurer_mage_mage",
        tradition="necromancer",
        tradition2="high_mage_partial_2",
    )
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    wwn.gain_xp(NAME, 3, "a test", ctx, rate="fast")

    result = wwn.level_up(NAME, ctx, rate="fast")

    assert result["art_picks"] == 1
    known = _pc(app, campaign_id).features
    art = next(
        feature.id
        for feature in WWN.character.features.values()
        if feature.feature_type == "feat" and feature.tag == "high" and feature.id not in known
    )
    assert wwn.learn_art(NAME, art, ctx)["tradition"] == "high"
