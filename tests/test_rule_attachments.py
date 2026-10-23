from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cygnusforge.content.definitions import ContentError
from cygnusforge.content.library import ContentLibrary
from cygnusforge.services.campaigns import system_parts
from cygnusforge.services.events import Item, ItemAdded, record
from cygnusforge.services.world import present_rules_text, world_state_text
from tests.conftest import content_library
from tests.test_character_creation import _cairn_campaign
from tests.test_combat import CAIRN, PC, _fight
from tests.test_content import _core, _rule_set, _world, _write
from tests.test_world import _db, _state

LIBRARY = content_library()
TWIN_LAKES = LIBRARY.world_templates["twin-lakes"]
VUK = "Vuk"
CHOKING = "vuk-choking-hold"
TEXT = {CHOKING: "The Ash Phantom chokes with its hand."}


def _with_vuk_in_the_yard(client_factory: Callable[[], TestClient]):
    app, campaign_id, _ = _fight(client_factory)
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
