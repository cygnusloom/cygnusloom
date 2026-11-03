"""The tools a campaign hands the GM carry its rule set's tables, inherited core tables, and the
world's overrides.

The rule set and world are written for the test, so this is about the wiring rather than any
shipped rule set's own tables.
"""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path
from typing import Any

from cygnusforge.content.character import TableOption
from cygnusforge.content.library import ContentLibrary
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.services.campaigns import _tool_context
from cygnusforge.services.world import WorldTools
from tests.factories import campaign_ready
from tests.support import DEFAULT_RULE_SET, DEFAULT_WORLD, content_tree

TABLE = "omen"


def _rule_set_table() -> dict[str, Any]:
    return {"type": "table", "id": TABLE, "options": ["a red comet"]}


def test_campaign_tools_receive_ruleset_tables_and_inherited_core_tables(
    client_factory, tmp_path: Path
):
    client, campaign_id = campaign_ready(client_factory)
    library = ContentLibrary.load(content_tree(tmp_path, definitions=[_rule_set_table()]))
    rule_set = library.rule_sets[DEFAULT_RULE_SET]
    world = library.world_content(DEFAULT_WORLD, rule_set.id)
    ctx = _tool_context(
        client.app.state.engine,
        random.Random(0),
        FakeProvider(),
        "fake/scripted",
        campaign_id,
        rule_set,
        world,
    )

    assert isinstance(ctx.world, WorldTools)
    assert ctx.world.rule_data is rule_set.rule_data
    assert ctx.world.tables[TABLE] == rule_set.tables[TABLE]
    assert set(library.core.tables) <= set(ctx.world.tables)

    override = (TableOption(name=None, description="A world-specific result"),)
    world = replace(world, tables={TABLE: override})
    overridden = _tool_context(
        client.app.state.engine,
        random.Random(0),
        FakeProvider(),
        "fake/scripted",
        campaign_id,
        rule_set,
        world,
    )
    assert isinstance(overridden.world, WorldTools)
    assert overridden.world.tables[TABLE] == override
