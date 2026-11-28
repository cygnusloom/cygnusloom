"""Experience and milestone leveling, and the character panel's next-level indicator."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Event
from cygnusloom.rules import dnd_srd_2024
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    CampaignOptionsChosen,
    CounterChanged,
    decode,
    record,
)
from cygnusloom.services.views import scene_view, world_state_text
from cygnusloom.services.world_support import set_campaign_options
from tests.factories import (
    create_campaign_direct,
    create_character_direct,
    open_campaign,
    place_pc,
    rules_for,
    signed_in_client,
    tools_for,
)
from tests.test_dnd_srd import _fields
from tests.test_wwn_advancement import _campaign as _wwn_campaign

DND = "dnd-srd-2024"
WWN = "worldswithoutnumber"


def _dnd_campaign(
    client_factory: Callable[[], TestClient],
) -> tuple[TestClient, int]:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign_direct(client, world_template="testhollow", rule_set=DND)
    create_character_direct(client, campaign_id, name="Rook", fields=_fields())
    return client, campaign_id


def _scene(client: TestClient, campaign_id: int, rule_set: str = DND):
    rules = rules_for(rule_set)
    state = open_campaign(client.app, campaign_id)
    return scene_view(
        state,
        rules.character,
        conditions=rules.conditions,
        counters=rules.counters,
        level_up=next(iter(rules.level_ups.values())),
    )


def _set_leveling(client: TestClient, campaign_id: int, value: str) -> None:
    rules = rules_for(DND)
    with Session(client.app.state.engine) as db:
        set_campaign_options(
            db,
            campaign_id,
            rules.campaign,
            {"xp_rate": "Standard", "leveling": value},
            replace=True,
        )
        db.commit()


def _dnd_context(app: FastAPI, campaign_id: int):
    rules = rules_for(DND)
    world = app.state.content.world_content("testhollow", DND)
    return level_up_service.tool_context(app.state.engine, campaign_id, rules, world)


def test_the_sheet_shows_how_much_experience_the_next_level_needs(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _dnd_campaign(client_factory)
    tools_for(client.app, campaign_id, rule_set=DND).adjust_counter(
        "Rook", "xp", 225, "a live test"
    )

    identity = dict(_scene(client, campaign_id).pc_sheet.identity)

    assert identity["Level"] == "1"
    assert identity["Experience"] == "225 / 300 (75 to go)"


def test_the_dnd_rule_set_offers_both_leveling_methods():
    rules = rules_for(DND)
    field = rules.campaign.fields["leveling"]

    assert field.default == "Experience"
    assert [option.value for option in rules.campaign.table_options(field)] == [
        "Experience",
        "Milestone",
    ]
    assert "milestone_level" in rules.tool_calls


def test_an_older_campaign_without_the_option_counts_experience(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _dnd_campaign(client_factory)
    with Session(client.app.state.engine) as db:
        record(
            db,
            campaign_id,
            CampaignOptionsChosen(options={"xp_rate": "Standard"}),
        )
        db.commit()
    tools_for(client.app, campaign_id, rule_set=DND).adjust_counter(
        "Rook", "xp", 75, "an older log"
    )
    rules = rules_for(DND)
    state = open_campaign(client.app, campaign_id)

    identity = dict(
        scene_view(
            state,
            rules.character,
            conditions=rules.conditions,
            counters=rules.counters,
            level_up=next(iter(rules.level_ups.values())),
        ).pc_sheet.identity
    )

    assert identity["Experience"] == "75 / 300 (225 to go)"
    assert "Campaign leveling: Experience." in world_state_text(state, rules.campaign)
    dnd_srd_2024.award_xp("Rook", 25, _dnd_context(client.app, campaign_id))
    assert open_campaign(client.app, campaign_id).characters["rook"].counters["xp"] == 100


def test_the_sheet_says_when_a_level_is_ready(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _dnd_campaign(client_factory)
    tools_for(client.app, campaign_id, rule_set=DND).adjust_counter(
        "Rook", "xp", 300, "a live test"
    )

    identity = dict(_scene(client, campaign_id).pc_sheet.identity)

    assert identity["Experience"] == "300 / 300 (level ready)"


def test_a_milestone_campaign_shows_the_method_instead_of_experience(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _dnd_campaign(client_factory)
    _set_leveling(client, campaign_id, "Milestone")

    identity = dict(_scene(client, campaign_id).pc_sheet.identity)

    assert identity["Leveling"] == "Milestone (the GM decides)"
    assert "Experience" not in identity


def test_milestone_level_fills_the_next_threshold_and_opens_the_level_page(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _dnd_campaign(client_factory)
    _set_leveling(client, campaign_id, "Milestone")
    rules = rules_for(DND)
    ctx = _dnd_context(client.app, campaign_id)

    result = dnd_srd_2024.milestone_level("Rook", ctx, "clearing the goblin cave")

    assert result == {
        "character": "Rook",
        "gained": 300,
        "xp": 300,
        "next_level": 2,
        "cause": "clearing the goblin cave",
    }
    state = open_campaign(client.app, campaign_id)
    assert state.characters["rook"].counters["xp"] == 300
    assert level_up_service.advancement_due_for_player(state, rules, state.owner_id)
    page = level_up_service.page_view(
        client.app.state.engine,
        campaign_id,
        rules,
        client.app.state.content.world_content("testhollow", DND),
        "Rook",
    )
    assert page is not None
    assert page.kind == level_up_service.LEVEL
    assert page.summary["milestone"] is True


def test_each_leveling_mode_refuses_the_other_tools_award(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _dnd_campaign(client_factory)
    ctx = _dnd_context(client.app, campaign_id)

    with pytest.raises(InvalidInput, match="experience leveling"):
        dnd_srd_2024.milestone_level("Rook", ctx)

    _set_leveling(client, campaign_id, "Milestone")

    with pytest.raises(InvalidInput, match="milestone_level"):
        dnd_srd_2024.award_xp("Rook", 50, ctx)


def test_experience_is_awarded_for_anything_the_gm_counts_as_a_challenge(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _dnd_campaign(client_factory)
    ctx = _dnd_context(client.app, campaign_id)

    result = dnd_srd_2024.award_xp("Rook", 50, ctx, "talking the reeve into opening the gate")

    assert result["gained"] == 50
    assert open_campaign(client.app, campaign_id).characters["rook"].counters["xp"] == 50
    with Session(client.app.state.engine) as db:
        rows = db.exec(select(Event).where(Event.campaign_id == campaign_id)).all()
    changes = [e for row in rows if (e := decode(row)).kind == "counter.changed"]
    awarded = [
        e for e in changes if isinstance(e, CounterChanged) and e.counter == "xp" and e.after == 50
    ]
    assert awarded[0].cause == "talking the reeve into opening the gate"


def test_a_hireling_cannot_take_a_milestone_level(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _dnd_campaign(client_factory)
    _set_leveling(client, campaign_id, "Milestone")
    tools = tools_for(client.app, campaign_id, rule_set=DND)
    tools.create_location("Yard", "A yard behind the inn.")
    place_pc(client, campaign_id, location="Yard", name="Rook")
    tools.hire_hireling(
        "Hesta",
        "Rook",
        0,
        "A steady hand.",
        character={"templates": {"class": "fighter"}},
    )
    ctx = _dnd_context(client.app, campaign_id)

    with pytest.raises(InvalidInput, match="hirelings follow"):
        dnd_srd_2024.milestone_level("Hesta", ctx)


def test_wwn_shows_its_own_track_threshold(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id = _wwn_campaign(client_factory, options={"campaign-xp_rate": "slow"})
    tools_for(app, campaign_id, rule_set=WWN).adjust_counter("Wren", "xp", 3, "a session")
    rules = rules_for(WWN)
    state = open_campaign(app, campaign_id)

    scene = scene_view(
        state,
        rules.character,
        conditions=rules.conditions,
        counters=rules.counters,
        level_up=next(iter(rules.level_ups.values())),
    )

    identity = dict(scene.pc_sheet.identity)
    assert identity["Experience"] == "3 / 6 (3 to go)"
