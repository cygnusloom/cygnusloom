"""D&D play through the engine: initiative, combat math, recovery, and content.

These tests exercise the paths a live GM uses, rather than only the rule module's
bookkeeping: an encounter with real participants, the printed monster fields, and the
advancement page after an experience award.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Event
from cygnusloom.rules import dnd_srd_2024
from cygnusloom.services import encounters
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import ChatGM, EncounterTurnEnded, decode, load_state, record

from .factories import (
    Rolls,
    character_named,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    rules_for,
    signed_in_client,
    tools_for,
)
from .test_combat import _struck_events

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

WIZARD_FIELDS = {
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

BARBARIAN_FIELDS = {
    **{key: value for key, value in FIGHTER_FIELDS.items() if not key.startswith("pick-feature")},
    "template-class": "barbarian",
    "pick-skill-class_skills-0": "nature",
    "pick-skill-class_skills-1": "survival",
}

ROGUE_FIELDS = {
    **{key: value for key, value in FIGHTER_FIELDS.items() if not key.startswith("pick-feature")},
    "template-class": "rogue",
    "pick-skill-class_skills-0": "stealth",
    "pick-skill-class_skills-1": "sleight_of_hand",
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


def test_the_character_sheet_shows_only_the_characters_relevant_resources(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ctx = _campaign(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    panel = page.partition('id="panel-character"')[2].partition('id="panel-inventory"')[0]

    assert 'class="sheet-hp"' in panel
    assert 'class="sheet-ac"' in panel
    assert "STR" in panel and 'title="Strength"' in panel
    assert "Hit Point Dice" in panel
    assert "Second Wind" in panel
    assert "Level 1 Spell Slots" not in panel
    assert "Pact Magic Slots" not in panel
    assert "Death Save Failures" not in panel
    assert "Soldier: Ability Scores" not in panel


def test_the_sheet_groups_spells_by_slot_level_and_marks_prepared_ones(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    from .test_dnd_srd import _prepared_wizard

    client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.prepare_spells("Wren", ["magic-missile"], ctx)
    page = client.get(f"/campaigns/{campaign_id}").text
    panel = page.partition('id="panel-character"')[2].partition('id="panel-inventory"')[0]

    assert 'id="sheet-spells"' in panel
    assert "Cantrips" in panel
    assert "Level 1" in panel and "2 slots" in panel
    assert "Magic Missile" in panel and "prepared" in panel


def test_the_sheet_shows_tool_training(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id, _ctx = _campaign(client_factory, fields=ROGUE_FIELDS)
    page = client.get(f"/campaigns/{campaign_id}").text
    panel = page.partition('id="panel-character"')[2].partition('id="panel-inventory"')[0]

    assert 'id="sheet-tools"' in panel
    assert "Thieves&#39; Tools" in panel


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


def test_the_initiative_log_shows_each_d20_and_total_not_only_the_bonus(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    _fight(client, campaign_id)
    # The fighter rolls a 1 and the ogre a 20, so the ogre acts first despite its -1 bonus.
    ctx = replace(ctx, world=replace(ctx.world, rng=Rolls(1, 20)))

    result = dnd_srd_2024.roll_initiative(ctx)

    assert result["order"] == ["Foe", "Rook"]
    with Session(app.state.engine) as db:
        row = db.exec(
            select(Event)
            .where(Event.campaign_id == campaign_id, Event.kind == "encounter.initiative_settled")
            .order_by(Event.id)
        ).all()[-1]
    text = decode(row).render()
    # The fighter's Dexterity is rolled at creation, so read back its bonus rather than
    # assuming one; the point is that the face and the total show beside it.
    rook = next(entry for entry in result["rolls"] if entry["name"] == "Rook")
    foe = next(entry for entry in result["rolls"] if entry["name"] == "Foe")
    assert rook["roll"] == 1 and foe["roll"] == 20
    assert f"Rook: 1d20 1 {rook['bonus']:+d} Initiative = {rook['total']}" in text
    assert f"Foe: 1d20 20 {foe['bonus']:+d} Initiative = {foe['total']}" in text
    assert "Initiative is set: Foe, Rook." in text


def test_an_old_individual_initiative_record_still_shows_its_d20() -> None:
    from cygnusloom.services.events import InitiativeSettled

    text = InitiativeSettled(
        order=["Aelric", "Nib"],
        individual=True,
        rolls=[
            {"name": "Aelric", "roll": 7, "bonus": 2, "total": 9},
            {"name": "Nib", "roll": 19, "bonus": 1, "total": 20},
        ],
    ).render()
    assert "Aelric: 1d20 7 +2 bonus = 9" in text
    assert "Nib: 1d20 19 +1 bonus = 20" in text


def test_a_gm_played_hirelings_spell_waits_for_its_turn(
    client_factory: Callable[[], TestClient],
) -> None:
    """A hireling whose turns the GM plays acts only on its own turn. Before the player presses
    Start, the engine refuses its save, its spell, and the harm the spell would cause. Once the
    turn begins they all resolve, and force lets the story break the order."""
    from .test_hireling_turns import _set_flag

    client, campaign_id, ctx = _campaign(client_factory)
    app = client.app
    tools = tools_for(app, campaign_id, rule_set="dnd-srd-2024")
    tools.create_location("Cave", "A low cave.")
    tools.move_actor("Rook", "Cave")
    tools.hire_hireling(
        "Hesta",
        "Rook",
        0,
        "A dwarf cleric, steady with a mace and a bright holy symbol.",
        character={
            "templates": {"species": "human", "background": "soldier", "class": "cleric"},
            "stats": {"str": 12, "dex": 10, "con": 13, "int": 10, "wis": 15, "cha": 11},
            "feature_choices": {"background-asi-soldier:scores": ["str", "dex", "con"]},
        },
    )
    _set_flag(app, campaign_id, True)
    dnd_srd_2024.learn_spell("Hesta", "spell-sacred-flame", ctx)
    tools.create_encounter_map("Cave Yard", "Cave", 12, 12, "Open stone.")
    tools.start_encounter("Cave Yard")
    tools.create_monster("Foe", "Cave", template="monster-ogre")
    for name in ("Hesta", "Rook", "Foe"):
        assert tools.add_participant(name)["added"] == name
    # Hesta wins initiative and is first in the order, with nobody taking a turn.
    initiative = replace(ctx, world=replace(ctx.world, rng=Rolls(20, 1, 1)))
    assert dnd_srd_2024.roll_initiative(initiative)["order"][0] == "Hesta"

    with pytest.raises(InvalidInput, match="no one is taking a turn"):
        dnd_srd_2024.cast_spell("Hesta", "spell-sacred-flame", ctx)
    with pytest.raises(InvalidInput, match="no one is taking a turn"):
        dnd_srd_2024.roll_save("Foe", "dex", 15, "Hesta's Sacred Flame", ctx)
    with pytest.raises(InvalidInput, match="no one is taking a turn"):
        tools.harm_character("Foe", "hp", 1, "Hesta's Sacred Flame")
    with pytest.raises(InvalidInput, match="no one is taking a turn"):
        tools.player_attack("Foe", "Mace", character="Hesta")

    # The GM may force an effect outside the order when the story needs it.
    forced = tools.harm_character("Foe", "hp", 1, "Hesta's Sacred Flame", force=True)
    assert forced["taken"] == 1
    saved = dnd_srd_2024.roll_save("Foe", "dex", 15, "Hesta's Sacred Flame", ctx, force=True)
    assert saved["character"] == "Foe"

    # The player presses Start, and now her turn is running.
    with Session(app.state.engine) as db:
        encounters.begin_turn(db, campaign_id, "Hesta")
    cast = dnd_srd_2024.cast_spell("Hesta", "spell-sacred-flame", ctx)
    assert cast["spell"] == "Sacred Flame"
    assert dnd_srd_2024.roll_save("Foe", "dex", 15, "Hesta's Sacred Flame", ctx)["character"] == (
        "Foe"
    )
    assert tools.harm_character("Foe", "hp", 1, "Hesta's Sacred Flame")["taken"] == 1


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


def test_a_versatile_weapon_uses_its_larger_die_in_two_hands(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    tools.give_item("Rook", "Longsword")
    tools.ready_item("Rook", "Longsword")
    assert tools.attack_dice("Longsword", two_handed=True) == "1d10"
    result = tools.player_attack("Foe", "Longsword", attack_roll=10, damage_roll=5, two_handed=True)
    assert result["target"] == "Foe"


def test_a_weapon_the_character_is_not_trained_with_loses_the_proficiency_bonus(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory, fields=WIZARD_FIELDS)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    tools.give_item("Rook", "Longsword")
    tools.ready_item("Rook", "Longsword")

    tools.player_attack("Foe", "Longsword", attack_roll=10, damage_roll=5)
    event = _struck_events(app, campaign_id)[-1]
    assert event.hit_breakdown is not None
    modifiers = {item["label"]: item["value"] for item in event.hit_breakdown["modifiers"]}
    assert modifiers["not proficient"] == -2


def test_a_trained_weapon_keeps_the_proficiency_bonus(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    tools.give_item("Rook", "Longsword")
    tools.ready_item("Rook", "Longsword")

    tools.player_attack("Foe", "Longsword", attack_roll=10, damage_roll=5)
    event = _struck_events(app, campaign_id)[-1]
    assert event.hit_breakdown is not None
    modifiers = {item["label"]: item["value"] for item in event.hit_breakdown["modifiers"]}
    assert "not proficient" not in modifiers


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


def test_dnd_turn_resources_include_actions_bonus_actions_and_reactions(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.create_location("Cave", "A low cave.")
    tools.create_encounter_map("Cave Yard", "Cave", 12, 12, "Open stone.")
    tools.start_encounter("Cave Yard")
    tools.create_monster("Foe", "Cave", template="monster-ogre")
    tools.add_participant("Rook")
    tools.add_participant("Foe")
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    state = _state(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.actions_used == 0
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Actions left" in page
    assert "Bonus actions left" in page
    assert "Reactions left" in page
    spent = tools.use_turn_resource("Rook", "action", "Dodge")
    assert spent["remaining"] == 0
    assert _state(app, campaign_id).encounter.actions_used == 1
    with pytest.raises(InvalidInput, match="no action left"):
        tools.player_attack("Foe", "Greatsword")
    with pytest.raises(InvalidInput, match="no action left"):
        tools.use_turn_resource("Rook", "action", "again")
    tools.use_turn_resource("Rook", "bonus_action", "Second Wind")
    assert _state(app, campaign_id).encounter.bonus_actions_used == 1
    tools.use_turn_resource("Rook", "reaction", "an opportunity attack")
    assert _state(app, campaign_id).encounter.reactions_used == {"rook": 1}


def test_a_reaction_is_spent_on_another_creatures_turn(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "template-species": "goliath",
        "pick-feature-goliath-lineage-0": "lineage-goliath-stone",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Foe")
    before = character_named(app, campaign_id, "Rook")
    used = dnd_srd_2024.use_feature("Rook", "lineage-goliath-stone", ctx)
    assert used["turn_resource"] == "reaction"
    assert used["remaining"] == before.counters["giant_ancestry"] - 1
    encounter = _state(app, campaign_id).encounter
    assert encounter is not None and encounter.reactions_used == {"rook": 1}
    # A second reaction in the same round is refused without spending an ancestry use.
    with pytest.raises(InvalidInput, match="no reaction left"):
        dnd_srd_2024.use_feature("Rook", "lineage-goliath-stone", ctx)
    after = character_named(app, campaign_id, "Rook")
    assert after.counters["giant_ancestry"] == used["remaining"]


def test_each_creature_has_its_own_reaction_each_round(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Foe")
    tools.use_turn_resource("Rook", "reaction", "Stone's Endurance")
    tools.use_turn_resource("Foe", "reaction", "an opportunity attack")
    state = _state(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.reactions_used == {"rook": 1, "foe": 1}
    with pytest.raises(InvalidInput, match="no reaction left"):
        tools.use_turn_resource("Rook", "reaction", "a second reaction")
    with Session(app.state.engine) as db:
        for name in ("Rook", "Foe"):
            record(db, campaign_id, EncounterTurnEnded(name=name))
        db.commit()
    new_round = _state(app, campaign_id).encounter
    assert new_round is not None and new_round.round == 2
    assert new_round.reactions_used == {}
    tools.use_turn_resource("Rook", "reaction", "a new round's reaction")
    assert _state(app, campaign_id).encounter.reactions_used == {"rook": 1}


def test_a_monster_attack_log_names_its_printed_attack_bonus(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    from cygnusloom.services.events import _decode_event

    client, campaign_id, ctx = _campaign(client_factory)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    tools.attack("Foe", "Greatclub", "Rook", count=1, force=True)
    with Session(app.state.engine) as db:
        row = db.exec(
            select(Event)
            .where(Event.campaign_id == campaign_id, Event.kind == "combat.struck")
            .order_by(Event.id.desc())
        ).first()
    assert row is not None
    text = _decode_event(row.kind, row.payload).render()
    assert "+6 Greatclub" in text
    assert "Proficiency Bonus" not in text


def test_an_unconscious_character_cannot_use_features_or_resources(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.harm_character("Rook", "hp", 99, "a test fall")
    assert character_named(app, campaign_id, "Rook").defeated
    with pytest.raises(InvalidInput, match="unconscious"):
        dnd_srd_2024.use_feature("Rook", "class-fighter-second-wind", ctx)
    with pytest.raises(InvalidInput, match="unconscious"):
        dnd_srd_2024.use_class_resource("Rook", "second_wind", ctx)


def test_an_unconscious_caster_cannot_cast_a_spell(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    from .test_dnd_srd import _prepared_wizard

    client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.harm_character("Wren", "hp", 99, "a test fall")
    with pytest.raises(InvalidInput, match="unconscious"):
        dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx)


def test_rage_spends_a_use_and_sets_the_raging_mark(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory, fields=BARBARIAN_FIELDS)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    result = dnd_srd_2024.use_feature("Rook", "class-barbarian-rage", ctx)
    assert result["remaining"] == 1
    pc = character_named(app, campaign_id, "Rook")
    assert pc.marks["raging"] is True
    assert _state(app, campaign_id).encounter.bonus_actions_used == 1


def test_using_a_class_feature_spends_its_resource_and_turn_action(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    before = character_named(app, campaign_id, "Rook")
    result = dnd_srd_2024.use_feature("Rook", "class-fighter-second-wind", ctx)
    assert result["resource"] == "second_wind"
    assert result["remaining"] == before.counters["second_wind"] - 1
    encounter = _state(app, campaign_id).encounter
    assert encounter is not None and encounter.bonus_actions_used == 1


def test_weapon_mastery_unlocks_graze_on_a_miss(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-greatsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-longsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-dagger",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    result = tools.player_attack("Foe", "Greatsword", attack_roll=1)
    assert result["hit"] is False
    assert result["graze"] == 3  # Strength 16
    assert result["harm"]["hp"] == 3


@pytest.mark.parametrize(
    ("weapon", "condition"),
    [("Flail", "sapped"), ("Javelin", "slowed")],
)
def test_sap_and_slow_end_at_the_start_of_the_attackers_next_turn(
    app: FastAPI,
    client_factory: Callable[[], TestClient],
    weapon: str,
    condition: str,
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-flail",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-javelin",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-longsword",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    tools.player_attack("Foe", weapon, attack_roll=20, damage_roll=3)
    foe = character_named(app, campaign_id, "Foe")
    assert condition in foe.conditions
    assert foe.condition_until[condition] == "Rook"

    # The target's own turn does not end it; the attacker's next turn does.
    with Session(app.state.engine) as db:
        record(db, campaign_id, EncounterTurnEnded(name="Rook"))
        db.commit()
    with Session(app.state.engine) as db:
        encounters.begin_turn(db, campaign_id, "Foe")
    assert condition in character_named(app, campaign_id, "Foe").conditions
    with Session(app.state.engine) as db:
        record(db, campaign_id, ChatGM(content="The foe's turn plays out.", model="test"))
        record(db, campaign_id, EncounterTurnEnded(name="Foe"))
        db.commit()
    with Session(app.state.engine) as db:
        encounters.begin_turn(db, campaign_id, "Rook")

    foe = character_named(app, campaign_id, "Foe")
    assert condition not in foe.conditions
    assert condition not in foe.condition_until


def test_tactical_master_can_replace_a_weapons_mastery(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-longsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-greatsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-dagger",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    rules = rules_for("dnd-srd-2024")
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    level_up_service.debug_grant(
        client.app.state.engine,
        campaign_id,
        rules,
        world,
        "feature",
        "Rook",
        feature="class-fighter-tactical-master",
    )
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    tools.give_item("Rook", "Longsword")
    tools.ready_item("Rook", "Longsword")

    tools.player_attack("Foe", "Longsword", attack_roll=20, damage_roll=5, mastery="slow")

    foe = character_named(app, campaign_id, "Foe")
    assert "slowed" in foe.conditions
    assert "sapped" not in foe.conditions


def test_a_fighter_without_tactical_master_cannot_replace_a_mastery(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-longsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-greatsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-dagger",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")
    tools.give_item("Rook", "Longsword")
    tools.ready_item("Rook", "Longsword")

    with pytest.raises(InvalidInput, match="cannot replace Longsword's mastery"):
        tools.player_attack("Foe", "Longsword", mastery="slow")
    with pytest.raises(InvalidInput, match="no weapon mastery property named"):
        tools.player_attack("Foe", "Longsword", mastery="banana")


def test_cleave_hits_a_second_creature_without_the_ability_modifier(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-greataxe",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-longsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-dagger",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    tools.create_monster("Second", "Cave", template="monster-goblin-warrior")
    tools.add_participant("Second")
    tools.give_item("Rook", "Greataxe")
    tools.ready_item("Rook", "Greataxe")
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe", "Second"], current="Rook")
    tools.place_participant("Rook", 1, 1)
    tools.place_participant("Foe", 2, 1)
    tools.place_participant("Second", 2, 2)

    tools.player_attack("Foe", "Greataxe", attack_roll=15, damage_roll=5)
    result = tools.player_attack(
        "Second",
        "Greataxe",
        attack_roll=15,
        damage_roll=5,
        extra="cleave",
        extra_from="Foe",
    )

    assert result["target"] == "Second"
    assert result["raw"] == 5  # Strength 16 is left out of the second target's damage
    with pytest.raises(InvalidInput, match="already made a cleave extra attack"):
        tools.player_attack(
            "Second",
            "Greataxe",
            attack_roll=15,
            damage_roll=5,
            extra="cleave",
            extra_from="Foe",
        )


def test_cleave_needs_the_first_attack_to_hit(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-greataxe",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-longsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-dagger",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    tools.create_monster("Second", "Cave", template="monster-goblin-warrior")
    tools.add_participant("Second")
    tools.give_item("Rook", "Greataxe")
    tools.ready_item("Rook", "Greataxe")
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe", "Second"], current="Rook")

    tools.player_attack("Foe", "Greataxe", attack_roll=1, damage_roll=5)
    with pytest.raises(InvalidInput, match="has not hit with Greataxe"):
        tools.player_attack(
            "Second",
            "Greataxe",
            attack_roll=15,
            damage_roll=5,
            extra="cleave",
            extra_from="Foe",
        )


def test_cleave_refuses_a_second_creature_more_than_five_feet_from_the_first(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-greataxe",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-longsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-dagger",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    tools.create_monster("Second", "Cave", template="monster-goblin-warrior")
    tools.add_participant("Second")
    tools.give_item("Rook", "Greataxe")
    tools.ready_item("Rook", "Greataxe")
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe", "Second"], current="Rook")
    tools.place_participant("Rook", 1, 1)
    tools.place_participant("Foe", 2, 1)
    tools.place_participant("Second", 5, 5)

    tools.player_attack("Foe", "Greataxe", attack_roll=15, damage_roll=5)
    with pytest.raises(InvalidInput, match="within 5 feet of the first"):
        tools.player_attack(
            "Second",
            "Greataxe",
            attack_roll=15,
            damage_roll=5,
            extra="cleave",
            extra_from="Foe",
        )


def test_a_light_weapon_extra_attack_with_nick_needs_no_bonus_action(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-dagger",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-shortsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-greataxe",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    tools.give_item("Rook", "Dagger")
    tools.give_item("Rook", "Shortsword")
    tools.ready_item("Rook", "Dagger")
    tools.ready_item("Rook", "Shortsword")
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")

    tools.player_attack("Foe", "Shortsword", attack_roll=15, damage_roll=4)
    result = tools.player_attack("Foe", "Dagger", attack_roll=15, damage_roll=4, extra="light")

    assert result["raw"] == 4  # Nick leaves out the positive ability modifier
    encounter = _state(app, campaign_id).encounter
    assert encounter is not None
    assert encounter.bonus_actions_used == 0
    assert encounter.extra_attacks == ["light"]


def test_a_light_weapon_extra_attack_without_nick_spends_a_bonus_action(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-dagger",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-shortsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-greataxe",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    tools.give_item("Rook", "Dagger")
    tools.give_item("Rook", "Shortsword")
    tools.ready_item("Rook", "Dagger")
    tools.ready_item("Rook", "Shortsword")
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")

    tools.player_attack("Foe", "Dagger", attack_roll=15, damage_roll=4)
    tools.player_attack("Foe", "Shortsword", attack_roll=15, damage_roll=4, extra="light")

    encounter = _state(app, campaign_id).encounter
    assert encounter is not None
    assert encounter.bonus_actions_used == 1


def test_a_light_weapon_extra_attack_needs_a_different_light_weapon(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    fields = {
        **FIGHTER_FIELDS,
        "feature-choice-class-fighter-weapon-mastery-weapons-0": "weapon-dagger",
        "feature-choice-class-fighter-weapon-mastery-weapons-1": "weapon-shortsword",
        "feature-choice-class-fighter-weapon-mastery-weapons-2": "weapon-greataxe",
    }
    client, campaign_id, ctx = _campaign(client_factory, fields=fields)
    tools, _ = _fight(client, campaign_id)
    tools.give_item("Rook", "Dagger")
    tools.ready_item("Rook", "Dagger")
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Rook", "Foe"], current="Rook")

    tools.player_attack("Foe", "Dagger", attack_roll=15, damage_roll=4)
    with pytest.raises(InvalidInput, match="different Light weapon"):
        tools.player_attack("Foe", "Dagger", attack_roll=15, damage_roll=4, extra="light")


def test_casting_a_spell_spends_the_casters_action(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _campaign(client_factory, name="Wren", fields=WIZARD_FIELDS)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.create_location("Cave", "A low cave.")
    tools.create_encounter_map("Cave Yard", "Cave", 12, 12, "Open stone.")
    tools.start_encounter("Cave Yard")
    tools.create_monster("Foe", "Cave", template="monster-ogre")
    tools.add_participant("Wren")
    tools.add_participant("Foe")
    dnd_srd_2024.roll_initiative(ctx)
    tools.adjust_encounter_order(["Wren", "Foe"], current="Wren")
    dnd_srd_2024.learn_spell("Wren", "magic-missile", ctx)
    dnd_srd_2024.prepare_spells("Wren", ["magic-missile"], ctx)
    result = dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx)
    assert result["slot_level"] == 1
    encounter = _state(app, campaign_id).encounter
    assert encounter is not None and encounter.actions_used == 1
    with pytest.raises(InvalidInput, match="no action left"):
        dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx)


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
    # A successful save halves 1d6, and 1 halved rounds down to 0.
    assert 0 <= hazard["results"][0]["damage"] <= 6


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


def test_the_gm_rolls_feature_dice_with_the_engine(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    from cygnusloom.services.events import _decode_event

    client, campaign_id, _ctx = _campaign(client_factory)
    tools = replace(tools_for(client.app, campaign_id, rule_set="dnd-srd-2024"), rng=Rolls(7))
    rolled = tools.roll_dice("1d12", "Stone's Endurance")
    assert rolled == {
        "dice": "1d12",
        "values": [7],
        "total": 7,
        "reason": "Stone's Endurance",
    }
    with Session(app.state.engine) as db:
        row = db.exec(
            select(Event)
            .where(Event.campaign_id == campaign_id, Event.kind == "dice.rolled")
            .order_by(Event.id.desc())
        ).first()
    assert row is not None
    assert _decode_event(row.kind, row.payload).render() == (
        "Rolled 1d12: 7 = 7. (Stone's Endurance)"
    )
    with pytest.raises(InvalidInput, match="not a dice formula"):
        tools.roll_dice("banana")
