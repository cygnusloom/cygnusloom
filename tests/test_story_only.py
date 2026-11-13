from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    NPC_KIND,
    ParticipantJoined,
    last_event_id,
    load_state,
    record,
)
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library
from tests.test_character_creation import _cairn_campaign
from tests.test_combat import PC, _fight, _tools
from tests.test_content import _core, _rule_set, _world, _write
from tests.test_world import _db, _state
from tests.turns import take_turn

LIBRARY = content_library()
TWIN_LAKES = LIBRARY.world_templates["twin-lakes"]
STATS = {"hp": 6, "str": 10, "dex": 10, "wil": 10}
STORY_ONLY = sorted(n.name for n in TWIN_LAKES.npcs if n.story_only)
STATTED = sorted(n.name for n in TWIN_LAKES.npcs if not n.story_only)
STORY = STORY_ONLY[0]
PLAIN = STATTED[0]
YARD = "Stonegate Yard"


def _tools_for(app: FastAPI, campaign_id: int) -> WorldTools:
    return _tools(app, campaign_id, random.Random(0))


def _story_only_world(**npc: Any) -> list[dict[str, Any]]:
    glade = {"type": "location", "id": "glade", "name": "Glade", "description": "A glade."}
    bo = {"type": "npc", "id": "bo", "name": "Bo", "location": "glade", "description": "A bo."}
    return [*_world(), glade, {**bo, **npc}]


def test_a_story_only_template_loads_with_the_flag_set(tmp_path: Path):
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _story_only_world(story_only=True),
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": _rule_set(),
        },
    )
    library = ContentLibrary.load(tmp_path)
    npc = next(n for n in library.world_templates["testland"].npcs if n.id == "bo")
    assert npc.story_only is True
    assert npc.monster is None


def test_a_story_only_npc_with_a_monster_is_a_content_error(tmp_path: Path):
    world = _story_only_world(story_only=True, monster="wolf")
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": world,
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": _rule_set(),
        },
    )
    with pytest.raises(ContentError, match="a story-only NPC has no statblock or monster"):
        ContentLibrary.load(tmp_path)


def test_seeding_gives_a_story_only_npc_empty_stats(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    npc = _state(client.app, campaign_id).named(STORY)
    assert npc is not None
    assert npc.kind == NPC_KIND
    assert npc.story_only is True
    assert npc.stats == {}
    assert npc.armor == 0
    assert npc.attacks == {}


def test_seeding_gives_a_normal_npc_no_story_only_flag(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    npc = _state(client.app, campaign_id).named(PLAIN)
    assert npc is not None
    assert npc.story_only is False


def test_a_story_only_npc_cannot_join_an_encounter(client_factory: Callable[[], TestClient]):
    _, _, tools = _fight(client_factory, twin_lakes=True)
    with pytest.raises(InvalidInput, match="has no stats yet"):
        tools.add_participant(STORY)


def test_a_story_only_npc_cannot_make_a_save(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    tools = _tools_for(client.app, campaign_id)
    with pytest.raises(InvalidInput, match="has no stats yet"):
        tools.roll_save(STORY, "str", "a test")


def test_a_story_only_npc_cannot_be_attacked_by_the_public_api(
    client_factory: Callable[[], TestClient],
):
    # add_participant refuses a story-only NPC, so it is never in the order and the public attack
    # path stops at the participant lookup before reaching the stats guard.
    _, _, tools = _fight(client_factory, twin_lakes=True)
    take_turn(tools, PC)
    with pytest.raises(InvalidInput, match="is not in the encounter"):
        tools.player_attack(STORY, "Sword")


def test_a_story_only_npc_in_the_order_is_refused_as_target_and_attacker(
    client_factory: Callable[[], TestClient],
):
    # Force the participant into the encounter straight through the event log, so the stats guard
    # in _living is what refuses it.
    app, campaign_id, tools = _fight(client_factory, twin_lakes=True)
    tools.create_monster(
        "Grave Thing",
        YARD,
        description="A foe.",
        stats={"hp": 6, "str": 10, "dex": 10, "wil": 10},
        armor=0,
        attacks=[{"name": "Claw", "dice": "1d6"}],
    )
    tools.add_participant("Grave Thing")
    with _db(app) as db:
        record(db, campaign_id, ParticipantJoined(name=STORY, side="foes"))
        db.commit()
    take_turn(tools, "Grave Thing")
    with pytest.raises(InvalidInput, match="has no stats yet"):
        tools.attack("Grave Thing", "Claw", STORY)
    take_turn(tools, STORY)
    with pytest.raises(InvalidInput, match="has no stats yet"):
        tools.attack(STORY, "Claw", "Grave Thing")


def test_update_npc_gives_a_story_only_npc_stats_armor_and_attacks(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    tools = _tools_for(client.app, campaign_id)
    result = tools.update_npc(
        STORY,
        character={"stats": STATS, "fields": {}},
        armor=1,
        attacks=[{"name": "Claw", "dice": "1d6"}],
    )
    assert result == {"updated": "npc", "name": STORY, "statted": True}
    npc = _state(client.app, campaign_id).named(STORY)
    assert npc.story_only is False
    assert npc.stats == STATS
    assert npc.armor == 1
    assert npc.attacks == {"Claw": "1d6"}


def test_a_second_stats_update_is_refused(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    tools = _tools_for(client.app, campaign_id)
    tools.update_npc(STORY, character={"stats": STATS, "fields": {}})
    with pytest.raises(InvalidInput, match="already has stats"):
        tools.update_npc(STORY, character={"stats": STATS, "fields": {}})


def test_stats_for_a_normal_npc_are_refused(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    tools = _tools_for(client.app, campaign_id)
    with pytest.raises(InvalidInput, match="only a story-only NPC can be given them"):
        tools.update_npc(PLAIN, character={"stats": STATS, "fields": {}})


def test_a_statted_npc_gets_attacks_without_rebuilding_the_sheet(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    tools = _tools_for(client.app, campaign_id)
    before = _state(client.app, campaign_id).named(PLAIN)
    assert before is not None

    result = tools.update_npc(PLAIN, attacks=[{"name": "Club", "dice": "1d6"}], armor=2)

    assert result == {"updated": "npc", "name": PLAIN, "statted": False, "armed": True}
    npc = _state(client.app, campaign_id).named(PLAIN)
    assert npc is not None
    assert npc.attacks == {"Club": "1d6"}
    assert npc.armor == 2
    # The rest of the sheet is left exactly as it was.
    assert npc.stats == before.stats
    assert npc.templates == before.templates
    assert npc.features == before.features
    assert npc.story_only is False


def test_attacks_on_a_statted_npc_are_replaced_by_a_later_update(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    tools = _tools_for(client.app, campaign_id)
    tools.update_npc(PLAIN, attacks=[{"name": "Club", "dice": "1d6"}])

    tools.update_npc(PLAIN, attacks=[{"name": "Spear", "dice": "1d8"}])

    assert _state(client.app, campaign_id).named(PLAIN).attacks == {"Spear": "1d8"}


def test_a_description_only_update_works_for_a_story_only_npc(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    tools = _tools_for(client.app, campaign_id)
    result = tools.update_npc(STORY, description="Waits by the door.")
    assert result["statted"] is False
    npc = _state(client.app, campaign_id).named(STORY)
    assert npc.story_only is True
    assert npc.stats == {}


def test_a_description_only_update_works_for_a_normal_npc(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    tools = _tools_for(client.app, campaign_id)
    result = tools.update_npc(PLAIN, description="Hums while working.")
    assert result["statted"] is False
    assert _state(client.app, campaign_id).named(PLAIN).story_only is False


def test_replaying_the_log_gives_the_statted_npc_and_stops_before_it(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _cairn_campaign(client_factory)
    tools = _tools_for(client.app, campaign_id)
    tools.update_npc(
        STORY,
        character={"stats": STATS, "fields": {}},
        armor=2,
        attacks=[{"name": "Claw", "dice": "1d6"}],
    )
    with _db(app) as db:
        statted_at = last_event_id(db, campaign_id)
        replayed = load_state(db, campaign_id)
        before_stats = load_state(db, campaign_id, before=statted_at)

    npc = replayed.named(STORY)
    assert npc.story_only is False
    assert npc.stats == STATS
    assert npc.armor == 2
    assert npc.attacks == {"Claw": "1d6"}

    earlier = before_stats.named(STORY)
    assert earlier.story_only is True
    assert earlier.stats == {}
    assert earlier.armor == 0
