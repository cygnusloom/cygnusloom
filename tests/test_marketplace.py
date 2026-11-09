from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.config import Settings
from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import DATA_DIR, ContentLibrary
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    ArmorSet,
    CurrencyChanged,
    ItemAdded,
    ItemRemoved,
    record,
)
from tests.conftest import bundled_library, csrf_token
from tests.support import content_tree
from tests.test_campaigns import _create_character, _debug_client_factory
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_clock_rest import _wound
from tests.test_combat import CAIRN, PC, _character, _set_stat, _tools
from tests.test_inventory import _fill

MARKET = CAIRN.marketplace
ARMOR_MAX = CAIRN.combat.armor_max
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
            CurrencyChanged(name=name, feature=MARKET.currency, amount=amount, cause="a test"),
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
        tools.buy_item(PC, "Moonstone", size="normal")


def test_an_off_catalogue_item_needs_an_explicit_size(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    with pytest.raises(InvalidInput, match="buy_service"):
        tools.buy_item(PC, "Moonstone", price=20)


def test_an_off_catalogue_item_costs_the_price_the_warden_sets(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    before = _gold(app, campaign_id)
    result = tools.buy_item(PC, "Moonstone", price=20, size="normal")
    assert result["price"] == 20
    assert _gold(app, campaign_id) == before - 20
    moonstone = [i for i in _character(app, campaign_id, PC).items if i.name == "Moonstone"]
    assert [(i.size, i.slots) for i in moonstone] == [("normal", 1)]


def test_buying_beyond_the_gold_is_refused_and_nothing_is_carried(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    before_gold = _gold(app, campaign_id)
    before_items = len(_character(app, campaign_id, PC).items)
    with pytest.raises(InvalidInput):
        tools.buy_item(PC, "Moonstone", price=before_gold + 1, size="normal")
    character = _character(app, campaign_id, PC)
    assert _gold(app, campaign_id) == before_gold
    assert len(character.items) == before_items


def test_a_purchase_that_does_not_fit_is_refused_and_nothing_is_paid(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    _fill(app, campaign_id, tools)
    before = _gold(app, campaign_id)
    with pytest.raises(InvalidInput):
        tools.buy_item(PC, "Moonstone", price=5, size="normal")
    assert _gold(app, campaign_id) == before


def test_buying_armor_sets_armor_and_stops_at_the_cap(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    with Session(app.state.engine) as db:
        record(db, campaign_id, ArmorSet(name=PC, armor=ARMOR_MAX))
        db.commit()
    armor = _listed(lambda item: item.armor > 0 and item.size == "normal")
    result = tools.buy_item(PC, armor.name)
    assert result["armor"] == ARMOR_MAX
    assert _character(app, campaign_id, PC).armor == ARMOR_MAX


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


def test_a_listed_service_refuses_a_supplied_price(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    with pytest.raises(InvalidInput, match="leave price out"):
        tools.buy_service([PC], "room", price=1)


def test_an_unlisted_service_costs_the_price_the_warden_sets_and_carries_nothing(
    client_factory,
):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    before = _gold(app, campaign_id)
    before_items = len(_character(app, campaign_id, PC).items)
    result = tools.buy_service([PC], "Back room and a hot meal for two", price=1)
    assert result["service"] == "Back room and a hot meal for two"
    assert result["served"] == [PC]
    assert result["payer"] == PC
    assert result["price"] == 1
    assert result["currency_left"] == before - 1
    assert result["currency"] == MARKET.currency
    assert _gold(app, campaign_id) == before - 1
    assert len(_character(app, campaign_id, PC).items) == before_items


def test_an_unlisted_service_serves_each_character_once_and_the_first_pays(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _with_companion(tools)
    _grant_gold(app, campaign_id, 100, name=BRAN)
    pc_before = _gold(app, campaign_id)
    result = tools.buy_service([BRAN, BRAN, PC], "Passage across the river", price=4)
    assert result["served"] == [BRAN, PC]
    assert result["payer"] == BRAN
    assert result["price"] == 4
    assert _gold(app, campaign_id, BRAN) == 96
    assert _gold(app, campaign_id) == pc_before


def test_shelter_for_a_group_takes_a_room_for_each_fits_characters(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _with_companion(tools)
    _grant_gold(app, campaign_id, 100)
    before = _gold(app, campaign_id)
    result = tools.buy_service([PC, BRAN], "private_room")
    assert result["price"] == 35
    assert _gold(app, campaign_id) == before - 35
    assert _character(app, campaign_id, PC).marks.get("sheltered")
    assert _character(app, campaign_id, BRAN).marks.get("sheltered")


def test_shelter_without_enough_gold_is_refused_and_nobody_is_sheltered(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _with_companion(tools)
    assert _gold(app, campaign_id) < 35
    with pytest.raises(InvalidInput):
        tools.buy_service([PC, BRAN], "private_room")
    assert not _character(app, campaign_id, PC).marks.get("sheltered")
    assert not _character(app, campaign_id, BRAN).marks.get("sheltered")


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


def test_medical_healing_also_restores_hp(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    _set_stat(app, campaign_id, PC, "hp", 0)
    assert _character(app, campaign_id, PC).harm.get("hp", 0) > 0
    result = tools.buy_service([PC], "medical_healing")
    assert result["served"] == [PC]
    assert _character(app, campaign_id, PC).harm.get("hp", 0) == 0


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


def test_an_unlisted_service_needs_a_price(client_factory):
    _app, _campaign_id, tools = _pcs(client_factory)
    with pytest.raises(InvalidInput, match="pass the price"):
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
    assert "deprived" in _character(app, campaign_id, PC).marks


def test_a_sheltered_character_eats_nothing_and_the_bed_is_used_up(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 100)
    tools.buy_service([PC], "room")
    before = _rations(app, campaign_id)[0].uses
    tools.rest("night", reason="camp")
    assert _rations(app, campaign_id)[0].uses == before
    assert not _character(app, campaign_id, PC).marks.get("sheltered")


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
    assert packs[0].uses == CAIRN.character.features[CAIRN.hooks.options["feature"]].uses


def test_search_needs_every_word_to_match(client_factory):
    _app, _campaign_id, tools = _pcs(client_factory)
    found = tools.search_marketplace("dagger")
    assert "Dagger" in [item["item"] for item in found["items"]]
    assert tools.search_marketplace("dagger zzzzqq")["items"] == []


def test_search_lists_services_by_name(client_factory):
    _app, _campaign_id, tools = _pcs(client_factory)
    found = tools.search_marketplace("medical")
    assert [service["service"] for service in found["services"]] == ["medical_healing"]


def _give_debug_gold(client: TestClient, campaign_id: int, amount: str, name: str = PC):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/debug/currency",
        data={"csrf_token": token, "name": name, "amount": amount},
        follow_redirects=False,
    )


def test_debug_gold_is_hidden_by_default(client_factory):
    client, campaign_id = _cairn_campaign(client_factory)
    assert _give_debug_gold(client, campaign_id, "5").status_code == 404


def test_debug_gold_adds_the_rule_sets_currency(settings: Settings, fake: FakeProvider):
    client, campaign_id = _cairn_campaign(_debug_client_factory(settings, fake))
    created = _create_character(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    app = client.app
    before = _gold(app, campaign_id)
    assert _give_debug_gold(client, campaign_id, "40").status_code == 303
    assert _gold(app, campaign_id) == before + 40
    assert f"Give {MARKET.currency}" in client.get(f"/campaigns/{campaign_id}").text


def test_debug_gold_refuses_a_bad_amount(settings: Settings, fake: FakeProvider):
    client, campaign_id = _cairn_campaign(_debug_client_factory(settings, fake))
    created = _create_character(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    assert _give_debug_gold(client, campaign_id, "lots").status_code == 400
    assert _give_debug_gold(client, campaign_id, "0").status_code == 400
    assert _give_debug_gold(client, campaign_id, "5", name="Nobody").status_code == 400


def test_a_gift_of_gold_is_recorded_as_a_gold_change(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    before = _gold(app, campaign_id)
    result = tools.change_currency(PC, 7, "a widow's thanks")
    assert result["currency_left"] == before + 7
    assert _gold(app, campaign_id) == before + 7


def test_a_taking_of_gold_is_recorded_and_refused_without_enough(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _grant_gold(app, campaign_id, 10)
    before = _gold(app, campaign_id)
    result = tools.change_currency(PC, -4, "a tithe")
    assert result["currency_left"] == before - 4
    assert _gold(app, campaign_id) == before - 4
    with pytest.raises(InvalidInput):
        tools.change_currency(PC, -(before - 4 + 1), "a bribe too far")
    assert _gold(app, campaign_id) == before - 4


def test_a_zero_or_unknown_gold_change_is_refused(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    before = _gold(app, campaign_id)
    with pytest.raises(InvalidInput):
        tools.change_currency(PC, 0, "nothing")
    with pytest.raises(InvalidInput):
        tools.change_currency("Nobody", 5, "a gift")
    assert _gold(app, campaign_id) == before


def _feature_market(tmp_path: Path, shop_item: dict[str, Any]) -> ContentLibrary:
    """A rule set with a currency, one item feature, and one shop item to load."""
    return ContentLibrary.load(
        content_tree(
            tmp_path,
            definitions=[
                {"type": "item_size", "id": "petty", "slots": 0},
                {"type": "item_size", "id": "normal", "slots": 1},
                {"type": "item_size", "id": "bulky", "slots": 2},
                {
                    "type": "feature",
                    "id": "gold",
                    "name": "Gold Pieces",
                    "feature_type": "item",
                    "description": "A purse of coins.",
                    "size": "petty",
                },
                {
                    "type": "feature",
                    "id": "sword",
                    "name": "Sword",
                    "feature_type": "item",
                    "description": "A well-balanced blade.",
                    "size": "normal",
                    "armor": 1,
                    "damage": "1d6",
                    "uses": 2,
                    "looks_like": "sword",
                    "encumbrance_checks": ["sneak"],
                },
                {"type": "marketplace", "id": "market", "currency": "gold"},
                shop_item,
            ],
        )
    )


def test_a_shop_item_inherits_the_feature_it_names(tmp_path):
    library = _feature_market(
        tmp_path,
        {
            "type": "shop_item",
            "id": "sword",
            "feature": "sword",
            "category": "weapons",
            "price": 10,
        },
    )
    item = library.rule_sets["testset"].marketplace.items[0]
    assert item.name == "Sword"
    assert item.size == "normal"
    assert item.armor == 1
    assert item.damage == "1d6"
    assert item.uses == 2
    assert item.description == "A well-balanced blade."
    assert item.encumbrance_checks == ("sneak",)
    assert (item.icon, item.looks_like) == ("sword", "sword")
    # The shop keeps its own catalogue fields.
    assert (item.category, item.price, item.armor_mode) == ("weapons", 10, "add")


def test_a_shop_item_overrides_what_it_would_inherit(tmp_path):
    library = _feature_market(
        tmp_path,
        {
            "type": "shop_item",
            "id": "sword",
            "feature": "sword",
            "name": "Fine Sword",
            "category": "weapons",
            "price": 50,
            "size": "bulky",
            "armor": 2,
            "damage": "1d8",
            "uses": 3,
            "description": "A sword of folded steel.",
            "looks_like": "dagger",
            "encumbrance_checks": ["exert"],
            "armor_mode": "max",
        },
    )
    item = library.rule_sets["testset"].marketplace.items[0]
    assert item.name == "Fine Sword"
    assert item.size == "bulky"
    assert item.armor == 2
    assert item.damage == "1d8"
    assert item.uses == 3
    assert item.description == "A sword of folded steel."
    assert item.encumbrance_checks == ("exert",)
    assert (item.icon, item.looks_like) == ("dagger", "dagger")
    assert (item.category, item.price, item.armor_mode) == ("weapons", 50, "max")


def test_an_unknown_shop_item_feature_is_refused(tmp_path):
    with pytest.raises(ContentError, match="names no feature \\('moon-sword'\\)"):
        _feature_market(
            tmp_path,
            {
                "type": "shop_item",
                "id": "sword",
                "feature": "moon-sword",
                "category": "weapons",
                "price": 10,
            },
        )


BUNDLED_ITEM_RULE_SETS = {"cairn2e": 16, "worldswithoutnumber": 40}


def _bundled_shop_item_definitions(rule_set_id: str) -> list[dict[str, Any]]:
    definitions = []
    for path in sorted((DATA_DIR / "rulesets" / rule_set_id).glob("*.json")):
        entries = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(entries, list):
            definitions.extend(e for e in entries if e.get("type") == "shop_item")
    return definitions


@pytest.mark.parametrize("rule_set_id", sorted(BUNDLED_ITEM_RULE_SETS))
def test_bundled_shop_items_name_the_feature_that_defines_them(rule_set_id):
    """Every bundled item that matches a feature by id or name names that feature, inherits
    whatever it does not author, and authors anything that differs from the feature."""
    rule_set = bundled_library().rule_sets[rule_set_id]
    items = {item.id: item for item in rule_set.marketplace.items}
    item_features = {
        feature.name: feature
        for feature in rule_set.character.features.values()
        if feature.feature_type == "item"
    }
    derived = 0
    for definition in _bundled_shop_item_definitions(rule_set_id):
        item = items[definition["id"]]
        feature = rule_set.character.features.get(definition["id"]) or item_features.get(item.name)
        if feature is None:
            assert "feature" not in definition
            continue
        derived += 1
        assert definition.get("feature") == feature.id, definition["id"]
        for key in (
            "name",
            "size",
            "armor",
            "damage",
            "uses",
            "description",
            "encumbrance_checks",
        ):
            assert getattr(item, key) == definition.get(key, getattr(feature, key)), (
                definition["id"],
                key,
            )
        if "icon" in definition:
            assert item.icon == definition["icon"]
        elif "looks_like" in definition:
            assert item.looks_like == definition["looks_like"]
            assert item.icon == definition["looks_like"]
        else:
            assert (item.icon, item.looks_like) == (feature.icon, feature.looks_like)
    assert derived == BUNDLED_ITEM_RULE_SETS[rule_set_id]


def test_bundled_cairn_items_keep_their_catalogue_fields():
    items = {item.id: item for item in bundled_library().rule_sets["cairn2e"].marketplace.items}
    dagger = items["dagger"]
    assert (
        dagger.name,
        dagger.category,
        dagger.price,
        dagger.size,
        dagger.armor,
        dagger.damage,
        dagger.uses,
    ) == ("Dagger", "weapons", 5, "normal", 0, "1d6", 0)
    assert dagger.description == "Dagger, Cudgel, Sickle, Staff, etc. (d6 damage)"
    assert (dagger.icon, dagger.looks_like) == ("dagger", "dagger")

    brigandine = items["brigandine"]
    assert (brigandine.size, brigandine.armor, brigandine.description) == (
        "bulky",
        1,
        "1 Armor, bulky",
    )
    assert (brigandine.icon, brigandine.looks_like) == ("armor", "armor")

    rations = items["rations"]
    assert (rations.uses, rations.icon, rations.looks_like, rations.description) == (
        3,
        "food",
        "food",
        "3 uses",
    )


def test_bundled_wwn_items_keep_their_catalogue_fields():
    items = {
        item.id: item
        for item in bundled_library().rule_sets["worldswithoutnumber"].marketplace.items
    }
    plate = items["shop_plate_armor"]
    assert (
        plate.name,
        plate.category,
        plate.price,
        plate.size,
        plate.armor,
        plate.armor_mode,
    ) == ("Plate Armor", "armor", 1000, "large", 7, "max")
    assert plate.encumbrance_checks == ("sneak", "exert")
    assert (plate.damage, plate.uses) == ("", 0)
    assert plate.description == "Heavy armor, AC 17, Encumbrance 2; penalizes Sneak and Exert."

    ration = items["shop_daily_ration"]
    assert (ration.icon, ration.looks_like) == ("tool", "tool")
    assert ration.description == "A day's food and water for one person. Encumbrance 1."

    axe = items["shop_axe_hand"]
    assert (axe.name, axe.damage, axe.icon, axe.looks_like) == (
        "Hand Axe",
        "1d6",
        "axe",
        "axe",
    )
