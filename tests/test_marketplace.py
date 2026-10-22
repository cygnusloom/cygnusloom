from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.services.combat import MAX_ARMOR
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    ArmorSet,
    GoldChanged,
    ItemAdded,
    ItemRemoved,
    record,
)
from tests.test_campaigns import _create_character
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_clock_rest import _wound
from tests.test_combat import CAIRN, PC, _character, _set_stat, _tools
from tests.test_inventory import _fill

MARKET = CAIRN.marketplace
BRAN = "Bran"
PLACE = "The Road"


def _pcs(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    return client.app, campaign_id, _tools(client.app, campaign_id)


def _with_companion(tools) -> None:
    """A companion travelling with the PC. Campaigns have one player character, so a party is
    the PC plus NPCs."""
    tools.create_location(PLACE, "A road between towns.")
    tools.create_npc(BRAN, PLACE, "A hired companion.")


def _gold(app: FastAPI, campaign_id: int, name: str = PC) -> int:
    return _character(app, campaign_id, name).features.get(MARKET.currency, 0)


def _grant_gold(app: FastAPI, campaign_id: int, amount: int, name: str = PC) -> None:
    with Session(app.state.engine) as db:
        record(
            db,
            campaign_id,
            GoldChanged(name=name, feature=MARKET.currency, amount=amount, cause="a test"),
        )
        db.commit()


def _set_rations(app: FastAPI, campaign_id: int, uses: int, name: str = PC) -> None:
    """Replace the character's rations with one pack of `uses`, or none when uses is 0."""
    with Session(app.state.engine) as db:
        record(db, campaign_id, ItemRemoved(name=name, item="Rations"))
        if uses:
            record(
                db,
                campaign_id,
                ItemAdded(
                    name=name,
                    item="Rations",
                    size="normal",
                    slots=1,
                    cause="a test",
                    uses=uses,
                    max_uses=uses,
                ),
            )
        db.commit()


def _rations(app: FastAPI, campaign_id: int, name: str = PC) -> list:
    return [item for item in _character(app, campaign_id, name).items if item.name == "Rations"]


def _listed(predicate: Callable[..., bool]):
    return next(item for item in MARKET.items if predicate(item))


def _cheap_normal_item():
    return _listed(lambda item: item.size == "normal" and item.price <= 10 and item.armor == 0)


def test_a_catalogue_item_costs_its_listed_price(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    before = _gold(app, campaign_id)
    listed = _cheap_normal_item()
    result = tools.buy_item(PC, listed.name)
    assert result["price"] == listed.price
    assert _gold(app, campaign_id) == before - listed.price
    assert listed.name in [item.name for item in _character(app, campaign_id, PC).items]


def test_a_catalogue_price_that_disagrees_with_the_listing_is_refused(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    listed = _cheap_normal_item()
    before = _gold(app, campaign_id)
    with pytest.raises(InvalidInput):
        tools.buy_item(PC, listed.name, price=listed.price + 1)
    assert _gold(app, campaign_id) == before


def test_a_catalogue_size_that_disagrees_with_the_listing_is_refused(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    listed = _cheap_normal_item()
    with pytest.raises(InvalidInput):
        tools.buy_item(PC, listed.name, size="bulky")


def test_an_off_catalogue_item_needs_a_price(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    with pytest.raises(InvalidInput):
        tools.buy_item(PC, "Moonstone")


def test_an_off_catalogue_item_costs_the_price_the_warden_sets(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    before = _gold(app, campaign_id)
    result = tools.buy_item(PC, "Moonstone", price=20)
    assert result["price"] == 20
    assert _gold(app, campaign_id) == before - 20
    moonstone = [i for i in _character(app, campaign_id, PC).items if i.name == "Moonstone"]
    assert [(i.size, i.slots) for i in moonstone] == [("normal", 1)]


def test_buying_beyond_the_gold_is_refused_and_nothing_is_carried(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    before_gold = _gold(app, campaign_id)
    before_items = len(_character(app, campaign_id, PC).items)
    with pytest.raises(InvalidInput):
        tools.buy_item(PC, "Moonstone", price=before_gold + 1)
    character = _character(app, campaign_id, PC)
    assert _gold(app, campaign_id) == before_gold
    assert len(character.items) == before_items


def test_a_purchase_that_does_not_fit_is_refused_and_nothing_is_paid(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    _fill(app, campaign_id, tools)
    before = _gold(app, campaign_id)
    with pytest.raises(InvalidInput):
        tools.buy_item(PC, "Moonstone", price=5)
    assert _gold(app, campaign_id) == before


def test_buying_armor_sets_armor_and_stops_at_the_cap(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    with Session(app.state.engine) as db:
        record(db, campaign_id, ArmorSet(name=PC, armor=MAX_ARMOR))
        db.commit()
    armor = _listed(lambda item: item.armor > 0 and item.size == "normal")
    result = tools.buy_item(PC, armor.name)
    assert result["armor"] == MAX_ARMOR
    assert _character(app, campaign_id, PC).armor == MAX_ARMOR


def test_a_service_is_paid_by_the_first_named_character(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _with_companion(tools)
    _grant_gold(app, campaign_id, 100, name=BRAN)
    pc_before = _gold(app, campaign_id)
    result = tools.buy_service([BRAN, PC], "room")
    assert result["payer"] == BRAN
    assert result["price"] == 10 * 2
    assert _gold(app, campaign_id, BRAN) == 100 - 10 * 2
    assert _gold(app, campaign_id) == pc_before


def test_lodging_for_a_group_takes_a_room_for_each_fits_characters(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _with_companion(tools)
    _grant_gold(app, campaign_id, 100)
    before = _gold(app, campaign_id)
    result = tools.buy_service([PC, BRAN], "private_room")
    assert result["price"] == 35
    assert _gold(app, campaign_id) == before - 35
    assert _character(app, campaign_id, PC).lodged
    assert _character(app, campaign_id, BRAN).lodged


def test_lodging_without_enough_gold_is_refused_and_nobody_is_lodged(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _with_companion(tools)
    assert _gold(app, campaign_id) < 35
    with pytest.raises(InvalidInput):
        tools.buy_service([PC, BRAN], "private_room")
    assert not _character(app, campaign_id, PC).lodged
    assert not _character(app, campaign_id, BRAN).lodged


def test_medical_healing_restores_only_the_patients_and_the_payer_pays_once(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _with_companion(tools)
    _grant_gold(app, campaign_id, 100, name=BRAN)
    _set_stat(app, campaign_id, PC, "str", 1)
    assert _character(app, campaign_id, PC).harm.get("str", 0) > 0
    result = tools.buy_service([BRAN, PC], "medical_healing")
    assert result["served"] == [PC]
    assert result["payer"] == BRAN
    assert result["price"] == 50
    assert _character(app, campaign_id, PC).harm.get("str", 0) == 0
    assert _gold(app, campaign_id, BRAN) == 100 - 50
    assert _gold(app, campaign_id) < 100


def test_medical_healing_is_refused_when_nobody_named_is_hurt(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    before = _gold(app, campaign_id)
    with pytest.raises(InvalidInput):
        tools.buy_service([PC], "medical_healing")
    assert _gold(app, campaign_id) == before


def test_a_deferred_service_is_refused(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    with pytest.raises(InvalidInput):
        tools.buy_service([PC], "stable")


def test_an_unknown_service_names_what_is_sold(client_factory):
    _app, _campaign_id, tools = _pcs(client_factory)
    with pytest.raises(InvalidInput, match="Room & Board"):
        tools.buy_service([PC], "tavern")


def test_a_night_rest_eats_one_use_of_the_starting_rations(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    before = _rations(app, campaign_id)[0].uses
    tools.rest("night", reason="camp")
    assert _rations(app, campaign_id)[0].uses == before - 1


def test_the_last_use_of_a_ration_removes_the_pack(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _set_rations(app, campaign_id, uses=1)
    tools.rest("night", reason="camp")
    assert _rations(app, campaign_id) == []


def test_a_rest_with_no_food_leaves_the_character_deprived(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _set_rations(app, campaign_id, uses=0)
    tools.rest("night", reason="camp")
    assert _character(app, campaign_id, PC).deprived_since is not None


def test_a_lodged_character_eats_nothing_and_the_bed_is_used_up(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    tools.buy_service([PC], "room")
    before = _rations(app, campaign_id)[0].uses
    tools.rest("night", reason="camp")
    assert _rations(app, campaign_id)[0].uses == before
    assert not _character(app, campaign_id, PC).lodged


def test_a_moments_rest_eats_nothing(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    before = _rations(app, campaign_id)[0].uses
    tools.rest("moments", reason="watch")
    assert _rations(app, campaign_id)[0].uses == before


def test_a_wounded_character_who_eats_still_heals(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _wound(app, campaign_id)
    tools.rest("night", reason="camp")
    assert _character(app, campaign_id, PC).harm.get("hp", 0) == 0


def test_the_starting_pack_is_an_item_with_its_uses(client_factory):
    app, campaign_id, _ = _pcs(client_factory)
    packs = _rations(app, campaign_id)
    assert len(packs) == 1
    assert packs[0].uses == CAIRN.food.uses


def test_search_needs_every_word_to_match(client_factory):
    _app, _campaign_id, tools = _pcs(client_factory)
    found = tools.search_marketplace("dagger")
    assert "Dagger" in [item["item"] for item in found["items"]]
    assert tools.search_marketplace("dagger zzzzqq")["items"] == []


def test_search_lists_services_by_name(client_factory):
    _app, _campaign_id, tools = _pcs(client_factory)
    found = tools.search_marketplace("medical")
    assert [service["service"] for service in found["services"]] == ["medical_healing"]
