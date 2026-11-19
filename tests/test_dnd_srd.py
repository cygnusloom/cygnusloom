"""The D&D 5.5e SRD rule set loads, creates characters, and advances them."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.rules import dnd_srd_2024
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.common import InvalidInput

from .factories import (
    character_named,
    create_campaign_direct,
    create_character_direct,
    rules_for,
    signed_in_client,
    tools_for,
)


def _dnd_rules() -> Any:
    return rules_for("dnd-srd-2024")


def _fields() -> dict[str, str]:
    return {
        "stat-str": "15",
        "stat-dex": "14",
        "stat-con": "13",
        "stat-int": "12",
        "stat-wis": "10",
        "stat-cha": "8",
        "campaign-xp_rate": "Standard",
        "template-species": "human",
        "template-background": "soldier",
        "template-class": "fighter",
        "pick-skill-class_skills-0": "history",
        "pick-skill-class_skills-1": "perception",
        "pick-feature-fighting_style-0": "feat-defense",
        "feature-choice-background-asi-soldier-scores-0": "str",
        "feature-choice-background-asi-soldier-scores-1": "dex",
        "feature-choice-background-asi-soldier-scores-2": "con",
    }


def _campaign(
    client_factory: Callable[[], TestClient],
) -> tuple[TestClient, int]:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(client, campaign_id, name="Rook", fields=_fields())
    return client, campaign_id


def test_the_srd_rule_set_loads_with_its_content() -> None:
    rules = _dnd_rules()
    assert rules.character.stats["str"].modifier(15) == 2
    assert rules.character.stats["str"].modifier(8) == -1
    assert set(rules.character.skills) >= {"athletics", "perception", "stealth"}
    assert len(rules.character.templates) > 20
    assert len(rules.monsters) > 300
    assert sum(1 for f in rules.character.features.values() if f.feature_type == "spell") == 339
    assert rules.level_ups["level"].max_level == 20
    monk = rules.character.templates["monk"]
    defense_id = next(grant.feature for grant in monk.features if "unarmored" in grant.feature)
    floor = rules.character.features[defense_id].defense_floor
    assert floor is not None and floor.requires_no_armor and floor.requires_no_shield


def test_a_fighter_is_created_with_srd_hit_points_and_saves(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id = _campaign(client_factory)
    pc = character_named(app, campaign_id, "Rook")
    assert pc.stats["con"] == 14
    assert pc.stats["hp"] == 12  # Fighter 9 + 1d1 + Constitution modifier 2
    assert pc.stats["attack_bonus"] == 2
    assert pc.skills["history"] == 2
    assert pc.skills["perception"] == 2
    assert pc.counters.get("hit_dice") == 1
    assert "feat-defense" in pc.features
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    sheet = tools.get_statblock("Rook")["statblock"]
    assert "Strength Save" not in sheet


def test_levelling_a_fighter_grants_hit_points_and_features(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id = _campaign(client_factory)
    rules = _dnd_rules()
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    ctx = level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)
    before = character_named(app, campaign_id, "Rook")
    tools.adjust_counter("Rook", "xp", 300)
    grants = dnd_srd_2024.project_level("Rook", ctx)
    assert grants.new_level == 2
    dnd_srd_2024.apply_level_up("Rook", ctx)
    after = character_named(app, campaign_id, "Rook")
    assert after.counters["level"] == 2
    assert after.stats["hp"] > before.stats["hp"]
    assert after.stats["attack_bonus"] == 2
    assert after.counters["hit_dice"] == 2
    assert "class-fighter-action-surge" in after.features


def _wizard_fields() -> dict[str, str]:
    return {
        "campaign-xp_rate": "Standard",
        "stat-str": "8",
        "stat-dex": "14",
        "stat-con": "13",
        "stat-int": "15",
        "stat-wis": "12",
        "stat-cha": "10",
        "template-species": "elf",
        "template-background": "sage",
        "template-class": "wizard",
        "pick-skill-class_skills-0": "investigation",
        "pick-skill-class_skills-1": "insight",
        "feature-choice-background-asi-sage-scores-0": "int",
        "feature-choice-background-asi-sage-scores-1": "wis",
        "feature-choice-background-asi-sage-scores-2": "con",
    }


def _wizard(app: FastAPI, client_factory: Callable[[], TestClient]) -> tuple[TestClient, int, Any]:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(client, campaign_id, name="Wren", fields=_wizard_fields())
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    ctx = level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)
    return client, campaign_id, ctx


def test_a_wizard_starts_with_slots_and_spell_picks(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, _ctx = _wizard(app, client_factory)
    pc = character_named(app, campaign_id, "Wren")
    assert pc.stats["hp"] == 8  # Wizard 5 + 1d1 + Constitution modifier 2
    assert pc.counters["slot_1"] == 2
    assert pc.counters["cantrip_picks"] == 3
    assert pc.counters["spell_picks"] == 6


def test_the_advancement_page_teaches_the_wizards_first_spells(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, _ctx = _wizard(app, client_factory)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    page = level_up_service.page_view(client.app.state.engine, campaign_id, rules, world, "Wren")
    assert page is not None and page.kind == level_up_service.PICKS
    picks = {pick["id"]: pick for pick in page.picks}
    assert picks["cantrips"]["available"] == 3
    assert picks["spells"]["available"] == 6
    form = {
        "pick-cantrips-0": "spell-fire-bolt",
        "pick-cantrips-1": "spell-light",
        "pick-cantrips-2": "spell-mage-hand",
        "pick-spells-0": "spell-magic-missile",
        "pick-spells-1": "spell-shield",
        "pick-spells-2": "spell-sleep",
        "pick-spells-3": "spell-thunderwave",
        "pick-spells-4": "spell-detect-magic",
        "pick-spells-5": "spell-fog-cloud",
    }
    level_up_service.apply_page(client.app.state.engine, campaign_id, rules, world, form, "Wren")
    after = character_named(app, campaign_id, "Wren")
    for spell in (
        "spell-fire-bolt",
        "spell-light",
        "spell-mage-hand",
        "spell-magic-missile",
        "spell-shield",
        "spell-sleep",
        "spell-thunderwave",
        "spell-detect-magic",
        "spell-fog-cloud",
    ):
        assert spell in after.features, spell
    assert after.counters["cantrip_picks"] == 0
    assert after.counters["spell_picks"] == 0


def test_a_level_with_no_picks_still_offers_the_level_up_button(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """Some levels only raise hit points and add features, so they have no picks to show."""
    from tests.conftest import csrf_token

    client, campaign_id = _campaign(client_factory)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    level_up_service.debug_grant(
        client.app.state.engine, campaign_id, rules, world, "xp", "Rook", 300
    )
    page = client.get(f"/campaigns/{campaign_id}/advancement").text
    assert "Level up to 2" in page
    token = csrf_token(client, f"/campaigns/{campaign_id}/advancement")
    response = client.post(
        f"/campaigns/{campaign_id}/advancement",
        data={"csrf_token": token, "character": "Rook"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    after = character_named(app, campaign_id, "Rook")
    assert after.counters["level"] == 2
    assert after.stats["hp"] == 20  # 12 + 8 (d10 average 6 + Constitution 2)
    assert "class-fighter-action-surge" in after.features
    assert "class-fighter-tactical-mind" in after.features


def test_casting_spends_a_slot_and_cantrips_are_free(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _wizard(app, client_factory)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {
            "pick-cantrips-0": "spell-fire-bolt",
            "pick-cantrips-1": "spell-light",
            "pick-cantrips-2": "spell-mage-hand",
            "pick-spells-0": "spell-magic-missile",
            "pick-spells-1": "spell-shield",
            "pick-spells-2": "spell-sleep",
            "pick-spells-3": "spell-thunderwave",
            "pick-spells-4": "spell-detect-magic",
            "pick-spells-5": "spell-fog-cloud",
        },
        "Wren",
    )
    result = dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx)
    assert result["slot_level"] == 1
    assert result["slots_left"] == 1
    cantrip = dnd_srd_2024.cast_spell("Wren", "fire-bolt", ctx)
    assert cantrip["slot_level"] is None
    after = character_named(app, campaign_id, "Wren")
    assert after.counters["slot_1"] == 1


def _prepared_wizard(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> tuple[TestClient, int, Any]:
    """A wizard who knows four leveled spells and three cantrips."""
    client, campaign_id, ctx = _wizard(app, client_factory)
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        _dnd_rules(),
        client.app.state.content.world_content("testhollow", "dnd-srd-2024"),
        {
            "pick-cantrips-0": "spell-fire-bolt",
            "pick-cantrips-1": "spell-light",
            "pick-cantrips-2": "spell-mage-hand",
            "pick-spells-0": "spell-magic-missile",
            "pick-spells-1": "spell-shield",
            "pick-spells-2": "spell-detect-magic",
            "pick-spells-3": "spell-fog-cloud",
            "pick-spells-4": "spell-charm-person",
            "pick-spells-5": "spell-mage-armor",
        },
        "Wren",
    )
    return client, campaign_id, ctx


def test_preparing_spells_limits_what_the_wizard_can_cast(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    result = dnd_srd_2024.prepare_spells("Wren", ["magic-missile", "shield"], ctx)
    assert result["limit"] == 4 and result["prepared"] == ["spell-magic-missile", "spell-shield"]
    pc = character_named(app, campaign_id, "Wren")
    assert pc.marks["prepared"] == ["spell-magic-missile", "spell-shield"]
    with pytest.raises(InvalidInput, match="has not prepared"):
        dnd_srd_2024.cast_spell("Wren", "fog-cloud", ctx)
    cast = dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx)
    assert cast["slot_level"] == 1 and cast["slots_left"] == 1


def test_a_prepared_ritual_casts_without_a_slot(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.prepare_spells("Wren", ["detect-magic", "magic-missile"], ctx)
    before = character_named(app, campaign_id, "Wren")
    assert before.counters["slot_1"] == 2
    result = dnd_srd_2024.cast_spell("Wren", "detect-magic", ctx, ritual=True)
    assert result["ritual"] is True and result["slot_level"] is None
    after = character_named(app, campaign_id, "Wren")
    assert after.counters["slot_1"] == 2
    with pytest.raises(InvalidInput, match="no Ritual tag"):
        dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx, ritual=True)


def test_concentration_moves_to_the_new_spell(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.prepare_spells("Wren", ["detect-magic", "fog-cloud"], ctx)
    dnd_srd_2024.cast_spell("Wren", "detect-magic", ctx, ritual=True)
    assert character_named(app, campaign_id, "Wren").marks["concentration"] == "spell-detect-magic"
    dnd_srd_2024.cast_spell("Wren", "fog-cloud", ctx)
    assert character_named(app, campaign_id, "Wren").marks["concentration"] == "spell-fog-cloud"


def test_a_save_uses_the_dc_and_reports_the_total(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, _campaign_id, ctx = _wizard(app, client_factory)
    passed = dnd_srd_2024.roll_save("Wren", "dex", 12, "a falling rock", ctx, roll=12)
    assert passed["bonus"] == 2  # +2 Dexterity; a wizard is not proficient in Dexterity saves
    assert passed["total"] == 14 and passed["passed"]
    failed = dnd_srd_2024.roll_save("Wren", "str", 15, "a shove", ctx, roll=10)
    assert failed["bonus"] == -1 and not failed["passed"]


def test_death_saves_and_hit_dice(app: FastAPI, client_factory: Callable[[], TestClient]) -> None:
    client, campaign_id, ctx = _wizard(app, client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.harm_character("Wren", "hp", 99, "a test fall")
    first = dnd_srd_2024.death_save("Wren", ctx, roll=9)
    assert first["failures"] == 1 and first["outcome"] == "failure"
    assert dnd_srd_2024.death_save("Wren", ctx, roll=10)["successes"] == 1
    healed_result = tools.heal_character("Wren", 7)
    assert healed_result["recovered"] is True
    healed = character_named(app, campaign_id, "Wren")
    assert not healed.defeated
    assert healed.counters["hit_dice"] == 1
    tools.harm_character("Wren", "hp", 3, "a test cut")
    spent = dnd_srd_2024.spend_hit_die("Wren", ctx)
    assert spent["hit_dice_left"] == 0


def test_subclass_and_ability_score_choices_apply(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _wizard(app, client_factory)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    for amount in (300, 600, 1800):
        tools.adjust_counter("Wren", "xp", amount)
        dnd_srd_2024.apply_level_up("Wren", ctx)
    after = character_named(app, campaign_id, "Wren")
    assert after.counters["level"] == 4
    page = level_up_service.page_view(client.app.state.engine, campaign_id, rules, world, "Wren")
    assert page is not None and page.kind == level_up_service.PICKS
    picks = {pick["id"]: pick for pick in page.picks}
    assert picks["subclass"]["available"] == 1
    assert picks["asi"]["available"] == 1
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {"pick-subclass-0": "subclass-evoker"},
        "Wren",
    )
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {
            "pick-asi-0": "feat-ability-score-improvement",
            "feature-choice-feat-ability-score-improvement-first-0": "int",
            "feature-choice-feat-ability-score-improvement-second-0": "int",
        },
        "Wren",
    )
    final = character_named(app, campaign_id, "Wren")
    assert "subclass-evoker" in final.features
    assert final.stats["int"] == 15 + 1 + 2  # sage background +1, ASI +2
    assert "feat-ability-score-improvement" in final.features


def test_a_constitution_increase_raises_hit_points_at_every_level(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _wizard(app, client_factory)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    for amount in (300, 600, 1800):
        tools.adjust_counter("Wren", "xp", amount)
        dnd_srd_2024.apply_level_up("Wren", ctx)
    before = character_named(app, campaign_id, "Wren")
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {
            "pick-asi-0": "feat-ability-score-improvement",
            "feature-choice-feat-ability-score-improvement-first-0": "con",
            "feature-choice-feat-ability-score-improvement-second-0": "con",
        },
        "Wren",
    )
    after = character_named(app, campaign_id, "Wren")
    assert after.stats["con"] == before.stats["con"] + 2
    # The Constitution modifier rises by 1, which adds 1 hit point for each of the 4 levels.
    assert after.stats["hp"] == before.stats["hp"] + 4


def test_a_long_rest_restores_slots_and_hit_dice(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _wizard(app, client_factory)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {
            "pick-cantrips-0": "spell-fire-bolt",
            "pick-cantrips-1": "spell-light",
            "pick-cantrips-2": "spell-mage-hand",
            "pick-spells-0": "spell-magic-missile",
            "pick-spells-1": "spell-shield",
            "pick-spells-2": "spell-sleep",
            "pick-spells-3": "spell-thunderwave",
            "pick-spells-4": "spell-detect-magic",
            "pick-spells-5": "spell-fog-cloud",
        },
        "Wren",
    )
    dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.rest("long", advance_clock=True)
    after = character_named(app, campaign_id, "Wren")
    assert after.counters["slot_1"] == 2
    assert after.counters["hit_dice"] == 1


def test_a_bestiary_monster_is_created_from_its_statblock(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, _ctx = _wizard(app, client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.create_location("Cave Mouth", "A low cave in a mossy hill.")
    tools.create_monster("Goblin", "Cave Mouth", template="monster-goblin-warrior")
    goblin = character_named(app, campaign_id, "Goblin")
    assert goblin.stats["hp"] == 10
    assert goblin.stats["attack_bonus"] == 4
    assert goblin.armor == 15
    assert goblin.attacks["Scimitar"] == "1d6+2"
    assert goblin.attacks["Shortbow"] == "1d6+2"
    save = dnd_srd_2024.roll_save("Goblin", "dex", 10, "a test", _ctx, roll=1)
    assert save["bonus"] == 2


def _standard_stats() -> dict[str, str]:
    return {
        "stat-str": "15",
        "stat-dex": "14",
        "stat-con": "13",
        "stat-int": "12",
        "stat-wis": "10",
        "stat-cha": "8",
        "campaign-xp_rate": "Standard",
        "template-species": "human",
    }


def _cleric_fields() -> dict[str, str]:
    return {
        **_standard_stats(),
        "template-background": "acolyte",
        "template-class": "cleric",
        "pick-skill-class_skills-0": "history",
        "pick-skill-class_skills-1": "medicine",
        "pick-feature-divine_order-0": "divine-order-protector",
        "feature-choice-background-asi-acolyte-scores-0": "int",
        "feature-choice-background-asi-acolyte-scores-1": "wis",
        "feature-choice-background-asi-acolyte-scores-2": "cha",
    }


def _sorcerer_fields() -> dict[str, str]:
    return {
        **_standard_stats(),
        "template-background": "sage",
        "template-class": "sorcerer",
        "pick-skill-class_skills-0": "insight",
        "pick-skill-class_skills-1": "religion",
        "feature-choice-background-asi-sage-scores-0": "int",
        "feature-choice-background-asi-sage-scores-1": "wis",
        "feature-choice-background-asi-sage-scores-2": "con",
    }


def _warlock_fields() -> dict[str, str]:
    return {
        **_standard_stats(),
        "template-background": "criminal",
        "template-class": "warlock",
        "pick-skill-class_skills-0": "arcana",
        "pick-skill-class_skills-1": "religion",
        "feature-choice-background-asi-criminal-scores-0": "dex",
        "feature-choice-background-asi-criminal-scores-1": "int",
        "feature-choice-background-asi-criminal-scores-2": "con",
    }


def _druid_fields() -> dict[str, str]:
    return {
        **_standard_stats(),
        "template-background": "sage",
        "template-class": "druid",
        "pick-skill-class_skills-0": "nature",
        "pick-skill-class_skills-1": "perception",
        "feature-choice-background-asi-sage-scores-0": "int",
        "feature-choice-background-asi-sage-scores-1": "wis",
        "feature-choice-background-asi-sage-scores-2": "con",
    }


def _class_campaign(
    client_factory: Callable[[], TestClient], name: str, fields: dict[str, str]
) -> tuple[TestClient, int, Any]:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(client, campaign_id, name=name, fields=fields)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    ctx = level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)
    return client, campaign_id, ctx


def test_class_resources_are_spent_and_refilled_by_rests(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id = _campaign(client_factory)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    ctx = level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    pc = character_named(app, campaign_id, "Rook")
    assert pc.counters["second_wind"] == 2
    dnd_srd_2024.use_class_resource("Rook", "second_wind", ctx)
    assert character_named(app, campaign_id, "Rook").counters["second_wind"] == 1
    dnd_srd_2024.use_class_resource("Rook", "second_wind", ctx)
    tools.rest("short", advance_clock=True)
    assert character_named(app, campaign_id, "Rook").counters["second_wind"] == 1
    tools.rest("long", advance_clock=True)
    assert character_named(app, campaign_id, "Rook").counters["second_wind"] == 2


def test_divine_order_and_life_domain_spells(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _class_campaign(client_factory, "Cleric", _cleric_fields())
    pc = character_named(app, campaign_id, "Cleric")
    assert "divine-order-protector" in pc.features
    assert pc.counters["channel_divinity"] == 0
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    for amount in (300, 600):
        tools.adjust_counter("Cleric", "xp", amount)
        dnd_srd_2024.apply_level_up("Cleric", ctx)
    leveled = character_named(app, campaign_id, "Cleric")
    assert leveled.counters["channel_divinity"] == 2
    page = level_up_service.page_view(client.app.state.engine, campaign_id, rules, world, "Cleric")
    assert page is not None and page.kind == level_up_service.PICKS
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {"pick-subclass-0": "subclass-life-domain"},
        "Cleric",
    )
    after = character_named(app, campaign_id, "Cleric")
    assert "spell-bless" in after.features and "spell-aid" in after.features
    assert "spell-bless" in after.marks["always_prepared"]
    dnd_srd_2024.prepare_spells("Cleric", ["cure-wounds"], ctx)
    cast = dnd_srd_2024.cast_spell("Cleric", "bless", ctx)
    assert cast["slot_level"] == 1


def test_metamagic_and_invocations_are_chosen_from_options(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    sorcerer_client, sorcerer_id, sorcerer_ctx = _class_campaign(
        client_factory, "Sorcerer", _sorcerer_fields()
    )
    tools = tools_for(sorcerer_client.app, sorcerer_id, rule_set="dnd-srd-2024")
    rules = _dnd_rules()
    world = sorcerer_client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    tools.adjust_counter("Sorcerer", "xp", 300)
    dnd_srd_2024.apply_level_up("Sorcerer", sorcerer_ctx)
    assert character_named(app, sorcerer_id, "Sorcerer").counters["metamagic_picks"] == 2
    level_up_service.apply_page(
        sorcerer_client.app.state.engine,
        sorcerer_id,
        rules,
        world,
        {
            "pick-metamagic-0": "metamagic-careful-spell",
            "pick-metamagic-1": "metamagic-distant-spell",
        },
        "Sorcerer",
    )
    sorcerer = character_named(app, sorcerer_id, "Sorcerer")
    assert "metamagic-careful-spell" in sorcerer.features
    assert "metamagic-distant-spell" in sorcerer.features

    warlock_client, warlock_id, _ctx = _class_campaign(client_factory, "Warlock", _warlock_fields())
    assert character_named(app, warlock_id, "Warlock").counters["invocation_picks"] == 1
    level_up_service.apply_page(
        warlock_client.app.state.engine,
        warlock_id,
        rules,
        warlock_client.app.state.content.world_content("testhollow", "dnd-srd-2024"),
        {"pick-invocations-0": "invocation-armor-of-shadows"},
        "Warlock",
    )
    warlock = character_named(app, warlock_id, "Warlock")
    assert "invocation-armor-of-shadows" in warlock.features


def test_attunement_tracks_carried_magic_items(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id = _campaign(client_factory)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    ctx = level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.give_item("Rook", "Amulet of Health")
    result = dnd_srd_2024.attune_item("Rook", "Amulet of Health", ctx)
    assert result["attuned"] == ["Amulet of Health"]
    assert character_named(app, campaign_id, "Rook").marks["attuned"] == ["Amulet of Health"]
    ended = dnd_srd_2024.unattune_item("Rook", "Amulet of Health", ctx)
    assert ended["attuned"] == []


def test_circle_of_the_land_chooses_its_land_spells(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _class_campaign(client_factory, "Druid", _druid_fields())
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    for amount in (300, 600):
        tools.adjust_counter("Druid", "xp", amount)
        dnd_srd_2024.apply_level_up("Druid", ctx)
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {"pick-subclass-0": "subclass-circle-of-the-land"},
        "Druid",
    )
    result = dnd_srd_2024.choose_land("Druid", "arid", ctx)
    assert "spell-burning-hands" in result["spells"]
    after = character_named(app, campaign_id, "Druid")
    assert after.marks["land"] == "arid"
    assert "spell-burning-hands" in after.marks["always_prepared"]
    assert "spell-burning-hands" in after.features
