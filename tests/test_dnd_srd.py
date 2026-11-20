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
from cygnusloom.services.conditions import condition_roll_mode

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


def test_class_starting_kits_offer_the_srd_lettered_packages() -> None:
    rules = _dnd_rules()
    fighter = [option.id for option in rules.character.templates["fighter"].grant.options]
    assert fighter == ["a", "b", "c"]
    gold_only = rules.character.templates["wizard"].grant.options[-1]
    assert gold_only.id == "b"
    assert gold_only.silver == 550
    assert not gold_only.items
    acolyte = rules.character.templates["acolyte"].grant.options
    assert [option.id for option in acolyte] == ["a", "b"]
    assert acolyte[0].silver == 80
    assert acolyte[0].items.count("Parchment") == 1
    assert acolyte[1].silver == 500


def test_monster_and_subclass_features_carry_their_passives() -> None:
    rules = _dnd_rules()
    assert rules.character.features["subclass-champion-improved-critical"].critical_min == 19
    assert rules.character.features["class-barbarian-rage"].active_mark == "raging"
    assert rules.character.features["class-barbarian-rage"].damage_bonus_levels[1] == 2


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


def test_a_character_keeps_the_starting_kit_pick_and_its_gold(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(
        client,
        campaign_id,
        name="Rook",
        fields={**_fields(), "grant-fighter": "c"},
    )
    pc = character_named(app, campaign_id, "Rook")
    assert pc.fields["grant:fighter"] == "c"
    assert pc.features.get("silver", 0) >= 1550
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    assert "grant:fighter" not in tools.get_statblock("Rook")["statblock"]


def test_a_dragonborn_chooses_an_ancestry_and_gains_its_resistance(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    fields = {
        **_fields(),
        "template-species": "dragonborn",
        "pick-feature-dragonborn-ancestry-0": "lineage-dragonborn-black",
    }
    create_character_direct(client, campaign_id, name="Ash", fields=fields)
    pc = character_named(app, campaign_id, "Ash")
    assert "lineage-dragonborn-black" in pc.features
    rules = _dnd_rules()
    ancestry = rules.character.features["lineage-dragonborn-black"]
    assert ancestry.resistances == ("acid",)
    assert ancestry.damage_type == "acid"


def test_a_tiefling_chooses_a_legacy_and_gains_its_resistance(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    fields = {
        **_fields(),
        "template-species": "tiefling",
        "pick-feature-tiefling-lineage-0": "lineage-tiefling-infernal",
    }
    create_character_direct(client, campaign_id, name="Ember", fields=fields)
    pc = character_named(app, campaign_id, "Ember")
    assert "lineage-tiefling-infernal" in pc.features
    assert _dnd_rules().character.features["lineage-tiefling-infernal"].resistances == ("fire",)


def test_alert_adds_proficiency_and_savage_attacker_rerolls_weapon_dice(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    fields = {
        **_fields(),
        "pick-feature-versatile-0": "feat-alert",
    }
    create_character_direct(client, campaign_id, name="Rook", fields=fields)
    pc = character_named(app, campaign_id, "Rook")
    assert dnd_srd_2024._initiative_bonus(pc, {}) == 4  # Dexterity +2 and PB +2
    rules = _dnd_rules()
    assert rules.character.features["feat-savage-attacker"].weapon_damage_reroll is True


def test_a_drow_learns_lineage_cantrips_and_later_spells(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    other = create_campaign_direct(client, world_template="testhollow", rule_set="dnd-srd-2024")
    fields = {
        **_wizard_fields(),
        "pick-feature-elf-lineage-0": "lineage-elf-drow",
        "feature-choice-lineage-elf-drow-ability-0": "cha",
    }
    create_character_direct(client, other, name="Drow", fields=fields)
    pc = character_named(app, other, "Drow")
    assert "spell-dancing-lights" in pc.features
    assert pc.feature_choices["lineage-elf-drow:ability"] == ["cha"]
    tools = tools_for(client.app, other, rule_set="dnd-srd-2024")
    tools.adjust_counter("Drow", "xp", 6500)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    other_ctx = level_up_service.tool_context(client.app.state.engine, other, rules, world)
    for _level in range(4):
        dnd_srd_2024.apply_level_up("Drow", other_ctx)
    levelled = character_named(app, other, "Drow")
    assert "spell-faerie-fire" in levelled.features
    assert "spell-darkness" in levelled.features
    assert "lineage-elf-drow-spell-3-spell-faerie-fire" in levelled.features
    assert "lineage-elf-drow-spell-5-spell-darkness" in levelled.features
    slots = int(levelled.counters.get("slot_1", 0))
    tools.adjust_counter("Drow", "slot_1", -slots)
    cast = dnd_srd_2024.cast_spell("Drow", "faerie-fire", other_ctx)
    assert cast["slot_level"] is None
    assert "spell-faerie-fire" in character_named(app, other, "Drow").marks["free_cast"]


def test_a_goliath_ancestry_boon_spends_its_resource(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    fields = {
        **_fields(),
        "template-species": "goliath",
        "pick-feature-goliath-lineage-0": "lineage-goliath-cloud",
    }
    create_character_direct(client, campaign_id, name="Rook", fields=fields)
    pc = character_named(app, campaign_id, "Rook")
    assert pc.counters["giant_ancestry"] == 2
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    ctx = level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)
    used = dnd_srd_2024.use_feature("Rook", "lineage-goliath-cloud", ctx)
    assert used["feature"] == "lineage-goliath-cloud"
    assert used["remaining"] == 1
    assert character_named(app, campaign_id, "Rook").counters["giant_ancestry"] == 1


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
    assert _dnd_rules().character.features["spell-magic-missile"].turn_resource == "action"
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


def test_a_level_up_shows_and_records_new_class_feature_choices(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    fields = {
        **_fields(),
        "template-class": "ranger",
        "pick-skill-class_skills-0": "perception",
        "pick-skill-class_skills-1": "stealth",
        "pick-skill-class_skills-2": "survival",
    }
    create_character_direct(client, campaign_id, name="Rook", fields=fields)
    level_up_service.debug_grant(
        client.app.state.engine,
        campaign_id,
        _dnd_rules(),
        client.app.state.content.world_content("testhollow", "dnd-srd-2024"),
        "xp",
        "Rook",
        300,
    )
    page = client.get(f"/campaigns/{campaign_id}/advancement").text
    assert "feature-choice-class-ranger-deft-explorer-expertise-0" in page
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        _dnd_rules(),
        client.app.state.content.world_content("testhollow", "dnd-srd-2024"),
        {"feature-choice-class-ranger-deft-explorer-expertise-0": "stealth"},
        "Rook",
    )
    pc = character_named(app, campaign_id, "Rook")
    assert pc.feature_choices["class-ranger-deft-explorer:expertise"] == ["stealth"]


def test_magic_initiate_casts_its_spell_once_without_a_slot(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    fields = {
        **_fields(),
        "pick-feature-versatile-0": "feat-magic-initiate",
        "feature-choice-feat-magic-initiate-cantrips-0": "spell-fire-bolt",
        "feature-choice-feat-magic-initiate-cantrips-1": "spell-light",
        "feature-choice-feat-magic-initiate-spell-0": "spell-detect-magic",
        "feature-choice-feat-magic-initiate-ability-0": "int",
    }
    create_character_direct(client, campaign_id, name="Rook", fields=fields)
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    ctx = level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)
    result = dnd_srd_2024.cast_spell("Rook", "detect-magic", ctx)
    assert result["slot_level"] is None
    pc = character_named(app, campaign_id, "Rook")
    assert pc.marks["magic_initiate_free"] == "spell-detect-magic"
    with pytest.raises(InvalidInput, match="no spell slot"):
        dnd_srd_2024.cast_spell("Rook", "detect-magic", ctx)


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


def test_a_background_can_use_the_plus_two_plus_one_ability_mode(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        key: value
        for key, value in _fields().items()
        if not key.startswith("feature-choice-background-asi-soldier-scores-")
    }
    fields.update(
        {
            "feature-choice-background-asi-soldier-scores-mode": "1",
            "feature-choice-background-asi-soldier-scores-0": "str",
            "feature-choice-background-asi-soldier-scores-1": "dex",
        }
    )
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(client, campaign_id, name="Rook", fields=fields)
    fighter = character_named(app, campaign_id, "Rook")
    assert fighter.stats["str"] == 17  # 15 +2
    assert fighter.stats["dex"] == 15  # 14 +1


def test_human_skillful_and_versatile_grant_a_skill_and_an_origin_feat(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **_fields(),
        "pick-skill-skillful-0": "arcana",
        "pick-feature-versatile-0": "feat-magic-initiate",
        "feature-choice-feat-magic-initiate-cantrips-0": "spell-fire-bolt",
        "feature-choice-feat-magic-initiate-cantrips-1": "spell-guidance",
        "feature-choice-feat-magic-initiate-spell-0": "spell-bless",
        "feature-choice-feat-magic-initiate-ability-0": "int",
    }
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(client, campaign_id, name="Rook", fields=fields)
    fighter = character_named(app, campaign_id, "Rook")
    assert fighter.skills["arcana"] == 2  # Human Skillful
    assert fighter.features["feat-magic-initiate"] == 1
    assert fighter.features["spell-fire-bolt"] == 1
    assert fighter.features["spell-guidance"] == 1
    assert fighter.features["spell-bless"] == 1


def test_dwarven_toughness_adds_hit_points_at_creation_and_level_up(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {**_fields(), "template-species": "dwarf"}
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(client, campaign_id, name="Rook", fields=fields)
    initial = character_named(app, campaign_id, "Rook")
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    ctx = level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.adjust_counter("Rook", "xp", 300)
    dnd_srd_2024.apply_level_up("Rook", ctx)
    levelled = character_named(app, campaign_id, "Rook")
    assert levelled.stats["hp"] >= initial.stats["hp"] + 9  # d10 average 6 + Con 2 + Toughness 1


def test_a_druid_always_has_speak_with_animals_prepared(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, _campaign_id, ctx = _class_campaign(client_factory, "Sela", _druid_fields())
    result = dnd_srd_2024.cast_spell("Sela", "spell-speak-with-animals", ctx)
    assert result["spell"] == "Speak with Animals"


def test_armor_training_refuses_armor_a_class_cannot_wear(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, _ctx = _class_campaign(client_factory, "Wren", _wizard_fields())
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.give_item("Wren", "Chain Mail")
    with pytest.raises(InvalidInput, match="not trained"):
        tools.equip_armor("Wren", "Chain Mail")


def test_inventory_links_items_to_the_compendium_without_dumping_descriptions(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.give_item("Rook", "Longsword")
    page = client.get(f"/campaigns/{campaign_id}").text
    start = page.index("data-inventory")
    panel = page[start : page.index("</ul>", start)]
    assert (
        'data-view-link="compendium"><strong>Longsword</strong></a>' in panel
    )
    assert "Sap. If you hit" not in panel


def test_a_poisoned_character_has_disadvantage_on_attacks_and_checks(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, _ctx = _class_campaign(client_factory, "Rook", _fields())
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.add_condition("Rook", "poisoned", "a test poison")
    fighter = character_named(app, campaign_id, "Rook")
    conditions = tools.conditions
    assert condition_roll_mode(fighter, conditions, "attack") == "disadvantage"
    assert condition_roll_mode(fighter, conditions, "check") == "disadvantage"


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


def test_a_cleric_prepares_every_spell_chosen_at_creation(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """Each new spell joins the prepared list; learning the next one must not stop after the
    first because the comparison shares the stored list."""
    client, campaign_id, _ctx = _class_campaign(client_factory, "Cleric", _cleric_fields())
    rules = _dnd_rules()
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {
            "pick-spells-0": "spell-guiding-bolt",
            "pick-spells-1": "spell-healing-word",
            "pick-spells-2": "spell-bless",
            "pick-spells-3": "spell-shield-of-faith",
            "pick-cantrips-0": "spell-sacred-flame",
            "pick-cantrips-1": "spell-guidance",
            "pick-cantrips-2": "spell-light",
        },
        "Cleric",
    )
    pc = character_named(app, campaign_id, "Cleric")
    assert pc.marks["prepared"] == [
        "spell-guiding-bolt",
        "spell-healing-word",
        "spell-bless",
        "spell-shield-of-faith",
    ]


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
