"""D&D play through the engine: initiative, combat math, recovery, and content.

These tests exercise the paths a live GM uses, rather than only the rule module's
bookkeeping: an encounter with real participants, the printed monster fields, and the
advancement page after an experience award.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Event
from cygnusloom.rules import dnd_srd_2024
from cygnusloom.services import encounters
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.events import EncounterTurnEnded, load_state, record

from .factories import (
    character_named,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    rules_for,
    signed_in_client,
    tools_for,
)

FIGHTER_FIELDS = {
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

BARBARIAN_FIELDS = {
    **{key: value for key, value in FIGHTER_FIELDS.items() if not key.startswith("pick-feature")},
    "template-class": "barbarian",
    "pick-skill-class_skills-0": "nature",
    "pick-skill-class_skills-1": "survival",
}


def _campaign(
    client_factory: Callable[[], TestClient],
    name: str = "Rook",
    fields: dict[str, str] | None = None,
) -> tuple[TestClient, int, Any]:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, world_template="testhollow", rule_set="dnd-srd-2024"
    )
    create_character_direct(
        client, campaign_id, name=name, fields=fields if fields is not None else FIGHTER_FIELDS
    )
    close_opening(client, campaign_id)
    rules = rules_for("dnd-srd-2024")
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    ctx = level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)
    return client, campaign_id, ctx


def _fight(
    client: TestClient, campaign_id: int, *, monster: str = "monster-ogre"
) -> tuple[Any, Any]:
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.create_location("Cave", "A low cave.")
    tools.create_encounter_map("Cave Yard", "Cave", 12, 12, "Open stone.")
    tools.start_encounter("Cave Yard")
    tools.create_monster("Foe", "Cave", template=monster)
    tools.add_participant("Rook")
    tools.add_participant("Foe")
    return tools, None


def _state(app: FastAPI, campaign_id: int) -> Any:
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id)


def test_roll_initiative_sets_a_numeric_order_and_unblocks_the_first_turn(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    _fight(client, campaign_id)
    result = dnd_srd_2024.roll_initiative(ctx)
    state = _state(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.settled
    assert state.encounter.fixed_order
    assert set(result["order"]) == {"Rook", "Foe"}
    assert len(result["rolls"]) == 2
    foe_roll = next(entry for entry in result["rolls"] if entry["name"] == "Foe")
    assert foe_roll["bonus"] == -1  # the ogre's printed initiative bonus
    assert not encounters.initiative_open(state)
    order = list(state.encounter.order)
    with Session(app.state.engine) as db:
        encounters.begin_turn(db, campaign_id, result["order"][0])
    assert _state(app, campaign_id).encounter.acting == result["order"][0]
    with Session(app.state.engine) as db:
        for name in order:
            record(db, campaign_id, EncounterTurnEnded(name=name))
        db.commit()
    later = _state(app, campaign_id)
    assert later.encounter is not None
    assert later.encounter.round == 2
    assert later.encounter.order == order


def test_awarding_experience_reaches_the_advancement_page(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    result = dnd_srd_2024.award_xp("Rook", 300, ctx, "a goblin")
    assert result["gained"] == 300
    assert result["xp"] == 300
    rules = rules_for("dnd-srd-2024")
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    page = level_up_service.page_view(client.app.state.engine, campaign_id, rules, world, "Rook")
    assert page is not None


def test_unarmored_defense_and_fighting_style_change_armor_class(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, fighter_id, _ctx = _campaign(client_factory)
    fighter = character_named(app, fighter_id, "Rook")
    assert fighter.armor == 17  # chain mail 16 + Defense 1

    _client, barbarian_id, _ctx = _campaign(client_factory, name="Bran", fields=BARBARIAN_FIELDS)
    barbarian = character_named(app, barbarian_id, "Bran")
    # The standard array gives Dex 14 and Soldier gives Con 14: 10 + 2 + 2.
    assert barbarian.armor == 14


def test_a_level_five_fighter_can_use_extra_attack(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    for amount in (300, 600, 1800, 3800):
        tools.adjust_counter("Rook", "xp", amount)
        dnd_srd_2024.apply_level_up("Rook", ctx)
    fighter = character_named(app, campaign_id, "Rook")
    assert fighter.counters["level"] == 5
    assert dnd_srd_2024.attacks_per_turn(fighter) == 2


def test_a_natural_twenty_is_a_critical_hit_and_doubles_dice(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    result = tools.player_attack("Foe", "Greatsword", attack_roll=20, damage_roll=7)
    assert result["critical"] is True
    assert result["raw"] > 7  # the second set of 2d6 dice is added


def test_spell_attacks_use_the_spell_attack_bonus(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    from .test_dnd_srd import _prepared_wizard

    client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.create_location("Cave", "A low cave.")
    tools.create_monster("Foe", "Cave", template="monster-goblin-warrior")
    result = dnd_srd_2024.spell_attack("Wren", "fire-bolt", "Foe", ctx, roll=15)
    assert result["bonus"] == 5  # Int 16 (+3) and PB +2
    assert result["total"] == 20
    assert result["hit"] is True
    with Session(app.state.engine) as db:
        kinds = db.exec(
            select(Event.kind).where(Event.campaign_id == campaign_id).order_by(Event.id)
        ).all()
    assert "combat.spell_attack" in kinds


def test_healing_and_stabilizing_recover_a_dying_character(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, _ctx = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.harm_character("Rook", "hp", 99, "a test fall")
    stabilized = tools.stabilize("Rook", "a companion binds the wound")
    assert stabilized["stabilized"] is True
    assert character_named(app, campaign_id, "Rook").stable
    healed = tools.heal_character("Rook", 5)
    assert healed["recovered"] is True
    assert not character_named(app, campaign_id, "Rook").defeated
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert state.characters["rook"].stable is False


def test_class_save_proficiencies_and_hazard_dc(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    save = dnd_srd_2024.roll_save("Rook", "con", 12, "a test", ctx, roll=10)
    assert save["bonus"] == 4  # Constitution +2 and Fighter proficiency +2
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.create_location("Cave", "A low cave.")
    tools.create_monster("Goblin", "Cave", template="monster-goblin-warrior")
    hazard = dnd_srd_2024.resolve_hazard(
        "a falling rock",
        ["Goblin"],
        ctx,
        save="dex",
        dc=10,
        damage="1d6",
        half_on_save=True,
    )
    assert hazard["results"][0]["save"]["bonus"] == 2
    assert hazard["results"][0]["save"]["dc"] == 10
    assert hazard["results"][0]["damage"] >= 1


def test_the_bestiary_keeps_multiattack_skills_and_cr(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, _ctx = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.create_location("Cave", "A low cave.")
    tools.create_monster("Bear", "Cave", template="monster-brown-bear")
    block = tools.get_statblock("Bear")["statblock"]
    assert "Attacks per action: 2" in block
    tools.create_monster("Goblin", "Cave", template="monster-goblin-warrior")
    goblin = character_named(app, campaign_id, "Goblin")
    assert goblin.skills["stealth"] == 6
