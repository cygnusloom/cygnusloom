from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary
from cygnusloom.llm.base import ToolRequest
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.campaigns import _tool_context
from cygnusloom.services.events import load_state
from cygnusloom.services.tools import ToolRun, run_tool
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
    if tiers is None:
        return [head]
    head["npc_hp"] = tiers
    head["npc_hp_stat"] = "hp"
    return [head, {"type": "stat", "id": "hp", "name": "Hit Protection", "formula": "1d6"}]


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


CAIRN = LIBRARY.rule_sets["cairn2e"]
CAIRN_TABLES = LIBRARY.core.tables
CREATE = CAIRN.tool_calls["create_npc"]


def _gm_create(app: Any, campaign_id: int, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run the GM's create_npc with the context a campaign builds, so the rule set's tiers and
    arguments are the ones the model sees."""
    ctx = _tool_context(
        app.state.engine,
        random.Random(0),
        FakeProvider(),
        "test",
        campaign_id,
        CAIRN,
        LIBRARY.world_content("twin-lakes", "cairn2e"),
    )
    request = ToolRequest(id="call_1", name="create_npc", arguments=json.dumps(arguments))
    run = asyncio.run(run_tool({"create_npc": CREATE}, request, None, ctx))
    assert isinstance(run, ToolRun)
    return json.loads(run.output)


def test_the_gm_is_offered_the_rule_sets_tiers_as_an_optional_argument():
    properties = CREATE.parameters["properties"]
    assert properties["hp_tier"]["enum"] == ["average", "hardier", "serious"]
    assert "hp_tier" not in CREATE.parameters.get("required", [])


def test_a_rule_set_without_tiers_is_not_offered_one(tmp_path: Path):
    library = _load(tmp_path, _world_with_npc(), None)
    calls = library.rule_sets["testset"].tool_calls.values()
    assert all("hp_tier" not in call.parameters["properties"] for call in calls)
    assert "hp_tier" not in LIBRARY.core.tool_calls["create_npc"].parameters["properties"]
    assert "hp_tier" not in LIBRARY.core.tool_calls["create_npc"].description


def test_the_gm_creates_an_npc_with_the_tier_hp(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    for tier, hp in (("average", 3), ("hardier", 6), ("serious", 10)):
        name = f"Sentry {tier}"
        result = _gm_create(
            client.app,
            campaign_id,
            {
                "name": name,
                "location": "The Stuffed Inn",
                "description": "A guard.",
                "hp_tier": tier,
            },
        )
        assert result.get("created") == "npc", result
        with _db(client.app) as db:
            npc = load_state(db, campaign_id).named(name)
        assert npc is not None
        assert npc.stats["hp"] == hp, tier


def test_an_unknown_tier_is_refused(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    result = _gm_create(
        client.app,
        campaign_id,
        {
            "name": "Sentry",
            "location": "The Stuffed Inn",
            "description": "A guard.",
            "hp_tier": "mighty",
        },
    )
    assert "error" in result
    assert "hp_tier" in result["error"] and "average, hardier, serious" in result["error"]


def test_a_tier_with_hp_is_refused(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    result = _gm_create(
        client.app,
        campaign_id,
        {
            "name": "Sentry",
            "location": "The Stuffed Inn",
            "description": "A guard.",
            "hp_tier": "average",
            "character": {"stats": {"hp": 2}},
        },
    )
    assert "Give either hp_tier or hp" in result["error"]


def test_a_tier_on_a_statblock_creature_is_refused(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    result = _gm_create(
        client.app,
        campaign_id,
        {
            "name": "Blink Dog",
            "location": "The Stuffed Inn",
            "description": "A beast.",
            "hp_tier": "average",
        },
    )
    assert "takes its HP from the statblock" in result["error"]


def test_a_statblock_creature_still_takes_its_statblock_without_a_tier(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    result = _gm_create(
        client.app,
        campaign_id,
        {"name": "Blink Dog", "location": "The Stuffed Inn", "description": "A beast."},
    )
    assert result.get("created") == "npc", result
    with _db(client.app) as db:
        npc = load_state(db, campaign_id).named("Blink Dog")
    assert npc is not None
    assert npc.stats["hp"] == 5


def test_a_tier_with_force_sets_the_hp_of_a_creature_without_its_statblock(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    result = _gm_create(
        client.app,
        campaign_id,
        {
            "name": "Blink Dog",
            "location": "The Stuffed Inn",
            "description": "A beast.",
            "hp_tier": "serious",
            "force": True,
        },
    )
    assert result.get("created") == "npc", result
    with _db(client.app) as db:
        npc = load_state(db, campaign_id).named("Blink Dog")
    assert npc is not None
    assert npc.stats["hp"] == 10


def test_hp_tiers_need_the_stat_they_set(tmp_path: Path):
    head = _rule_set_with_tiers({"average": 3})
    del head[0]["npc_hp_stat"]
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _world(),
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": head,
        },
    )
    with pytest.raises(ContentError, match="needs 'npc_hp_stat'"):
        ContentLibrary.load(tmp_path)


def test_hp_tiers_must_set_a_stat_the_rule_set_has(tmp_path: Path):
    head = _rule_set_with_tiers({"average": 3})
    head[0]["npc_hp_stat"] = "vigor"
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _world(),
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": head,
        },
    )
    with pytest.raises(ContentError, match="is not a stat of this rule set"):
        ContentLibrary.load(tmp_path)


def test_a_condition_may_only_end_by_a_trigger_its_rule_set_declares(tmp_path: Path):
    head = _rule_set_with_tiers()
    head[0]["condition_ends"] = ["rest"]
    head.append(
        {
            "type": "condition",
            "id": "winded",
            "label": "Winded",
            "description": "Out of breath.",
            "ends": ["aid"],
        }
    )
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _world(),
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": head,
        },
    )
    with pytest.raises(ContentError, match="'ends' must list only"):
        ContentLibrary.load(tmp_path)
