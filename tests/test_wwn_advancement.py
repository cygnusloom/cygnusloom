"""Worlds Without Number advancement: experience, levels, skills, Foci, spells, and AC.

The helpers here build a real campaign and player character through the web creation form, the
way `tests/test_wwn.py` does, and hand the rule set's tools a `WorldTools` for the campaign.
`tests/test_wwn_magic.py` reuses the same helpers.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.content.character import Feature
from cygnusforge.content.library import ContentLibrary
from cygnusforge.rules import wwn
from cygnusforge.services import campaigns as campaigns_service
from cygnusforge.services.common import InvalidInput, session_scope
from cygnusforge.services.events import (
    ArmorSet,
    FeatureLearned,
    SkillChanged,
    decode,
    events_since,
    load_state,
)
from cygnusforge.services.world import WorldTools
from tests.conftest import content_library
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_character_creation import _choices, _rolled_stats

LIBRARY: ContentLibrary = content_library()
WWN = LIBRARY.rule_sets["worldswithoutnumber"]
NAME = "Wren"

# The creation form rolls attributes from an unseeded generator. These tests pin the roll so the
# class, Constitution, Dexterity, and mental modifiers a level or a check sees are known. A
# Constitution of 10 gives no HP modifier, so the rolled 6 is also the submitted HP.
FIXED_STATS = {
    "str": 12,
    "dex": 14,
    "con": 10,
    "int": 18,
    "wis": 10,
    "cha": 8,
    "hp": 6,
    "attack_bonus": 1,
    "castings": 1,
    "morale": 1,
}


def _fixed_roll(spec: object, rng: object, *, plus: str) -> dict[str, int]:
    """The rolled attribute pool, pinned so the tests know every modifier."""
    return dict(FIXED_STATS)


# The Focus and art picks each class and tradition takes in the creation form, so the tests know
# exactly which features the character starts with instead of rolling them.
CLASS_PICKS: dict[str, dict[str, str]] = {
    "warrior": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_warrior-0": "focus_alert",
    },
    "expert": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_expert-0": "focus_connected",
    },
    "mage": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
    },
    "adventurer_expert_warrior": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_expert-0": "focus_connected",
        "pick-feature-class_focus_warrior-0": "focus_alert",
    },
    "adventurer_expert_mage": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_expert-0": "focus_connected",
    },
    "adventurer_warrior_mage": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_warrior-0": "focus_alert",
    },
    "adventurer_mage_mage": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
    },
}
TRADITION_PICKS: dict[str, dict[str, str]] = {
    "high_mage": {"pick-feature-tradition_art_high-0": "art_arcane_lexicon"},
    "necromancer": {"pick-feature-tradition_art_necromancer-0": "art_bonetalker"},
    "elementalist": {"pick-feature-tradition_art_elementalist-0": "art_ele_elemental_resilience"},
}


def _campaign(
    client_factory: Callable[[], TestClient],
    *,
    class_id: str = "warrior",
    tradition: str | None = None,
    name: str = NAME,
) -> tuple[FastAPI, int]:
    """A campaign with one player character of the chosen class, created through the form."""
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="latter-earth", rule_set="worldswithoutnumber")
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(campaigns_service, "roll_stats", _fixed_roll)
        page = client.get(f"/campaigns/{campaign_id}").text
        fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
        fields.update(_choices(page))
        fields["template-class"] = class_id
        fields.update(CLASS_PICKS[class_id])
        if tradition is not None:
            fields["template-tradition"] = tradition
            fields.update(TRADITION_PICKS[tradition])
        created = _create_character(client, campaign_id, name=name, fields=fields)
    error = re.search(r'<p class="form-error"[^>]*>(.*?)</p>', created.text, re.DOTALL)
    assert created.status_code == 303, error.group(1) if error else created.text[:500]
    return client.app, campaign_id


def _world_tools(
    app: FastAPI,
    campaign_id: int,
    *,
    seed: int = 0,
    spec: Any = None,
) -> WorldTools:
    """The campaign's world tools, configured as `services/campaigns.py` configures them."""
    world = LIBRARY.world_content("latter-earth", "worldswithoutnumber")
    return WorldTools(
        app.state.engine,
        campaign_id,
        {**LIBRARY.core.tables, **world.tables},
        random.Random(seed),
        spec or WWN.character,
        grid=WWN.grid,
        terrain=WWN.terrain,
        encounter_objects=WWN.encounter_objects,
        encounter_templates=WWN.encounter_templates,
        combat=WWN.combat,
        monsters={**WWN.monsters, **world.monsters},
        features={**world.features},
        checks=WWN.checks,
        conditions=WWN.conditions,
        moves=WWN.moves,
        counters=WWN.counters,
        advancements=WWN.advancements,
        periods=WWN.periods,
        tallies=WWN.tallies,
        relations=WWN.relations,
        dice_tables={**WWN.dice_tables, **world.dice_tables},
        clock_rules=WWN.clock,
        rests=WWN.rests,
        interrupted_rest=WWN.interrupted_rest,
        hooks=WWN.hooks,
        inventory=WWN.inventory,
        item_sizes=WWN.item_sizes,
        magic=WWN.magic,
        marketplace=WWN.marketplace,
        plus=WWN.plus,
        npc_hp=WWN.npc_hp,
        npc_hp_stat=WWN.npc_hp_stat,
        travel=WWN.travel,
        wilderness=WWN.wilderness,
        world_terrain=WWN.world_terrain,
    )


def _ctx(tools: WorldTools) -> Any:
    """A tool context holds the world the rule set's functions read."""
    return SimpleNamespace(world=tools)


def _pc(app: FastAPI, campaign_id: int):
    with Session(app.state.engine) as db:
        character = load_state(db, campaign_id).pc()
    assert character is not None
    return character


def _stat(app: FastAPI, campaign_id: int, stat: str) -> int:
    return _pc(app, campaign_id).stats[stat]


def _counter(app: FastAPI, campaign_id: int, counter: str) -> int:
    return _pc(app, campaign_id).counters.get(counter, WWN.counters[counter].initial)


def _events_of(app: FastAPI, campaign_id: int, kind: str) -> list[Any]:
    with session_scope(app.state.engine) as db:
        return [decode(row) for row in events_since(db, campaign_id) if row.kind == kind]


def _set_stat(tools: WorldTools, stat: str, value: int) -> None:
    tools.set_stat_max(NAME, stat, value, cause="a test")


def _give_pick(tools: WorldTools, counter: str, amount: int) -> None:
    tools.adjust_counter(NAME, counter, amount)


def test_the_submitted_warrior_and_mage_keep_their_class_and_tradition(
    client_factory: Callable[[], TestClient],
) -> None:
    warrior_app, warrior_campaign = _campaign(client_factory, class_id="warrior")
    warrior = _pc(warrior_app, warrior_campaign)
    assert warrior.templates["class"] == "warrior"
    assert "tradition" not in warrior.templates
    assert warrior.stats["hp"] == 8  # 6 rolled + a Warrior's +2
    assert warrior.stats["attack_bonus"] == 1

    mage_app, mage_campaign = _campaign(client_factory, class_id="mage", tradition="high_mage")
    mage = _pc(mage_app, mage_campaign)
    assert mage.templates["class"] == "mage"
    assert mage.templates["tradition"] == "high_mage"
    assert mage.stats["hp"] == 5  # 6 rolled - a Mage's -1
    assert mage.stats["attack_bonus"] == 0
    assert mage.skills.get("magic") == 0  # the tradition's free skill
    assert "art_arcane_lexicon" in mage.features


def test_gain_xp_accumulates_and_marks_the_next_level_threshold(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))

    below = wwn.gain_xp(NAME, 2, "a first session", ctx)
    assert below["xp"] == 2
    assert below["level"] == 1
    assert below["next_level"] == 2
    assert below["next_threshold"] == wwn.WWN_XP[1] == 3
    assert below["ready"] is False
    assert _counter(app, campaign_id, "xp") == 2

    with pytest.raises(InvalidInput, match="level 2 needs 3"):
        wwn.level_up(NAME, ctx)
    assert _counter(app, campaign_id, "level") == 1

    at = wwn.gain_xp(NAME, 1, "a second session", ctx)
    assert at["xp"] == 3
    assert at["ready"] is True
    assert _counter(app, campaign_id, "xp") == 3


def test_level_up_raises_hp_attack_bonus_skill_points_and_focus_picks(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id, seed=0)
    ctx = _ctx(tools)
    _set_stat(tools, "con", 10)  # no Constitution modifier
    before = _pc(app, campaign_id)

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.level_up(NAME, ctx)

    # random.Random(0) rolls 4 on the first d6; a Warrior adds +2 a level.
    assert result["level"] == 2
    assert result["hp_gained"] == 6
    assert result["attack_bonus"] == 2
    assert result["skill_points"] == 1
    assert result["focus_picks"] == 1
    assert result["castings"] is None

    character = _pc(app, campaign_id)
    assert character.counters["level"] == 2
    assert character.stats["hp"] == before.stats["hp"] + 6
    assert character.stats["attack_bonus"] == 2
    assert character.counters["skill_points"] == 1
    assert character.counters["focus_picks"] == 1
    # The saves lose one point a level.
    assert character.stats["save_luck"] == before.stats["save_luck"] - 1


def test_an_expert_pairing_gains_two_skill_points_on_level_up(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="adventurer_expert_warrior")
    ctx = _ctx(_world_tools(app, campaign_id))

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.level_up(NAME, ctx)

    # Quick Learner gives the Expert and partial Expert an extra point each level.
    assert result["skill_points"] == 2
    assert _counter(app, campaign_id, "skill_points") == 2


def test_a_caster_raises_castings_from_the_class_table_on_level_up(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    ctx = _ctx(_world_tools(app, campaign_id))
    assert _stat(app, campaign_id, "castings") == 1

    wwn.gain_xp(NAME, 3, "a session", ctx)
    second = wwn.level_up(NAME, ctx)
    assert second["level"] == 2
    assert second["castings"] == 1
    assert _stat(app, campaign_id, "castings") == 1

    wwn.gain_xp(NAME, 3, "another session", ctx)
    third = wwn.level_up(NAME, ctx)
    assert third["level"] == 3
    assert third["castings"] == 2
    assert _stat(app, campaign_id, "castings") == 2
    assert wwn.casting_row("mage", 3).casts == 2


def test_a_mage_with_a_poor_constitution_gains_at_least_one_hit_point(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    # Seed 2 rolls 1 on the first d6. A Mage adds -1 a level, and Constitution 3 adds -2.
    tools = _world_tools(app, campaign_id, seed=2)
    ctx = _ctx(tools)
    _set_stat(tools, "con", 3)
    tools.set_stat_max(NAME, "hp", 5, cause="a test")

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.level_up(NAME, ctx)

    assert result["hp_gained"] == 1
    assert _stat(app, campaign_id, "hp") == 6


def test_spend_skill_point_raises_a_skill_and_spends_one_point(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    _give_pick(tools, "skill_points", 2)
    before = _pc(app, campaign_id).skills.get("sneak", -1)

    result = wwn.spend_skill_point(NAME, "sneak", ctx)

    assert result["level"] == before + 1
    assert result["remaining"] == 1
    assert _pc(app, campaign_id).skills["sneak"] == before + 1
    assert _counter(app, campaign_id, "skill_points") == 1
    (changed,) = _events_of(app, campaign_id, SkillChanged.kind)
    assert (changed.skill, changed.after) == ("sneak", before + 1)


def test_spend_skill_point_caps_a_skill_at_four(client_factory: Callable[[], TestClient]) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    _give_pick(tools, "skill_points", 10)

    for _ in range(10):
        if _pc(app, campaign_id).skills.get("sneak", -1) >= 4:
            break
        wwn.spend_skill_point(NAME, "sneak", ctx)

    assert _pc(app, campaign_id).skills["sneak"] == 4
    points = _counter(app, campaign_id, "skill_points")
    with pytest.raises(InvalidInput, match="already at level 4"):
        wwn.spend_skill_point(NAME, "sneak", ctx)
    assert _counter(app, campaign_id, "skill_points") == points


def test_spend_skill_point_refuses_without_a_point_or_a_skill(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    assert _counter(app, campaign_id, "skill_points") == 0

    with pytest.raises(InvalidInput, match="no skill points to spend"):
        wwn.spend_skill_point(NAME, "sneak", ctx)
    with pytest.raises(InvalidInput, match="there is no skill named 'wizardry'"):
        wwn.spend_skill_point(NAME, "wizardry", ctx)


def test_learn_focus_spends_a_pick_and_applies_skill_stat_and_armor_effects(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    synthetic = Feature(
        id="focus_test_effects",
        name="Test Effects",
        feature_type="feat",
        description="A focus that exercises each effect the advancement tools apply.",
        size="normal",
        uses=0,
        armor=0,
        tag="focus",
        rank=1,
        effects=(
            {"op": "skill", "id": "sneak", "delta": 1},
            {"op": "stat", "id": "str", "delta": 1},
            {"op": "armor", "delta": 1},
        ),
    )
    spec = replace(
        WWN.character,
        features={**WWN.character.features, synthetic.id: synthetic},
    )
    tools = _world_tools(app, campaign_id, spec=spec)
    ctx = _ctx(tools)
    _give_pick(tools, "focus_picks", 1)
    _set_stat(tools, "str", 10)
    _set_stat(tools, "dex", 10)
    before = _pc(app, campaign_id)

    result = wwn.learn_focus(NAME, synthetic.id, ctx)

    character = _pc(app, campaign_id)
    assert result["focus_picks"] == 0
    assert result["rank"] == 1
    assert character.features[synthetic.id] == 1
    assert character.counters["focus_picks"] == 0
    assert character.skills["sneak"] == min(4, before.skills.get("sneak", -1) + 1)
    assert character.stats["str"] == 11
    assert character.armor == 11  # 10 + Dexterity 0 + one point of armor
    (learned,) = _events_of(app, campaign_id, FeatureLearned.kind)
    assert (learned.feature, learned.name) == (synthetic.id, NAME)


def test_learn_focus_needs_a_pick_and_the_first_rank(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)

    with pytest.raises(InvalidInput, match="no focus picks to spend"):
        wwn.learn_focus(NAME, "focus_armsmaster", ctx)

    _give_pick(tools, "focus_picks", 2)
    with pytest.raises(InvalidInput, match="needs Armsmaster first"):
        wwn.learn_focus(NAME, "focus_armsmaster_2", ctx)
    assert _counter(app, campaign_id, "focus_picks") == 2

    wwn.learn_focus(NAME, "focus_armsmaster", ctx)
    assert _counter(app, campaign_id, "focus_picks") == 1
    wwn.learn_focus(NAME, "focus_armsmaster_2", ctx)
    assert _counter(app, campaign_id, "focus_picks") == 0

    _give_pick(tools, "focus_picks", 1)
    with pytest.raises(InvalidInput, match="already has Armsmaster"):
        wwn.learn_focus(NAME, "focus_armsmaster", ctx)


def test_learn_spell_adds_a_tradition_spell_of_a_legal_level(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)

    result = wwn.learn_spell(NAME, "spell_coruscating_coffin", ctx)

    assert result["rank"] == 1
    assert result["tradition"] == "high"
    assert "spell_coruscating_coffin" in _pc(app, campaign_id).features

    with pytest.raises(InvalidInput, match="can cast spells of level 1 at most"):
        wwn.learn_spell(NAME, "spell_calculation_evoked_servitor", ctx)
    with pytest.raises(InvalidInput, match="not a spell of"):
        wwn.learn_spell(NAME, "spell_command_the_dead", ctx)
    with pytest.raises(InvalidInput, match="is not a spell"):
        wwn.learn_spell(NAME, "focus_alert", ctx)
    with pytest.raises(InvalidInput, match="already knows"):
        wwn.learn_spell(NAME, "spell_coruscating_coffin", ctx)


def test_set_ac_adds_dexterity_and_base_armor_and_records_it(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    _set_stat(tools, "dex", 14)  # a +1 modifier

    result = wwn.set_ac(name=NAME, base_armor=2, ctx=ctx)

    assert result["dexterity"] == 1
    assert result["base_armor"] == 2
    assert result["armor"] == 13
    assert _pc(app, campaign_id).armor == 13
    (armor,) = _events_of(app, campaign_id, ArmorSet.kind)
    assert (armor.name, armor.armor) == (NAME, 13)
