"""D&D 5.5e multiclassing: prerequisites, class levels, Hit Dice, and spell slots."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.content.character import Sheet, describe
from cygnusloom.rules import dnd_srd_2024
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.common import InvalidInput

from .factories import (
    character_named,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    rules_for,
    signed_in_client,
    tools_for,
)
from .test_dnd_srd import _campaign, _fields


def _world(client: TestClient) -> Any:
    return client.app.state.content.world_content("testhollow", "dnd-srd-2024")


def _ctx(client: TestClient, campaign_id: int) -> Any:
    return level_up_service.tool_context(
        client.app.state.engine, campaign_id, rules_for("dnd-srd-2024"), _world(client)
    )


def _fields_with(**changes: str) -> dict[str, str]:
    return {**_fields(), **changes}


def _campaign_with(
    client_factory: Callable[[], TestClient], fields: Mapping[str, str]
) -> tuple[TestClient, int]:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(client, campaign_id, name="Rook", fields=fields)
    close_opening(client, campaign_id)
    return client, campaign_id


def test_a_new_class_needs_a_13_in_its_primary_ability(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id = _campaign(client_factory)
    rules = rules_for("dnd-srd-2024")
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.adjust_counter("Rook", "xp", 300)
    with pytest.raises(InvalidInput, match="ability score prerequisite"):
        level_up_service.page_view(
            client.app.state.engine, campaign_id, rules, _world(client), "Rook", "wizard"
        )


def test_a_fighter_can_take_a_wizard_level(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    # Strength 15, Dexterity 14, Constitution 12, Intelligence 13, Wisdom 10, Charisma 8:
    # one legal swap of the standard array that leaves the Fighter and Wizard prerequisites met.
    client, campaign_id = _campaign_with(
        client_factory, _fields_with(**{"stat-con": "12", "stat-int": "13"})
    )
    rules = rules_for("dnd-srd-2024")
    world = _world(client)
    ctx = _ctx(client, campaign_id)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.adjust_counter("Rook", "xp", 300)
    before = character_named(app, campaign_id, "Rook")
    page = level_up_service.page_view(
        client.app.state.engine, campaign_id, rules, world, "Rook", "wizard"
    )
    assert page is not None and page.summary is not None
    assert page.summary["class_id"] == "wizard"
    assert page.summary["class_level"] == 1
    assert any(
        option["id"] == "wizard" and option["current"] is False
        for option in page.summary["class_options"]
    )
    html = client.get(f"/campaigns/{campaign_id}/advancement?character=Rook").text
    assert "Advance in" in html
    wizard_html = client.get(
        f"/campaigns/{campaign_id}/advancement?character=Rook&class=wizard"
    ).text
    assert "Wizard 1" in wizard_html
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {
            "character": "Rook",
            "class": "wizard",
            "pick-spells-0": "spell-magic-missile",
            "pick-cantrips-0": "spell-fire-bolt",
        },
        "Rook",
    )
    pc = character_named(app, campaign_id, "Rook")
    assert pc.counters["level"] == 2
    assert pc.class_levels == {"fighter": 1, "wizard": 1}
    assert pc.hit_dice_pools == {"d10": 1, "d6": 1}
    assert pc.stats["hp"] == before.stats["hp"] + 5  # d6 average 4, Constitution 12 (+1)
    assert "class-wizard-spellcasting" in pc.features
    assert pc.counters["spell_picks"] == 5
    assert pc.counters["cantrip_picks"] == 2
    assert pc.counters["slot_1"] == 2
    assert pc.marks["spell_class"] == {
        "spell-magic-missile": "wizard",
        "spell-fire-bolt": "wizard",
    }
    assert dnd_srd_2024.class_level_of(pc, "wizard") == 1
    # Adding Wizard does not grant its Intelligence and Wisdom save proficiencies.
    assert dnd_srd_2024.roll_save("Rook", "int", 10, "test", ctx, roll=10)["bonus"] == 1
    assert dnd_srd_2024.roll_save("Rook", "str", 10, "test", ctx, roll=10)["bonus"] == 5
    sheet = Sheet(
        stats=pc.stats,
        templates=pc.templates,
        features=pc.features,
        items=pc.items,
        fields=pc.fields,
        class_levels=pc.class_levels,
        counters=pc.counters,
        skills=pc.skills,
        feature_choices=pc.feature_choices,
        stat_modifiers=pc.stat_modifiers,
    )
    text = describe(rules.character, sheet)
    assert "Classes: Fighter 1 / Wizard 1" in text
    assert "Class: Fighter" not in text
    spent = dnd_srd_2024.spend_hit_die("Rook", ctx, "d6")
    assert spent["hit_die"] == "d6"
    pc = character_named(app, campaign_id, "Rook")
    assert pc.hit_dice_pools == {"d10": 1, "d6": 0}
    assert pc.counters["hit_dice"] == 1
    with pytest.raises(InvalidInput, match="no d6"):
        dnd_srd_2024.spend_hit_die("Rook", ctx, "d6")


def test_each_caster_class_keeps_its_own_prepared_spells(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    base = _fields()
    base.pop("pick-feature-fighting_style-0", None)
    fields = {
        **base,
        "template-class": "druid",
        "stat-con": "10",
        "stat-wis": "13",
        "pick-skill-class_skills-0": "nature",
        "pick-skill-class_skills-1": "perception",
    }
    client, campaign_id = _campaign_with(client_factory, fields)
    rules = rules_for("dnd-srd-2024")
    world = _world(client)
    ctx = _ctx(client, campaign_id)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.adjust_counter("Rook", "xp", 300)
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {"character": "Rook", "class": "cleric"},
        "Rook",
    )
    dnd_srd_2024.prepare_spells("Rook", ["spell-cure-wounds", "spell-entangle"], ctx, "druid")
    dnd_srd_2024.prepare_spells("Rook", ["spell-bless", "spell-guiding-bolt"], ctx, "cleric")
    pc = character_named(app, campaign_id, "Rook")
    assert set(pc.marks["prepared_by_class"]["druid"]) == {
        "spell-cure-wounds",
        "spell-entangle",
    }
    assert set(pc.marks["prepared_by_class"]["cleric"]) == {
        "spell-bless",
        "spell-guiding-bolt",
    }
    assert {
        "spell-cure-wounds",
        "spell-entangle",
        "spell-bless",
        "spell-guiding-bolt",
    } <= set(pc.marks["prepared"])
    before_slots = pc.counters["slot_1"]
    cast = dnd_srd_2024.cast_spell("Rook", "spell-cure-wounds", ctx)
    assert cast["slot_level"] == 1
    assert character_named(app, campaign_id, "Rook").counters["slot_1"] == before_slots - 1


def test_a_banked_spell_pick_uses_the_class_the_player_names(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id = _campaign_with(
        client_factory, _fields_with(**{"stat-con": "12", "stat-int": "13"})
    )
    rules = rules_for("dnd-srd-2024")
    world = _world(client)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.adjust_counter("Rook", "xp", 300)
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {"character": "Rook", "class": "wizard"},
        "Rook",
    )
    page = level_up_service.page_view(client.app.state.engine, campaign_id, rules, world, "Rook")
    assert page is not None and page.kind == level_up_service.PICKS
    assert {option["id"] for option in page.class_options} == {"fighter", "wizard"}
    wizard_page = level_up_service.page_view(
        client.app.state.engine, campaign_id, rules, world, "Rook", "wizard"
    )
    assert wizard_page is not None
    spells = next(pick for pick in wizard_page.picks if pick["id"] == "spells")
    assert "spell-magic-missile" in {option["id"] for option in spells["options"]}
    html = client.get(f"/campaigns/{campaign_id}/advancement?character=Rook").text
    assert "Picks for" in html
    wizard_html = client.get(
        f"/campaigns/{campaign_id}/advancement?character=Rook&class=wizard"
    ).text
    assert "Magic Missile" in wizard_html
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {
            "character": "Rook",
            "class": "wizard",
            "pick-spells-0": "spell-magic-missile",
        },
        "Rook",
    )
    pc = character_named(app, campaign_id, "Rook")
    assert "spell-magic-missile" in pc.features
    assert pc.marks["spell_class"]["spell-magic-missile"] == "wizard"


def test_continuing_the_first_class_records_its_class_level(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id = _campaign(client_factory)
    rules = rules_for("dnd-srd-2024")
    world = _world(client)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.adjust_counter("Rook", "xp", 300)
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {"character": "Rook"},
        "Rook",
    )
    pc = character_named(app, campaign_id, "Rook")
    assert pc.counters["level"] == 2
    assert pc.class_levels == {"fighter": 2}
    assert pc.hit_dice_pools == {"d10": 2}
    assert "class-fighter-action-surge" in pc.features


def test_a_multiclass_skill_pick_trains_a_new_skill(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id = _campaign(client_factory)
    rules = rules_for("dnd-srd-2024")
    world = _world(client)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.adjust_counter("Rook", "xp", 300)
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {"character": "Rook", "class": "rogue"},
        "Rook",
    )
    pc = character_named(app, campaign_id, "Rook")
    assert pc.counters["multiclass_skill_picks"] == 1
    page = level_up_service.page_view(client.app.state.engine, campaign_id, rules, world, "Rook")
    assert page is not None and page.kind == level_up_service.PICKS
    skill_pick = next(pick for pick in page.picks if pick["id"] == "multiclass_skills")
    rogue = rules.character.templates["rogue"].skill_picks[0]
    allowed = {option.value for option in rules.character.tables[rogue.table]}
    assert {skill["id"] for skill in skill_pick["skills"]} <= allowed
    available = [skill for skill in skill_pick["skills"] if skill["targets"]]
    assert available
    chosen = available[0]["id"]
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {
            "character": "Rook",
            "class": "rogue",
            f"skill-multiclass_skills-{chosen}": "2",
        },
        "Rook",
    )
    pc = character_named(app, campaign_id, "Rook")
    assert rules.character.skill_level(pc.skills, chosen) == 2
    assert pc.counters["multiclass_skill_picks"] == 0


def test_a_warlock_multiclass_keeps_pact_slots_separate(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    # Charisma 13 and Constitution 8, a legal swap that meets the Warlock prerequisite.
    client, campaign_id = _campaign_with(
        client_factory, _fields_with(**{"stat-con": "8", "stat-cha": "13"})
    )
    rules = rules_for("dnd-srd-2024")
    world = _world(client)
    ctx = _ctx(client, campaign_id)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.adjust_counter("Rook", "xp", 300)
    level_up_service.apply_page(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        {"character": "Rook", "class": "warlock"},
        "Rook",
    )
    pc = character_named(app, campaign_id, "Rook")
    assert pc.class_levels == {"fighter": 1, "warlock": 1}
    assert pc.counters["pact_slots"] == 1
    assert pc.counters.get("slot_1", 0) == 0
    dnd_srd_2024.learn_spell("Rook", "hex", ctx)
    dnd_srd_2024.prepare_spells("Rook", ["hex"], ctx, "warlock")
    cast = dnd_srd_2024.cast_spell("Rook", "hex", ctx)
    assert cast["slot_level"] == 1
    assert character_named(app, campaign_id, "Rook").counters["pact_slots"] == 0
    with pytest.raises(InvalidInput, match="no spell slot"):
        dnd_srd_2024.cast_spell("Rook", "hex", ctx)
