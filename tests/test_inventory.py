from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.content.library import InventoryPool, InventoryRules
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import ItemAdded, current, record
from cygnusforge.services.market import listed_item
from cygnusforge.services.world import WorldTools
from tests.conftest import content_library
from tests.test_combat import CAIRN, PC, _character, _fight, _fight_client

CAPACITY = CAIRN.inventory.capacity


def _used(app: FastAPI, campaign_id: int) -> int:
    return sum(item.slots for item in _character(app, campaign_id, PC).items)


def _fill(app: FastAPI, campaign_id: int, tools: WorldTools) -> list[dict[str, Any]]:
    """Give normal items until full; the last result is the one that fills it."""
    needed = CAPACITY - _used(app, campaign_id)
    return [tools.give_item(PC, f"Rope {n}") for n in range(needed)]


def test_give_item_adds_a_normal_item_for_one_slot(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    before = _used(app, campaign_id)
    lanterns = len([i for i in _character(app, campaign_id, PC).items if i.name == "Lantern"])
    result = tools.give_item(PC, "Lantern")
    assert result["slots_used"] == before + 1
    assert result["capacity"] == CAPACITY
    held = [i for i in _character(app, campaign_id, PC).items if i.name == "Lantern"]
    assert len(held) == lanterns + 1
    assert (held[-1].size, held[-1].slots) == ("normal", 1)


def test_a_catalogue_item_given_keeps_its_listed_uses(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    listed = listed_item(CAIRN.marketplace, "Rations")
    assert listed is not None and listed.uses > 0
    tools.give_item(PC, "Rations")
    rations = [i for i in _character(app, campaign_id, PC).items if i.name == listed.name]
    assert rations[-1].uses == rations[-1].max_uses == listed.uses


def test_bulky_items_take_two_slots_and_petty_items_none(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    before = _used(app, campaign_id)
    assert tools.give_item(PC, "Ladder", size="bulky")["slots_used"] == before + 2
    assert tools.give_item(PC, "Coin", size="petty")["slots_used"] == before + 2
    sizes = {i.name: (i.size, i.slots) for i in _character(app, campaign_id, PC).items}
    assert sizes["Ladder"] == ("bulky", 2)
    assert sizes["Coin"] == ("petty", 0)


def _with_world_features(tools: WorldTools) -> WorldTools:
    return replace(
        tools, features=content_library().world_content("twin-lakes", "cairn2e").features
    )


def test_a_bulky_catalogue_item_takes_its_listed_size_when_none_is_given(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    before = _used(app, campaign_id)
    assert tools.give_item(PC, "Chainmail")["slots_used"] == before + 2
    (held,) = [i for i in _character(app, campaign_id, PC).items if i.name == "Chainmail"]
    assert (held.size, held.slots) == ("bulky", 2)


def test_a_bulky_world_item_takes_its_listed_size_when_none_is_given(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = _with_world_features(tools)
    before = _used(app, campaign_id)
    result = tools.give_item(PC, "Fletcher's Ire")
    assert result["slots_used"] == before + 2
    (held,) = [i for i in _character(app, campaign_id, PC).items if i.name == "Fletcher's Ire"]
    assert (held.size, held.slots) == ("bulky", 2)
    assert held.uses == 3


def test_a_petty_world_item_takes_no_slots_when_no_size_is_given(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = _with_world_features(tools)
    before = _used(app, campaign_id)
    assert tools.give_item(PC, "Moon Cloth")["slots_used"] == before
    (held,) = [i for i in _character(app, campaign_id, PC).items if i.name == "Moon Cloth"]
    assert (held.size, held.slots) == ("petty", 0)


def test_an_unknown_item_size_is_refused(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    with pytest.raises(InvalidInput):
        tools.give_item(PC, "Anvil", size="huge")


def test_filling_the_inventory_drains_hp_once(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    results = _fill(app, campaign_id, tools)
    assert results, "the PC must have room to fill"
    assert all("drained" not in r for r in results[:-1])
    assert results[-1]["drained"] == "hp"
    assert results[-1]["slots_used"] == CAPACITY
    character = _character(app, campaign_id, PC)
    assert current(character, "hp") == 0


def test_items_after_the_full_inventory_do_not_drain_again(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _fill(app, campaign_id, tools)
    extra = tools.give_item(PC, "Lantern")
    assert "drained" not in extra


def test_going_over_capacity_is_reported_without_a_second_drain(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _fill(app, campaign_id, tools)
    result = tools.give_item(PC, "Lantern")
    assert result["over_capacity"] is True
    assert result["slots_used"] == CAPACITY + 1
    assert "drained" not in result


def test_fatigue_on_a_full_inventory_is_still_recorded(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _fill(app, campaign_id, tools)
    result = tools.add_penalty(PC, "a spell")
    assert result["over_capacity"] is True
    fatigue = [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]
    assert len(fatigue) == 1


def test_drop_item_removes_a_carried_item_and_its_slots(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_item(PC, "Rope")
    before = _used(app, campaign_id)
    result = tools.drop_item(PC, "Rope")
    assert result["slots_used"] == before - 1
    assert "Rope" not in [i.name for i in _character(app, campaign_id, PC).items]


def test_drop_item_refuses_an_item_not_carried(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    with pytest.raises(InvalidInput):
        tools.drop_item(PC, "Unicorn")


def test_fatigue_cannot_be_dropped_before_a_full_rest(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    tools.add_penalty(PC, "a spell")
    with pytest.raises(InvalidInput, match="lasts until"):
        tools.drop_item(PC, "Fatigue")


def test_a_full_night_rest_clears_fatigue(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.add_penalty(PC, "a spell")
    kinds = [i.kind for i in _character(app, campaign_id, PC).items]
    assert "fatigue" in kinds
    tools.rest("night", reason="camp")
    kinds = [i.kind for i in _character(app, campaign_id, PC).items]
    assert "fatigue" not in kinds


def _add_consumable(app: FastAPI, campaign_id: int, item: str, uses: int, kind: str = "") -> None:
    with Session(app.state.engine) as db:
        record(
            db,
            campaign_id,
            ItemAdded(
                name=PC,
                item=item,
                size="normal",
                slots=1,
                kind_of_item=kind,
                cause="a test",
                uses=uses,
                max_uses=uses,
            ),
        )
        db.commit()


def test_use_item_spends_one_use_and_keeps_the_item(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_consumable(app, campaign_id, "Torch", 3)
    result = tools.use_item(PC, "Torch")
    assert result["uses_left"] == 2
    assert result["used_up"] is False
    torch = next(i for i in _character(app, campaign_id, PC).items if i.name == "Torch")
    assert torch.uses == 2


def test_use_item_on_the_last_use_removes_the_item(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_consumable(app, campaign_id, "Torch", 1)
    before = _used(app, campaign_id)
    result = tools.use_item(PC, "Torch")
    assert result["used_up"] is True
    assert result["slots_used"] == before - 1
    assert "Torch" not in [i.name for i in _character(app, campaign_id, PC).items]


def test_use_item_refuses_an_item_with_no_uses(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    tools.give_item(PC, "Rope")
    with pytest.raises(InvalidInput, match="not a consumable"):
        tools.use_item(PC, "Rope")


def test_use_item_refuses_an_item_not_carried(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="carries no"):
        tools.use_item(PC, "Unicorn")


def test_use_item_refuses_a_relic(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_consumable(app, campaign_id, "Sun Charm", 3, kind=CAIRN.magic.charged.kind)
    with pytest.raises(InvalidInput, match="use_charge"):
        tools.use_item(PC, "Sun Charm")


def test_the_campaign_page_lists_the_pack(client_factory: Callable[[], TestClient]):
    client, campaign_id, tools = _fight_client(client_factory, rolled=False)
    tools.give_item(PC, "Rope")
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'id="tab-inventory"' in page
    start = page.index('id="panel-inventory"')
    inventory = page[start : page.index("</section>", start)]
    assert f"Carrying {_used(client.app, campaign_id)} of {CAPACITY} slots." in inventory
    assert "<strong>Rope</strong>" in inventory


def _empty_pack(app: FastAPI, campaign_id: int, tools: WorldTools) -> None:
    """Drop the character's starting gear so a pool test starts with nothing carried."""
    for held in list(_character(app, campaign_id, PC).items):
        tools.drop_item(PC, held.name)


def _pooled(tools: WorldTools) -> WorldTools:
    """A single campaign using two named pools: two Readied slots and four Stowed."""
    return replace(
        tools,
        inventory=InventoryRules(
            capacity=0,
            penalty_name="Overloaded",
            penalty_lasts="until rest",
            penalty_item="Overload",
            penalty_slots=0,
            pools=(
                InventoryPool(id="readied", name="Readied", ready=True, capacity=2),
                InventoryPool(id="stowed", name="Stowed", capacity=4),
            ),
            default_pool="stowed",
        ),
    )


def test_named_pools_hold_items_and_readying_moves_one_to_the_readied_pool(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    tools = _pooled(tools)
    _empty_pack(app, campaign_id, tools)
    first = tools.give_item(PC, "Lantern")
    assert first["pool"] == "stowed"
    assert first["pool_slots"] == 1

    result = tools.ready_item(PC, "Lantern")
    assert result["ready"] is True
    assert result["pool"] == "readied"
    assert result["readied_slots"] == 1
    assert _character(app, campaign_id, PC).items[0].ready is True


def test_a_full_readied_pool_refuses_another_item(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = _pooled(tools)
    _empty_pack(app, campaign_id, tools)
    tools.give_item(PC, "Lantern")
    tools.give_item(PC, "Rope")
    tools.ready_item(PC, "Lantern")
    tools.ready_item(PC, "Rope")
    tools.give_item(PC, "Torch")

    with pytest.raises(InvalidInput, match="Readied pool has no room"):
        tools.ready_item(PC, "Torch")


def test_a_bulky_item_cannot_be_readied(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _empty_pack(app, campaign_id, tools)
    rules = replace(
        _pooled(tools).inventory,
        size_pools={"bulky": "stowed"},
    )
    tools = replace(tools, inventory=rules)
    tools.give_item(PC, "Ladder", size="bulky")

    with pytest.raises(InvalidInput, match="too bulky"):
        tools.ready_item(PC, "Ladder")


def test_a_stowed_item_cannot_be_used_until_it_is_readied(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = _pooled(tools)
    _empty_pack(app, campaign_id, tools)
    tools.give_item(PC, "Rations")

    with pytest.raises(InvalidInput, match="stowed; ready it before use"):
        tools.use_item(PC, "Rations")

    tools.ready_item(PC, "Rations")
    assert tools.use_item(PC, "Rations")["uses_left"] >= 0


def test_a_named_stowed_pool_refuses_an_item_that_does_not_fit(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = _pooled(tools)
    _empty_pack(app, campaign_id, tools)
    tools.give_item(PC, "Ladder", size="bulky")
    tools.give_item(PC, "Rope", size="bulky")

    with pytest.raises(InvalidInput, match="Stowed pool is full"):
        tools.give_item(PC, "Anvil", size="bulky")
