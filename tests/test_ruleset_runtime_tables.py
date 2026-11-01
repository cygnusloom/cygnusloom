from __future__ import annotations

import random
from dataclasses import replace

from sqlmodel import Session

from cygnusforge.content.character import TableOption
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.services.campaigns import _tool_context
from cygnusforge.services.events import load_state
from cygnusforge.services.world import WorldTools
from tests.test_wwn_combat import _wwn_campaign


def test_campaign_tools_receive_ruleset_tables_and_inherited_core_tables(client_factory):
    client, campaign_id, _ = _wwn_campaign(client_factory)
    library = client.app.state.content
    rule_set = library.rule_sets["worldswithoutnumber"]
    world = library.world_content("latter-earth", rule_set.id)
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
    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    growth = f"growth_{pc.templates['background']}"
    assert ctx.world.tables[growth] == rule_set.tables[growth]
    assert ctx.world.rule_data is rule_set.rule_data
    assert set(library.core.tables) <= set(ctx.world.tables)

    override = (TableOption(name=None, description="A world-specific result"),)
    world = replace(world, tables={growth: override})
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
    assert overridden.world.tables[growth] == override
