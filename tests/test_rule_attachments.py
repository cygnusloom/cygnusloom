from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary
from cygnusloom.services.events import Item, ItemAdded, LocationAdded, record
from cygnusloom.services.prompts import system_parts
from cygnusloom.services.views import present_rules_text, world_state_text
from tests.conftest import content_library
from tests.test_character_creation import _cairn_campaign
from tests.test_combat import CAIRN, PC, YARD, _fight
from tests.test_content import _core, _rule_set, _world, _write
from tests.test_world import _db, _state

LIBRARY = content_library()
TWIN_LAKES = LIBRARY.world_templates["twin-lakes"]
VUK = "Vuk"
CHOKING = "vuk-choking-hold"
TEXT = {CHOKING: "The Ash Phantom chokes with its hand."}


def _with_vuk_in_the_yard(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory, twin_lakes=True)
    state = _state(app, campaign_id)
    pc = state.pc()
    assert pc is not None and pc.location is not None
    vuk = state.named(VUK)
    assert vuk is not None
    vuk.location = pc.location
    return app, campaign_id, state, vuk


def _story_only_world(**npc: object) -> list[dict[str, object]]:
    glade = {"type": "location", "id": "glade", "name": "Glade", "description": "A glade."}
    bo = {"type": "npc", "id": "bo", "name": "Bo", "location": "glade", "description": "A bo."}
    return [*_world(), glade, {**bo, **npc}]


def test_twin_lakes_attaches_vuks_choking_hold():
    vuk = next(n for n in TWIN_LAKES.npcs if n.name == VUK)
    assert vuk.rules == (CHOKING,)


def test_seeding_gives_vuk_her_rules(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    vuk = _state(client.app, campaign_id).named(VUK)
    assert vuk is not None
    assert list(vuk.rules) == [CHOKING]


def test_twin_lakes_monsters_attach_their_module_rules():
    monsters = LIBRARY.world_content("twin-lakes", "cairn2e").monsters
    assert monsters["night-cat"].rules == ("night-cat-attack", "night-cat-critical-damage")
    assert monsters["suraru-imperator"].rules == ("suraru-imperator-critical-damage",)


def test_a_monster_made_from_a_statblock_brings_its_rules(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    content = LIBRARY.world_content("twin-lakes", "cairn2e")
    # The app merges the world's monsters over the rule set's, as campaigns.py does.
    tools = replace(tools, monsters={**tools.monsters, **content.monsters})
    tools.create_monster("Night Cat", YARD)
    state = _state(app, campaign_id)
    cat = state.named("Night Cat")
    assert cat is not None
    assert list(cat.rules) == ["night-cat-attack", "night-cat-critical-damage"]
    text = present_rules_text(state, {**LIBRARY.rule_sets["cairn2e"].rules, **content.rules})
    assert "- night-cat-attack (from Night Cat):" in text


def test_a_standing_npc_here_brings_its_rules(client_factory: Callable[[], TestClient]):
    _, _, state, _ = _with_vuk_in_the_yard(client_factory)
    text = present_rules_text(state, TEXT)
    assert f"- {CHOKING} (from {VUK}): {TEXT[CHOKING]}" in text


def test_an_npc_elsewhere_brings_no_rules(client_factory: Callable[[], TestClient]):
    _, _, state, vuk = _with_vuk_in_the_yard(client_factory)
    vuk.location = "Somewhere else"
    assert present_rules_text(state, TEXT) == ""


def test_a_defeated_npc_still_here_keeps_its_rules(client_factory: Callable[[], TestClient]):
    _, _, state, vuk = _with_vuk_in_the_yard(client_factory)
    vuk.defeated = True
    assert "vuk-choking-hold (from Vuk)" in present_rules_text(state, TEXT)


def test_an_item_carried_brings_its_rules(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
    with _db(app) as db:
        record(
            db,
            campaign_id,
            ItemAdded(
                name=PC,
                item="Charm",
                size="petty",
                slots=0,
                kind_of_item="",
                cause="",
                rules=[CHOKING],
            ),
        )
        db.commit()
    state = _state(app, campaign_id)
    charm = next(i for i in state.pc().items if i.name == "Charm")
    assert charm.rules == (CHOKING,)
    text = present_rules_text(state, TEXT)
    assert f"- {CHOKING} (from Charm): {TEXT[CHOKING]}" in text


def test_a_rule_on_an_npc_and_an_item_is_listed_once_with_both_sources(
    client_factory: Callable[[], TestClient],
):
    _, _, state, _ = _with_vuk_in_the_yard(client_factory)
    pc = state.pc()
    assert pc is not None
    pc.items.append(Item("Charm", "petty", 0, rules=(CHOKING,)))
    text = present_rules_text(state, TEXT)
    assert text.count(f"- {CHOKING} ") == 1
    assert f"(from {VUK}, Charm)" in text


def test_nothing_attached_gives_no_text(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
    assert present_rules_text(_state(app, campaign_id), TEXT) == ""


def test_a_story_only_npc_here_is_marked_in_the_world_state(
    client_factory: Callable[[], TestClient],
):
    _, _, state, _ = _with_vuk_in_the_yard(client_factory)
    text = world_state_text(
        state,
        CAIRN.campaign,
        CAIRN.grid,
        CAIRN.combat,
        CAIRN.conditions,
        clock_rules=CAIRN.clock,
        inventory=CAIRN.inventory,
    )
    assert f"{VUK} (story only, no stats yet)" in text


def test_present_rules_get_their_own_system_part():
    parts = system_parts(
        "cairn2e", {}, frozenset(), "", 0, present_text="- vuk-choking-hold (from Vuk): text"
    )
    present = [p for p in parts if p.source == "Rules for what is present"]
    assert len(present) == 1
    assert present[0].content == "- vuk-choking-hold (from Vuk): text"


def test_no_present_rules_add_no_system_part():
    parts = system_parts("cairn2e", {}, frozenset(), "", 0)
    assert all(p.source != "Rules for what is present" for p in parts)


def test_a_world_monster_attaching_an_unknown_rule_is_a_content_error(tmp_path: Path):
    files = {
        "worlds/testland/world.json": _world(),
        "rulesets/core/rule_set.json": _core(),
        "rulesets/testset/rule_set.json": _rule_set(),
    }
    _write(tmp_path, files)
    stats = dict.fromkeys(ContentLibrary.load(tmp_path).rule_sets["testset"].character.stats, 1)
    ghoul = {
        "type": "monster",
        "id": "ghoul",
        "name": "Ghoul",
        "description": "A ghoul.",
        "stats": stats,
        "rules": ["no-such-rule"],
    }
    _write(tmp_path, {"worlds/testland/world.json": [*_world(), ghoul]})
    with pytest.raises(ContentError, match="monster 'ghoul' attaches rule 'no-such-rule'"):
        ContentLibrary.load(tmp_path).world_content("testland", "testset")


def test_a_world_npc_attaching_an_unknown_rule_is_a_content_error(tmp_path: Path):
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _story_only_world(
                story_only=True, rules=["no-such-rule"]
            ),
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": _rule_set(),
        },
    )
    with pytest.raises(ContentError, match="attaches rule 'no-such-rule'"):
        ContentLibrary.load(tmp_path).world_content("testland", "testset")


def test_every_bestiary_entry_has_its_description_and_every_attached_rule_its_text():
    rule_set = LIBRARY.rule_sets["cairn2e"]
    assert len(rule_set.monsters) == 84
    for monster in rule_set.monsters.values():
        assert monster.description, monster.id
        for rule_id in monster.rules:
            assert rule_set.attached_rules.get(rule_id), f"{monster.id} has no text for {rule_id}"


def test_a_bestiary_monster_present_brings_its_special_rules(
    client_factory: Callable[[], TestClient],
):
    rule_set = LIBRARY.rule_sets["cairn2e"]
    app, campaign_id, tools = _fight(client_factory)
    tools.create_monster("Basilisk", YARD)
    state = _state(app, campaign_id)
    basilisk = state.named("Basilisk")
    assert basilisk is not None
    assert "basilisk-gaze" in basilisk.rules
    text = present_rules_text(state, {**rule_set.rules, **rule_set.attached_rules})
    assert "- basilisk-gaze (from Basilisk):" in text
    assert "A target facing the Basilisk is slowly petrified" in text


def test_a_place_the_player_is_in_brings_its_rules(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
    with _db(app) as db:
        record(
            db,
            campaign_id,
            LocationAdded(name=YARD, description="The yard.", discovered=True, rules=[CHOKING]),
        )
        db.commit()
    text = present_rules_text(_state(app, campaign_id), TEXT)
    assert f"- {CHOKING} (from {YARD}): {TEXT[CHOKING]}" in text


def test_a_place_elsewhere_brings_no_rules(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
    with _db(app) as db:
        record(
            db,
            campaign_id,
            LocationAdded(name="Far Hollow", description="Far.", discovered=True, rules=[CHOKING]),
        )
        db.commit()
    assert present_rules_text(_state(app, campaign_id), TEXT) == ""


def test_twin_lakes_places_attach_their_module_rules():
    spots = {spot.id: spot for spot in TWIN_LAKES.locations}
    assert spots["deadmill"].rules == ("deadmill-sinking", "deadmill-time-loop")
    assert spots["the-downs"].rules == ("the-downs-travel",)
    assert spots["butcher-shop"].rules == ("trapdoor-butcher-block",)


def test_a_world_place_attaching_an_unknown_rule_is_a_content_error(tmp_path: Path):
    glade = {
        "type": "location",
        "id": "glade",
        "name": "Glade",
        "description": "A glade.",
        "rules": ["no-such-rule"],
    }
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": [*_world(), glade],
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": _rule_set(),
        },
    )
    with pytest.raises(ContentError, match="location 'glade' attaches rule 'no-such-rule'"):
        ContentLibrary.load(tmp_path).world_content("testland", "testset")


UNATTACHED_TWIN_LAKES_RULES = (
    "twin-lakes-overview",
    "the-roots",
    "outcome-vuk-reborn",
    "outcome-gate-opened",
    "outcome-vuk-stopped-gate-closed",
    "outcome-vuk-reborn-and-gate-opened",
)
ATTACHED_TWIN_LAKES_RULES = (
    "vuk-choking-hold",
    "deadmill-sinking",
    "deadmill-time-loop",
    "trapdoor-butcher-block",
    "suraru-imperator-critical-damage",
    "night-cat-attack",
    "the-downs-travel",
)


def test_the_unattached_twin_lakes_rules_are_the_module_wide_ones():
    world = LIBRARY.world_content("twin-lakes", "cairn2e")
    for rule_id in UNATTACHED_TWIN_LAKES_RULES:
        assert rule_id in world.unattached_rules, rule_id


def test_entity_attached_twin_lakes_rules_are_not_unattached():
    world = LIBRARY.world_content("twin-lakes", "cairn2e")
    for rule_id in ATTACHED_TWIN_LAKES_RULES:
        assert rule_id in world.rules, rule_id
        assert rule_id not in world.unattached_rules, rule_id
