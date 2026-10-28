"""Buying from the rule set's marketplace. Catalogue items cost their listed price. The GM
prices anything else. Services are paid by the first named character, who covers the whole bill."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import InventoryRules, Marketplace, ShopItem, ShopService
from cygnusforge.services.common import InvalidInput, whole_number
from cygnusforge.services.events import (
    ArmorSet,
    CampaignState,
    Character,
    CurrencyChanged,
    Item,
    RestRecovered,
    ShelterSet,
    load_state,
    name_key,
    record,
    same_item_name,
)
from cygnusforge.services.inventory import carry_item, character_named, used_slots

MAX_PRICE = 100_000


def listed_item(marketplace: Marketplace, item: str) -> ShopItem | None:
    return next((listed for listed in marketplace.items if same_item_name(listed.name, item)), None)


def _held(character: Character, currency: str) -> int:
    return character.features.get(currency, 0)


def _pay(
    db: Session,
    campaign_id: int,
    marketplace: Marketplace,
    payer: Character,
    price: int,
    cause: str,
) -> int:
    """Take the price from the payer's currency. Returns what the payer has left."""
    held = _held(payer, marketplace.currency)
    if held < price:
        raise InvalidInput(
            f"{payer.name} has {held} {marketplace.currency}, not the {price} this costs"
        )
    record(
        db,
        campaign_id,
        CurrencyChanged(name=payer.name, feature=marketplace.currency, amount=-price, cause=cause),
    )
    return held - price


def buy_item(
    db: Session,
    campaign_id: int,
    marketplace: Marketplace,
    inventory: InventoryRules,
    item_sizes: Mapping[str, int],
    name: str,
    item: str,
    price: int | None = None,
    size: str = "",
    armor_max: int = 0,
) -> dict[str, Any]:
    """A character buys an item. A catalogue item costs its listed price and takes its listed
    size, and a price or size that disagrees with the listing is refused. Any other item needs a
    price. Armor adds to the buyer's armor, up to armor_max (0 means no cap). A purchase that
    would not fit in the inventory is refused, and nothing is paid."""
    state = load_state(db, campaign_id)
    buyer = character_named(state, name)
    listed = listed_item(marketplace, item)
    if listed is not None:
        if price is not None and price != listed.price:
            raise InvalidInput(f"{listed.name} costs {listed.price} in the catalogue, not {price}")
        if size and size != listed.size:
            raise InvalidInput(f"{listed.name} is a {listed.size} item, not {size}")
        price, size = listed.price, listed.size
    else:
        if price is None:
            raise InvalidInput(
                f"{item} is not in the catalogue. Price it from similar catalogue items and pass "
                "the price"
            )
        size = size or "normal"
    price = whole_number(price, "price", 0, MAX_PRICE)
    if size not in item_sizes:
        raise InvalidInput(f"there is no item size named {size!r}")
    slots = item_sizes[size]
    free = inventory.capacity - used_slots(buyer)
    if slots > free:
        raise InvalidInput(
            f"{buyer.name} has room for {max(free, 0)} slots, and {item} takes {slots}"
        )
    left = _pay(db, campaign_id, marketplace, buyer, price, cause=f"buying {item}")
    uses = listed.uses if listed is not None else 0
    text = listed.description if listed is not None else ""
    result = carry_item(
        db,
        campaign_id,
        inventory,
        buyer.name,
        Item(
            item if listed is None else listed.name,
            size,
            slots,
            text=text,
            uses=uses,
            max_uses=uses,
        ),
        cause="bought",
    )
    if listed is not None and listed.armor > 0:
        armor = buyer.armor + listed.armor
        if armor_max > 0:
            armor = min(armor_max, armor)
        record(db, campaign_id, ArmorSet(name=buyer.name, armor=armor))
        result["armor"] = armor
    db.commit()
    result.update({"price": price, "currency_left": left, "currency": marketplace.currency})
    return result


def _find_service(marketplace: Marketplace, service: str) -> ShopService:
    key = name_key(service)
    for listed in marketplace.services:
        if key in (name_key(listed.id), name_key(listed.name)):
            return listed
    offered = ", ".join(listed.name for listed in marketplace.services)
    raise InvalidInput(f"there is no service named {service!r}. The marketplace sells: {offered}")


def buy_service(
    db: Session,
    campaign_id: int,
    marketplace: Marketplace,
    names: Sequence[str],
    service: str,
) -> dict[str, Any]:
    """The named characters buy a service, and the first of them pays for all of it. Shelter
    covers the whole party a night, with a room for each `fits` characters. Restoring treats each
    named character who has lost any of the attributes it restores, at the price per patient."""
    listed = _find_service(marketplace, service)
    if listed.effect == "deferred":
        raise InvalidInput(f"{listed.name} is not modelled yet, so the GM must narrate it")
    if not names:
        raise InvalidInput("name the characters the service is for")
    state: CampaignState = load_state(db, campaign_id)
    seen: dict[str, Character] = {}
    for named in names:
        character = character_named(state, named)
        seen.setdefault(character.name, character)
    buyers = list(seen.values())
    payer = buyers[0]
    if listed.effect == "shelter":
        rooms = math.ceil(len(buyers) / listed.fits)
        total = listed.price * rooms
        left = _pay(db, campaign_id, marketplace, payer, total, cause=listed.name)
        for buyer in buyers:
            record(db, campaign_id, ShelterSet(name=buyer.name, sheltered=True))
        served = [buyer.name for buyer in buyers]
    else:
        patients = [
            buyer
            for buyer in buyers
            if any(buyer.harm.get(stat, 0) > 0 for stat in listed.restores)
        ]
        if not patients:
            raise InvalidInput(f"nobody named needs {listed.name}")
        total = listed.price * len(patients)
        left = _pay(db, campaign_id, marketplace, payer, total, cause=listed.name)
        for patient in patients:
            record(
                db,
                campaign_id,
                RestRecovered(name=patient.name, restores=list(listed.restores), cause=listed.name),
            )
        served = [patient.name for patient in patients]
    db.commit()
    return {
        "service": listed.name,
        "served": served,
        "payer": payer.name,
        "price": total,
        "currency_left": left,
        "currency": marketplace.currency,
    }


def change_currency(
    db: Session,
    campaign_id: int,
    marketplace: Marketplace,
    name: str,
    amount: int,
    cause: str,
) -> dict[str, Any]:
    """Give currency to a character (positive) or take it (negative) outside a purchase. Taking
    refuses if the character lacks the currency, so there is no credit."""
    amount = whole_number(amount, "the amount", -MAX_PRICE, MAX_PRICE)
    if amount == 0:
        raise InvalidInput("the amount must not be zero")
    character = character_named(load_state(db, campaign_id), name)
    if amount < 0:
        left = _pay(db, campaign_id, marketplace, character, -amount, cause=cause)
    else:
        record(
            db,
            campaign_id,
            CurrencyChanged(
                name=character.name, feature=marketplace.currency, amount=amount, cause=cause
            ),
        )
        left = _held(character, marketplace.currency) + amount
    db.commit()
    return {
        "name": character.name,
        "amount": amount,
        "currency_left": left,
        "currency": marketplace.currency,
        "cause": cause,
    }
