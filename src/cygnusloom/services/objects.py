"""World objects: currency, an item, or anything the GM describes, held by a place. The GM
stocks them; a character takes them. Nothing here creates treasure on its own."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.library import InventoryRules, Marketplace
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.events import (
    CampaignState,
    CurrencyChanged,
    Item,
    ObjectPlaced,
    ObjectTaken,
    Place,
    WorldObject,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.inventory import capacity_of, carry_item, character_named, used_slots
from cygnusloom.services.market import MAX_PRICE


def visible_objects(state: CampaignState, place: str | None) -> list[WorldObject]:
    """The objects a character in this place can see, oldest first. Hidden ones are left out."""
    if place is None:
        return []
    key = name_key(place)
    found = [o for o in state.objects.values() if not o.hidden and name_key(o.place) == key]
    return sorted(found, key=lambda o: o.id)


def place_object(
    db: Session,
    campaign_id: int,
    marketplace: Marketplace | None,
    item_sizes: Mapping[str, int],
    place: str,
    contents: str,
    hidden: bool,
    cause: str,
    currency: int | None = None,
    item: str | None = None,
    item_size: str = "",
    name: str = "",
) -> dict[str, Any]:
    """Put an object in a place: a short name to take it by, a description, and optionally currency
    or an item. Currency needs the rule set's currency. An item takes its size's slots, and the size
    defaults to normal."""
    state = load_state(db, campaign_id)
    found = state.named(place)
    if not isinstance(found, Place):
        raise InvalidInput(f"there is no place named {place!r}")
    contents = contents.strip()
    if not contents:
        raise InvalidInput("the object needs a description")
    name = name.strip() or contents
    currency = 0 if currency is None else whole_number(currency, "the currency", 0, MAX_PRICE)
    if currency and marketplace is None:
        raise InvalidInput("this rule set has no currency to hold money")
    item = (item or "").strip()
    size, slots = "", 0
    if item:
        size = item_size or "normal"
        if size not in item_sizes:
            raise InvalidInput(f"there is no item size named {size!r}")
        slots = item_sizes[size]
    event_id = record(
        db,
        campaign_id,
        ObjectPlaced(
            place=found.name,
            contents=contents,
            currency=currency,
            item=item,
            item_size=size,
            item_slots=slots,
            hidden=hidden,
            cause=cause,
            name=name,
        ),
    )
    db.commit()
    return {
        "id": event_id,
        "place": found.name,
        "name": name,
        "contents": contents,
        "currency": currency,
        "item": item,
        "hidden": hidden,
    }


def take_object(
    db: Session,
    campaign_id: int,
    marketplace: Marketplace | None,
    inventory: InventoryRules | None,
    item_sizes: Mapping[str, int],
    object_name: str,
    character: str,
    cause: str,
) -> dict[str, Any]:
    """A character takes a visible object in the place they are in. Currency goes to that character.
    An item goes into their inventory, and the take is refused if it does not fit. An object
    placed without an item is carried as an item named after the object, so a described find is
    not lost. If several identical objects are here, the one placed first is taken."""
    state = load_state(db, campaign_id)
    taker = character_named(state, character)
    if taker.dead:
        raise InvalidInput(f"{taker.name} is dead")
    visible = visible_objects(state, taker.location)
    obj = _find_object(visible, object_name)
    if obj is None:
        if not visible:
            raise InvalidInput(f"there is nothing here for {taker.name} to take")
        names = ", ".join(_label(o) for o in visible)
        raise InvalidInput(
            f"there is no {object_name.strip()!r} here. Here {taker.name} can take: {names}"
        )
    if obj.currency and marketplace is None:
        raise InvalidInput("this rule set has no currency to hold money")
    item = obj.item
    size = obj.item_size
    slots = obj.item_slots
    if not item and not obj.currency:
        item = obj.name or obj.contents
        size = size or "normal"
        if size not in item_sizes:
            raise InvalidInput(f"there is no item size named {size!r}")
        slots = item_sizes[size]
    if item:
        if inventory is None:
            raise InvalidInput("this rule set has no inventory to carry an item")
        free = capacity_of(taker, inventory) - used_slots(taker)
        if slots > free:
            raise InvalidInput(
                f"{taker.name} has room for {max(free, 0)} slots, and {item} takes {slots}"
            )
    if obj.currency and marketplace is not None:
        record(
            db,
            campaign_id,
            CurrencyChanged(
                name=taker.name, feature=marketplace.currency, amount=obj.currency, cause=cause
            ),
        )
    if item and inventory is not None:
        carry_item(
            db,
            campaign_id,
            inventory,
            taker.name,
            Item(
                name=item,
                size=size,
                slots=slots,
                text=obj.contents,
            ),
            cause=cause,
        )
    record(
        db,
        campaign_id,
        ObjectTaken(object_id=obj.id, name=taker.name, contents=obj.contents, cause=cause),
    )
    db.commit()
    return {
        "object": _label(obj),
        "character": taker.name,
        "currency": obj.currency,
        "item": item,
    }


def _label(obj: WorldObject) -> str:
    return obj.name or obj.contents


def _find_object(visible: list[WorldObject], wanted: str) -> WorldObject | None:
    """Exact name or description first, then a partial match, so the GM can say the short
    name or a fuller one. Ties go to the object placed first."""
    key = name_key(wanted)
    if not key:
        return None
    for exact in (True, False):
        for obj in visible:
            names = [name_key(obj.name), name_key(obj.contents)]
            if exact and key in names:
                return obj
            if not exact and len(key) >= 3 and any(n and key in n for n in names):
                return obj
    return None
