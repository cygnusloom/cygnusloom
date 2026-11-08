"""The per-campaign option that turns the rule set's inventory space on or off."""

from __future__ import annotations

import json
import random
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.db.models import Campaign
from cygnusloom.services import turns
from cygnusloom.services.campaigns import set_campaign_inventory_space
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import current, load_state
from cygnusloom.services.inventory import capacity_of, tracks_inventory
from cygnusloom.services.views import world_state_text
from tests.conftest import csrf_token
from tests.factories import campaign_ready_fast
from tests.test_combat import CAIRN, PC, YARD, _character, _fight, _fight_client
from tests.test_export_import import _export, _import, _imported_id, _target_app, _target_client
from tests.test_inventory import CAPACITY, _fill, _used
from tests.test_wwn_refactor import WREN, WWN, _empty_pack, _wwn_campaign, _wwn_pc, _wwn_tools


def _flag(app: FastAPI, campaign_id: int) -> bool:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign.use_inventory_space


def _owner_id(app: FastAPI, campaign_id: int) -> int:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign.owner_id


def _set_space(client: TestClient, campaign_id: int, enabled: bool):
    """Post the Settings checkbox. An unchecked box sends no field at all."""
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    data = {"csrf_token": token}
    if enabled:
        data["use_inventory_space"] = "1"
    return client.post(
        f"/campaigns/{campaign_id}/inventory-space",
        data=data,
        follow_redirects=False,
    )


def _disable(app: FastAPI, campaign_id: int) -> None:
    set_campaign_inventory_space(app.state.engine, _owner_id(app, campaign_id), campaign_id, False)


def _inventory_panel(page: str) -> str:
    start = page.index('id="panel-inventory"')
    return page[start : page.index("</section>", start)]


def test_a_new_campaign_tracks_inventory_space_by_default(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    assert _flag(client.app, campaign_id) is True
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'name="use_inventory_space" value="1" checked' in page
    assert page.index('id="view-settings"') < page.index('name="use_inventory_space"')


def test_tracks_inventory_keeps_the_rule_set_limits_without_a_campaign_row(
    client_factory: Callable[[], TestClient],
):
    client, _ = campaign_ready_fast(client_factory)
    with Session(client.app.state.engine) as db:
        assert tracks_inventory(db, 10_000) is True


def test_the_owner_turns_inventory_space_off_and_on(client_factory: Callable[[], TestClient]):
    client, campaign_id = campaign_ready_fast(client_factory)

    assert _set_space(client, campaign_id, False).status_code == 303
    assert _flag(client.app, campaign_id) is False
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'name="use_inventory_space" value="1" checked' not in page
    assert 'name="use_inventory_space" value="1"' in page

    assert _set_space(client, campaign_id, True).status_code == 303
    assert _flag(client.app, campaign_id) is True


def test_a_member_cannot_turn_inventory_space_off(client_factory: Callable[[], TestClient]):
    from tests.test_multiplayer import _joined_campaign

    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    token = csrf_token(guest, "/campaigns")
    response = guest.post(
        f"/campaigns/{campaign_id}/inventory-space",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert _flag(owner.app, campaign_id) is True


def test_the_inventory_tab_says_space_is_not_tracked(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _fight_client(client_factory, rolled=False)
    used = _used(client.app, campaign_id)
    assert _set_space(client, campaign_id, False).status_code == 303
    panel = _inventory_panel(client.get(f"/campaigns/{campaign_id}").text)
    assert f"Carrying {used} slots; inventory space is not tracked." in panel
    assert f"Carrying {used} of" not in panel


def test_the_gm_state_and_turn_prompt_say_space_is_not_tracked(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _fight_client(client_factory, rolled=False)
    _disable(client.app, campaign_id)
    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None

    text = world_state_text(
        state,
        CAIRN.campaign,
        CAIRN.grid,
        CAIRN.combat,
        CAIRN.conditions,
        clock_rules=CAIRN.clock,
        inventory=CAIRN.inventory,
        hooks=CAIRN.hooks,
        counters=CAIRN.counters,
        tallies=CAIRN.tallies,
        track_inventory=False,
    )
    assert "Inventory: not tracked" in text
    assert "Inventory space is not tracked in this campaign" in text
    assert " of 10 slots" not in text

    run = turns._start_run(
        client.app.state.engine,
        client.app.state.providers,
        client.app.state.content,
        CAIRN,
        campaign,
        100_000,
        random.Random(0),
    )
    assert "Inventory space is not tracked in this campaign" in turns._world_text(run, state)


def test_cairn_does_not_drain_hp_when_space_is_not_tracked(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    _disable(app, campaign_id)
    before = current(_character(app, campaign_id, PC), "hp")

    needed = CAPACITY - _used(app, campaign_id)
    results = [tools.give_item(PC, f"Rope {number}") for number in range(needed)]
    assert _used(app, campaign_id) == CAPACITY
    extra = tools.give_item(PC, "Lantern")

    assert all("drained" not in result for result in results)
    assert "drained" not in extra
    assert extra.get("over_capacity") is not True
    assert current(_character(app, campaign_id, PC), "hp") == before


def test_a_wwn_character_carries_past_its_pools_when_space_is_not_tracked(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _wwn_campaign(client_factory)
    _empty_pack(client, campaign_id)
    _disable(client.app, campaign_id)
    tools = _wwn_tools(client.app, campaign_id)
    before = _wwn_pc(client, campaign_id)
    capacity = capacity_of(before, WWN.inventory, WWN.character)
    assert capacity == before.stats["str"] + before.stats["str"] // 2

    results = [tools.give_item(WREN, f"Rope {number}") for number in range(capacity + 1)]

    held = _wwn_pc(client, campaign_id)
    assert sum(item.slots for item in held.items) == capacity + 1
    assert sum(item.slots for item in held.items) > before.stats["str"]
    assert WWN.inventory.penalty_item not in [item.kind for item in held.items]
    assert results[-1].get("over_capacity") is not True


def test_a_wwn_character_still_meets_its_pools_when_space_is_tracked(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _wwn_campaign(client_factory)
    _empty_pack(client, campaign_id)
    tools = _wwn_tools(client.app, campaign_id)
    strength = _wwn_pc(client, campaign_id).stats["str"]

    for number in range(strength):
        tools.give_item(WREN, f"Rope {number}")
    with pytest.raises(InvalidInput, match="Stowed pool is full"):
        tools.give_item(WREN, "One Rope Too Many")


def test_readying_ignores_the_pool_limit_but_not_the_bulky_rule_when_untracked(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _wwn_campaign(client_factory)
    _empty_pack(client, campaign_id)
    _disable(client.app, campaign_id)
    tools = _wwn_tools(client.app, campaign_id)
    readied_capacity = _wwn_pc(client, campaign_id).stats["str"] // 2

    for number in range(readied_capacity + 1):
        tools.give_item(WREN, f"Rope {number}")
        tools.ready_item(WREN, f"Rope {number}")
    held = _wwn_pc(client, campaign_id)
    assert sum(1 for item in held.items if item.ready) == readied_capacity + 1

    tools.give_item(WREN, "Boulder", size="massive")
    with pytest.raises(InvalidInput, match="too bulky"):
        tools.ready_item(WREN, "Boulder")


def test_a_purchase_succeeds_past_capacity_when_space_is_not_tracked(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    _disable(app, campaign_id)
    currency = CAIRN.marketplace.currency
    tools.change_currency(PC, 100, cause="a test")
    _fill(app, campaign_id, tools)
    before = _character(app, campaign_id, PC).features[currency]

    result = tools.buy_item(PC, "Moonstone", price=5)

    assert result["price"] == 5
    assert _character(app, campaign_id, PC).features[currency] == before - 5
    assert "Moonstone" in [item.name for item in _character(app, campaign_id, PC).items]


def test_taking_an_object_succeeds_past_capacity_when_space_is_not_tracked(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    _disable(app, campaign_id)
    _fill(app, campaign_id, tools)
    tools.place_object(YARD, "a brass lamp", False, "a test", item="Lantern")

    tools.take_object("a brass lamp", PC, "a test")

    assert "Lantern" in [item.name for item in _character(app, campaign_id, PC).items]


def test_turning_space_off_keeps_an_existing_penalty(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    tools.add_penalty(PC, "a spell")
    kinds = [item.kind for item in _character(app, campaign_id, PC).items]
    assert "fatigue" in kinds

    _disable(app, campaign_id)

    kinds = [item.kind for item in _character(app, campaign_id, PC).items]
    assert kinds.count("fatigue") == 1


def test_export_and_import_round_trip_the_inventory_space_flag(
    tmp_path, settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    assert _set_space(client, campaign_id, False).status_code == 303

    response = _export(client, campaign_id)

    assert response.json()["campaign"]["use_inventory_space"] is False
    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    new_id = _imported_id(_import(target_client, response.content))
    assert _flag(target_app, new_id) is False


def test_an_old_export_without_the_flag_imports_with_space_tracked(
    tmp_path, settings, client_factory: Callable[[], TestClient]
):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _export(client, campaign_id).json()
    document["campaign"].pop("use_inventory_space")

    target_app = _target_app(tmp_path, settings)
    target_client = _target_client(target_app)
    new_id = _imported_id(_import(target_client, json.dumps(document).encode()))
    assert _flag(target_app, new_id) is True
