from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cygnusforge.content.definitions import ContentError
from cygnusforge.content.library import ContentLibrary
from cygnusforge.services.events import load_state
from tests.conftest import content_library
from tests.test_character_creation import _cairn_campaign
from tests.test_content import _core, _world, _write
from tests.test_world import _db

LIBRARY = content_library()
TWIN_LAKES = LIBRARY.world_templates["twin-lakes"]
STATLESS = (
    "Reeve Trellis",
    "Aldra",
    "Flavia",
    "Jandy",
    "Giva",
    "Drum",
    "Corsetta",
    "Franz",
    "Beju",
    "Nicolas",
    "Berk",
)


def _rule_set_with_tiers(tiers: Any = None) -> list[dict[str, Any]]:
    head = {"type": "rule_set", "id": "testset", "name": "Test set", "description": "For tests."}
    if tiers is not None:
        head["npc_hp"] = tiers
    return [head]


def _world_with_npc(**npc: Any) -> list[dict[str, Any]]:
    glade = {"type": "location", "id": "glade", "name": "Glade", "description": "A glade."}
    bo = {"type": "npc", "id": "bo", "name": "Bo", "location": "glade", "description": "A bo."}
    return [*_world(), glade, {**bo, **npc}]


def _load(tmp_path: Path, world: list[dict[str, Any]], tiers: Any = None) -> ContentLibrary:
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": world,
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": _rule_set_with_tiers(tiers),
        },
    )
    return ContentLibrary.load(tmp_path)


def test_the_cairn_rule_set_defines_the_book_hp_tiers():
    assert LIBRARY.rule_sets["cairn2e"].npc_hp == {"average": 3, "hardier": 6, "serious": 10}


def test_every_statless_twin_lakes_npc_has_the_average_tier():
    tiers = {npc.name: npc.hp_tier for npc in TWIN_LAKES.npcs if npc.name in STATLESS}
    assert tiers == {name: "average" for name in STATLESS}


def test_a_tier_loads_on_an_npc_and_the_rule_set_map_loads(tmp_path: Path):
    library = _load(tmp_path, _world_with_npc(hp_tier="hardier"), {"hardier": 6})
    npc = next(n for n in library.world_templates["testland"].npcs if n.id == "bo")
    assert npc.hp_tier == "hardier"
    assert library.rule_sets["testset"].npc_hp == {"hardier": 6}


def test_an_npc_without_a_tier_has_none(tmp_path: Path):
    library = _load(tmp_path, _world_with_npc(), {"average": 3})
    npc = next(n for n in library.world_templates["testland"].npcs if n.id == "bo")
    assert npc.hp_tier is None


def test_a_tier_the_rule_set_does_not_define_is_a_content_error(tmp_path: Path):
    library = _load(tmp_path, _world_with_npc(hp_tier="serious"), {"average": 3})
    with pytest.raises(ContentError, match="does not define"):
        library.world_content("testland", "testset")


def test_a_tier_with_no_rule_set_map_is_a_content_error(tmp_path: Path):
    library = _load(tmp_path, _world_with_npc(hp_tier="average"))
    with pytest.raises(ContentError, match="does not define"):
        library.world_content("testland", "testset")


@pytest.mark.parametrize("tiers", [{"average": 0}, {"average": "3"}, {"average": True}, [3]])
def test_a_bad_hp_tier_value_is_a_content_error(tmp_path: Path, tiers: Any):
    with pytest.raises(ContentError, match="npc_hp"):
        _load(tmp_path, _world_with_npc(), tiers)


def test_a_tier_on_an_npc_with_a_monster_is_a_content_error(tmp_path: Path):
    world = _world_with_npc(hp_tier="average", monster="wolf")
    with pytest.raises(ContentError, match="not an 'hp_tier'"):
        _load(tmp_path, world, {"average": 3})


def test_a_tier_on_a_story_only_npc_is_a_content_error(tmp_path: Path):
    world = _world_with_npc(hp_tier="average", story_only=True)
    with pytest.raises(ContentError, match="no HP to set"):
        _load(tmp_path, world, {"average": 3})


def test_seeding_gives_each_statless_npc_the_average_hp(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    with _db(client.app) as db:
        state = load_state(db, campaign_id)
    for name in STATLESS:
        npc = state.named(name)
        assert npc is not None, name
        assert npc.stats["hp"] == 3, name


def test_a_statblock_npc_keeps_its_monster_hp(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    with _db(client.app) as db:
        state = load_state(db, campaign_id)
    fasha = state.named("Fasha")
    assert fasha is not None
    assert fasha.stats["hp"] == 6
