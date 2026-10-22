from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import current
from cygnusforge.services.world import WorldTools
from tests.test_combat import CAIRN, PC, _character, _fight

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
    result = tools.give_item(PC, "Lantern")
    assert result["slots_used"] == before + 1
    assert result["capacity"] == CAPACITY
    lantern = [i for i in _character(app, campaign_id, PC).items if i.name == "Lantern"]
    assert [(i.size, i.slots) for i in lantern] == [("normal", 1)]


def test_bulky_items_take_two_slots_and_petty_items_none(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    before = _used(app, campaign_id)
    assert tools.give_item(PC, "Ladder", size="bulky")["slots_used"] == before + 2
    assert tools.give_item(PC, "Coin", size="petty")["slots_used"] == before + 2
    sizes = {i.name: (i.size, i.slots) for i in _character(app, campaign_id, PC).items}
    assert sizes["Ladder"] == ("bulky", 2)
    assert sizes["Coin"] == ("petty", 0)


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
    result = tools.add_fatigue(PC, "a spell")
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
    tools.add_fatigue(PC, "a spell")
    with pytest.raises(InvalidInput, match="lasts until"):
        tools.drop_item(PC, "Fatigue")


def test_a_full_night_rest_clears_fatigue(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.add_fatigue(PC, "a spell")
    kinds = [i.kind for i in _character(app, campaign_id, PC).items]
    assert "fatigue" in kinds
    tools.rest("night", reason="camp")
    kinds = [i.kind for i in _character(app, campaign_id, PC).items]
    assert "fatigue" not in kinds
