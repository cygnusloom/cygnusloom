from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.content.library import (
    DATA_DIR,
    ContentLibrary,
    _load_creative_table,
    _patch_creative_table,
)
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.dice_tables import roll_creative
from cygnusloom.services.events import decode, events_since
from tests.conftest import bundled_library
from tests.test_combat import CAIRN, _fight
from tests.test_content import _core, _rule_set, _world, _write

TABLES_ROLLED = "dice.tables_rolled"


def _creative(entries: object, **extra: object) -> Definition:
    data: dict[str, object] = {"type": "creative_table", "id": "t", "entries": entries, **extra}
    return Definition(type="creative_table", id="t", data=data)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A valid content tree whose core has one creative table, `plot`, of three entries."""
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _world(),
            "rulesets/core/rule_set.json": _core(),
            "rulesets/core/creative.json": [
                {"type": "creative_table", "id": "plot", "entries": ["a", "b", "c"]}
            ],
            "rulesets/testset/rule_set.json": _rule_set(),
        },
    )
    return tmp_path


CORE_CREATIVE_TABLES = ["action", "theme", "plot_twist", "location", "location_descriptor"]
# The setting tables live in core too, after the core's own, so every rule set can roll them.
WORLDGEN_CREATIVE_TABLES = [
    "setting_premise",
    "region_identity",
    "faction",
    "conflict",
    "mystery",
    "adventure_hook",
]
ALL_CORE_CREATIVE_TABLES = [*CORE_CREATIVE_TABLES, *WORLDGEN_CREATIVE_TABLES]


def _rule_set_tables(tree: Path, definitions: list[dict[str, Any]]) -> dict[str, Any]:
    _write(tree, {"rulesets/testset/creative.json": definitions})
    return ContentLibrary.load(tree).rule_sets["testset"].dice_tables


def test_core_creative_tables_roll_one_die_with_an_entry_per_roll():
    tables = bundled_library().rule_sets["cairn2e"].dice_tables
    for table_id in CORE_CREATIVE_TABLES:
        table = tables[table_id]
        assert table.creative
        assert table.dice == f"1d{len(table.rows)}"
        assert [row.high for row in table.rows] == list(range(1, len(table.rows) + 1))
        assert all(row.low == row.high for row in table.rows)
        assert table.row_for(1).result


def test_core_offers_the_roll_creative_tool():
    tools = bundled_library().core.tool_calls
    assert "roll_creative" in tools


def test_a_rule_set_adds_and_removes_entries_from_a_core_table(tree: Path):
    tables = _rule_set_tables(
        tree, [{"type": "creative_table", "id": "plot", "add": ["d"], "remove": ["b"]}]
    )
    plot = tables["plot"]
    assert plot.creative
    assert plot.dice == "1d3"
    assert [row.result for row in plot.rows] == ["a", "c", "d"]


def test_a_rule_set_may_rename_a_core_table(tree: Path):
    tables = _rule_set_tables(
        tree, [{"type": "creative_table", "id": "plot", "name": "Hook", "remove": ["a"]}]
    )
    assert tables["plot"].name == "Hook"
    assert [row.result for row in tables["plot"].rows] == ["b", "c"]


def test_a_rule_set_may_replace_a_core_table_with_entries(tree: Path):
    tables = _rule_set_tables(
        tree, [{"type": "creative_table", "id": "plot", "entries": ["x", "y"]}]
    )
    assert [row.result for row in tables["plot"].rows] == ["x", "y"]


def test_a_creative_table_reads_its_genre_tags():
    table = _load_creative_table(_creative(["a", "b"], genres=["horror"]), "testset")
    assert table.genres == ("horror",)


def _core_creative_with_genres(tree: Path) -> None:
    _write(
        tree,
        {
            "rulesets/core/creative.json": [
                {
                    "type": "creative_table",
                    "id": "plot",
                    "entries": ["a", "b", "c"],
                    "genres": ["fantasy"],
                }
            ]
        },
    )


def test_a_rule_set_patch_keeps_the_core_tables_genres(tree: Path):
    _core_creative_with_genres(tree)
    tables = _rule_set_tables(tree, [{"type": "creative_table", "id": "plot", "add": ["d"]}])
    assert tables["plot"].genres == ("fantasy",)


def test_a_rule_set_patch_may_retag_a_core_table(tree: Path):
    _core_creative_with_genres(tree)
    tables = _rule_set_tables(
        tree,
        [{"type": "creative_table", "id": "plot", "add": ["d"], "genres": ["generic"]}],
    )
    assert tables["plot"].genres == ("generic",)


def test_a_rule_set_may_define_a_new_creative_table(tree: Path):
    tables = _rule_set_tables(
        tree, [{"type": "creative_table", "id": "heist", "entries": ["vault", "vent"]}]
    )
    assert tables["heist"].creative
    assert tables["heist"].name == "heist"
    assert tables["plot"].creative
    assert [row.result for row in tables["plot"].rows] == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("definitions", "message"),
    [
        (
            [{"type": "creative_table", "id": "plot", "remove": ["zzz"]}],
            "'remove' names 'zzz', which the core table lacks",
        ),
        (
            [{"type": "creative_table", "id": "plot", "add": ["a"]}],
            "'add' repeats 'a', which the table already has",
        ),
        (
            [{"type": "creative_table", "id": "plot", "add": ["d", "d"]}],
            "'add' repeats 'd'",
        ),
        (
            [{"type": "creative_table", "id": "plot", "remove": ["a", "a"]}],
            "'remove' names 'a'",
        ),
        (
            [{"type": "creative_table", "id": "plot", "remove": ["a", "b", "c"]}],
            "'entries' must be a non-empty list",
        ),
        (
            [{"type": "creative_table", "id": "nope", "add": ["x"]}],
            "need a core creative table of this id",
        ),
        (
            [{"type": "creative_table", "id": "plot", "add": ["x"], "entries": ["y"]}],
            "unknown keys",
        ),
        (
            [{"type": "creative_table", "id": "plot", "add": "x"}],
            "'add' must be a list of non-empty text",
        ),
        (
            [{"type": "creative_table", "id": "plot", "add": [""]}],
            "'add' must be a list of non-empty text",
        ),
        (
            [{"type": "creative_table", "id": "heist", "entries": []}],
            "'entries' must be a non-empty list",
        ),
        (
            [{"type": "creative_table", "id": "heist", "entries": ["a", "a"]}],
            "repeats an entry",
        ),
        (
            [{"type": "creative_table", "id": "heist", "entries": ["a", 3]}],
            "must be a list of non-empty text",
        ),
        (
            [{"type": "creative_table", "id": "heist", "entries": ["a"], "rolls": 2}],
            "unknown keys",
        ),
    ],
)
def test_a_bad_creative_table_is_rejected(tree: Path, definitions, message):
    _write(tree, {"rulesets/testset/creative.json": definitions})
    with pytest.raises(ContentError, match=message):
        ContentLibrary.load(tree)


def test_a_world_may_define_its_own_creative_table(tree: Path):
    _write(
        tree,
        {
            "worlds/testland/creative.json": [
                {"type": "creative_table", "id": "heist", "entries": ["vault", "vent"]}
            ]
        },
    )
    tables = ContentLibrary.load(tree).world_content("testland", "testset").dice_tables
    assert tables["heist"].creative
    assert [row.result for row in tables["heist"].rows] == ["vault", "vent"]


def test_a_world_cannot_add_to_or_remove_from_a_core_table(tree: Path):
    _write(
        tree,
        {"worlds/testland/creative.json": [{"type": "creative_table", "id": "plot", "add": ["x"]}]},
    )
    with pytest.raises(ContentError, match="unknown keys"):
        ContentLibrary.load(tree).world_content("testland", "testset")


def test_a_core_creative_table_cannot_carry_add_or_remove():
    with pytest.raises(ContentError, match="unknown keys"):
        _load_creative_table(_creative(["a"], add=["b"]), "core")


def test_creative_entries_are_listed_in_order_of_the_roll():
    table = _load_creative_table(_creative(["first", "second", "third"]), "core")
    assert table.dice == "1d3"
    assert table.row_for(2).result == "second"


def test_roll_creative_rolls_every_creative_table_by_default(client_factory):
    _, campaign_id, tools = _fight(client_factory)
    with Session(tools.engine) as db:
        last_id = max((row.id for row in events_since(db, campaign_id)), default=0)
        out = roll_creative(
            db, campaign_id, None, CAIRN.dice_tables, random.Random(3), plus=CAIRN.plus
        )
        new = events_since(db, campaign_id, after=last_id)
    rolls = out["rolls"]
    assert [roll["table"] for roll in rolls] == ALL_CORE_CREATIVE_TABLES
    for roll in rolls:
        assert roll["result"] == CAIRN.dice_tables[roll["table"]].row_for(roll["roll"]).result
    (event,) = [row for row in new if row.kind == TABLES_ROLLED]
    assert decode(event).rolls == rolls


def test_roll_creative_treats_an_empty_list_as_every_creative_table(client_factory):
    _, campaign_id, tools = _fight(client_factory)
    with Session(tools.engine) as db:
        out = roll_creative(
            db, campaign_id, [], CAIRN.dice_tables, random.Random(3), plus=CAIRN.plus
        )
    assert [roll["table"] for roll in out["rolls"]] == ALL_CORE_CREATIVE_TABLES


def test_roll_creative_rolls_only_the_named_tables(client_factory):
    _, campaign_id, tools = _fight(client_factory)
    with Session(tools.engine) as db:
        out = roll_creative(
            db, campaign_id, ["location"], CAIRN.dice_tables, random.Random(3), plus=CAIRN.plus
        )
    assert [roll["table"] for roll in out["rolls"]] == ["location"]


def test_roll_creative_refuses_a_table_that_is_not_creative(client_factory):
    _, campaign_id, tools = _fight(client_factory)
    with (
        Session(tools.engine) as db,
        pytest.raises(InvalidInput, match="no creative table named 'dungeon_reaction'"),
    ):
        roll_creative(
            db,
            campaign_id,
            ["dungeon_reaction"],
            CAIRN.dice_tables,
            random.Random(3),
            plus=CAIRN.plus,
        )


def test_roll_creative_refuses_when_no_creative_table_is_defined(client_factory):
    _, campaign_id, tools = _fight(client_factory)
    plain = {k: v for k, v in CAIRN.dice_tables.items() if not v.creative}
    with (
        Session(tools.engine) as db,
        pytest.raises(InvalidInput, match="no creative tables are defined"),
    ):
        roll_creative(db, campaign_id, None, plain, random.Random(3), plus=CAIRN.plus)


def test_the_world_roll_creative_returns_the_rolls_shape(client_factory: Callable[[], TestClient]):
    _, _, tools = _fight(client_factory)
    live = replace(tools, dice_tables=CAIRN.dice_tables, rng=random.Random(1))
    out = live.roll_creative(None)
    assert list(out) == ["rolls"]
    assert [roll["table"] for roll in out["rolls"]] == ALL_CORE_CREATIVE_TABLES


def test_a_creative_table_may_credit_its_source():
    table = _load_creative_table(_creative(["a", "b"], source="Some rulebook, CC BY 4.0"), "core")
    assert [row.result for row in table.rows] == ["a", "b"]


@pytest.mark.parametrize("source", ["", 3, ["x"]])
def test_a_creative_table_source_must_be_text(source):
    with pytest.raises(ContentError, match="'source' must be a list of non-empty text"):
        _load_creative_table(_creative(["a"], source=source), "core")


def test_a_patched_core_table_keeps_the_core_source():
    core = {"plot": _creative(["a"], source="Core credit")}
    patched = _patch_creative_table(
        Definition(
            type="creative_table",
            id="plot",
            data={"type": "creative_table", "id": "plot", "add": ["b"]},
        ),
        core,
        "rulesets/testset",
    )
    assert patched.data["source"] == "Core credit"
    assert patched.data["entries"] == ["a", "b"]


def test_a_patch_may_give_its_own_source():
    core = {"plot": _creative(["a"], source="Core credit")}
    patched = _patch_creative_table(
        Definition(
            type="creative_table",
            id="plot",
            data={"type": "creative_table", "id": "plot", "add": ["b"], "source": "Mine"},
        ),
        core,
        "rulesets/testset",
    )
    assert patched.data["source"] == "Mine"


def test_every_core_creative_table_credits_its_source():
    data = json.loads((DATA_DIR / "rulesets" / "core" / "creative_tables.json").read_text())
    assert [table["id"] for table in data] == CORE_CREATIVE_TABLES
    for table in data:
        assert "CC BY 4.0" in table["source"], table["id"]
        assert "Ironsworn" in table["source"], table["id"]
