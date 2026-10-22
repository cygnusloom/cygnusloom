from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from cygnusforge.services.combat import MAX_ARMOR
from tests.test_campaigns import _create_character
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_combat import CAIRN, _character


def _new_character(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client, campaign_id, name="Ada", fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    return client.app, campaign_id


def test_starting_rations_are_an_item_with_three_uses(client_factory):
    app, campaign_id = _new_character(client_factory)
    rations = [i for i in _character(app, campaign_id, "Ada").items if i.name == "Rations"]
    assert len(rations) == 1
    assert rations[0].uses == 3
    assert rations[0].max_uses == 3


def test_starting_gold_is_a_count_not_an_item(client_factory):
    app, campaign_id = _new_character(client_factory)
    names = [i.name.lower() for i in _character(app, campaign_id, "Ada").items]
    assert CAIRN.marketplace.currency not in names
    assert "gold" not in names


def test_starting_armor_is_recorded_and_capped(client_factory):
    app, campaign_id = _new_character(client_factory)
    assert 0 <= _character(app, campaign_id, "Ada").armor <= MAX_ARMOR


def test_starting_items_carry_their_real_slots(client_factory):
    app, campaign_id = _new_character(client_factory)
    items = _character(app, campaign_id, "Ada").items
    assert items
    assert all(item.slots >= 0 for item in items)
    assert sum(item.slots for item in items) <= CAIRN.inventory.capacity
