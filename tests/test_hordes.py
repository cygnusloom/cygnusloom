"""Hordes: the PbtA zombie-survival ruleset and its engine additions."""

from __future__ import annotations

from cygnusloom.services.compendium import compendium_view
from cygnusloom.services.events import MoveMade, decode, events_since, name_key
from tests.conftest import bundled_library
from tests.factories import (
    character_named,
    close_opening,
    create_campaign_direct,
    create_character_direct,
    db,
    rules_for,
    signed_in_client,
    tools_for,
)

WORLD = "unwritten-apocalypse"
RULE_SET = "hordes"
FIELDS = {
    "template-playbook": "soldier",
    "stat-grit": "1",
    "stat-reflexes": "2",
    "stat-wits": "1",
    "stat-presence": "0",
    "stat-know_how": "-1",
    "field-drive-freeform": "Keep the shelter alive through the winter.",
    "field-burden-freeform": "A promise to a sister who never came home.",
    "field-scar-freeform": "A cut across the left hand.",
    "field-look-freeform": "Mud on the boots and a sheriff's badge in the pocket.",
    "field-background-freeform": "County sheriff before the fall.",
    "field-shelter_duty-freeform": "Watch the gate.",
    "field-personal_item-freeform": "A sheriff's badge.",
}


def _campaign(client_factory, *, name: str = "Ada", fields=FIELDS):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(
        client, rule_set=RULE_SET, world_template=WORLD
    )
    create_character_direct(client, campaign_id, name=name, fields=fields)
    close_opening(client, campaign_id)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    return client, campaign_id, tools


def test_hordes_loads_and_hides_turn_based_tools():
    rules = rules_for(RULE_SET)
    assert rules.combat is not None
    assert rules.combat.mode == "moves"
    assert rules.capabilities.combat_mode == "moves"
    for tool in (
        "start_encounter",
        "stage_encounter",
        "end_encounter",
        "attack",
        "player_attack",
        "resolve_attacks",
        "roll_initiative",
    ):
        assert tool not in rules.tool_calls
    assert "roll_move" in rules.tool_calls
    assert "apply_harm" in rules.tool_calls
    assert rules.counters["harm"].thresholds.keys() == {4, 6}
    assert rules.campaign_clocks["noise"].segments == 6


def test_hordes_compendium_has_its_new_sections():
    compendium = compendium_view(bundled_library(), RULE_SET, WORLD)
    sections = {section.id: section for section in compendium.sections}
    assert len(sections["clocks"].entries) == 7
    assert len(sections["threats"].entries) == 20
    assert len(sections["vehicles"].entries) == 9
    assert len(sections["recipes"].entries) == 15
    assert len(sections["improvements"].entries) == 16
    assert len(sections["sites"].entries) == 20
    threat = next(entry for entry in sections["threats"].entries if entry.name == "Horde")
    assert "Harm capacity: 3" in threat.text
    site = next(entry for entry in sections["sites"].entries if entry.name == "Cybernetics Lab")
    assert "Find: hidden" in site.text


def test_a_hordes_character_gets_a_playbook_kit_and_loaded_firearm(client_factory):
    client, campaign_id, _tools = _campaign(client_factory)

    character = character_named(client.app, campaign_id, "Ada")
    assert character.stats["grit"] == 2
    assert character.skills["firearms"] == 1
    assert "radio-operation" in character.features

    pistol = next(item for item in character.items if name_key(item.name) == "pistol")
    assert pistol.capacity == 12
    assert pistol.loaded == 12
    assert pistol.caliber == "9mm"


def test_a_hordes_move_records_its_band_and_practice(client_factory):
    client, campaign_id, tools = _campaign(client_factory)

    result = tools.roll_move("assess", "Ada", roll=7)
    assert result["band"] == "7-9"

    with db(client.app) as session:
        moves = [
            decode(event)
            for event in events_since(session, campaign_id)
            if event.kind == MoveMade.kind
        ]
    assert moves
    assert moves[-1].band == "7-9"


def test_shoot_harms_a_threat_spends_ammo_and_raises_noise(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    tools.create_threat("walker", name="the walker")

    result = tools.roll_move(
        "shoot", "Ada", roll=8, target="the walker", weapon="pistol"
    )
    assert result["band"] == "10+"

    threat = tools.get_threat("the walker")
    assert threat["harm"] == 0
    assert threat["broken"] is True
    pistol = next(
        item
        for item in character_named(client.app, campaign_id, "Ada").items
        if name_key(item.name) == "pistol"
    )
    assert pistol.loaded == 11
    clocks = tools.list_clocks()["clocks"]
    assert any(clock["id"] == "noise" and clock["filled"] >= 1 for clock in clocks)


def test_harm_crosses_the_wounded_and_dying_thresholds(client_factory):
    client, campaign_id, tools = _campaign(client_factory)

    tools.apply_harm("Ada", 4, "a deep bite", armor=0)
    character = character_named(client.app, campaign_id, "Ada")
    assert character.counters["harm"] == 4
    assert "wounded" in character.conditions

    tools.apply_harm("Ada", 2, "a second bite", armor=0)
    character = character_named(client.app, campaign_id, "Ada")
    assert character.counters["harm"] == 6
    assert "dying" in character.conditions
    assert character.dying_hours == 24


def test_clocks_threats_and_vehicles(client_factory):
    _client, _campaign_id, tools = _campaign(client_factory)

    tools.start_clock("noise")
    tools.advance_clock("noise", 3, "a gunshot")
    assert any(
        clock["id"] == "noise" and clock["filled"] == 3
        for clock in tools.list_clocks()["clocks"]
    )

    tools.create_threat("horde", name="the tide", magnitude=2)
    split = tools.split_horde("the tide", zombie="walker", count=2)
    assert split["zombies"]
    assert len(tools.list_threats()["threats"]) == 3

    tools.create_vehicle("pickup", name="the blue pickup")
    damaged = tools.damage_vehicle("the blue pickup", 2)
    assert damaged["condition"] < damaged["condition_max"]
    refuelled = tools.refuel_vehicle("the blue pickup", 1)
    assert refuelled["fuel"] >= 1


def test_crafting_and_shelter_improvements(client_factory):
    _client, _campaign_id, tools = _campaign(client_factory)
    for item in ("toolkit", "9mm-components", "parts"):
        tools.give_item("Ada", item)

    crafted = tools.craft("Ada", "reload-9mm", 8)
    assert crafted["success"] is True
    assert crafted["output"] == "9mm-rounds"

    built = tools.improve_site("The Shelter", "barricade", "Ada", 8)
    assert built["completed"] is True
    assert any(
        improvement["improvement"] == "barricade"
        for improvement in tools.list_improvements("The Shelter")["improvements"]
    )


def test_a_safe_night_rest_clears_one_harm(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    tools.apply_harm("Ada", 4, "a deep bite", armor=0)

    tools.rest("night", reason="a safe night behind the barricade")

    assert character_named(client.app, campaign_id, "Ada").counters["harm"] == 3


def test_a_hordes_fight_creates_no_encounter_or_turn_events(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    tools.create_threat("walker", name="the walker")
    tools.roll_move("shoot", "Ada", roll=8, target="the walker", weapon="pistol")

    with db(client.app) as session:
        kinds = {row.kind for row in events_since(session, campaign_id)}
    assert "move.made" in kinds
    assert "threat.harmed" in kinds
    assert "campaign_clock.advanced" in kinds
    assert not any(
        kind.startswith(("encounter.", "initiative."))
        or kind in {"combat.struck", "combat.save_rolled", "combat.group_break_resolved"}
        for kind in kinds
    )


def test_hordes_names_are_modern_for_players_and_npcs():
    rules = rules_for(RULE_SET)
    survivors = [option.value for option in rules.tables["survivor-names"]]
    names = rules.dice_tables["name"]

    assert names.dice == "1d20"
    full_names = [row.result for row in names.rows]
    assert len(full_names) == 20
    assert len(set(full_names)) == 20
    assert all(len(name.split()) == 2 for name in full_names)

    assert len(survivors) >= 36
    assert len(set(survivors)) == len(survivors)
    for name in ("Marcus", "Danielle", "Priya", "Miguel", "Grace"):
        assert name in survivors
    for name in ("Kestrel", "Rook", "Sable", "Briar", "Wren"):
        assert name not in survivors

    first_names = {name.split()[0] for name in full_names}
    assert first_names <= set(survivors)


def test_hordes_tells_the_gm_to_roll_modern_names():
    prompts = {prompt.id: prompt for prompt in rules_for(RULE_SET).prompt_fragments}

    assert "people_and_names" in prompts
    assert "roll_tables" in prompts["people_and_names"].text
    assert "name table" in prompts["people_and_names"].text
