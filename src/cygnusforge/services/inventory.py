"""What a character carries, the inventory's capacity, and Fatigue. Each item takes slots, and a
full inventory drains the rule set's full stat, so it is shared by every rule set that tracks
items."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import InventoryRules
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CampaignState,
    Character,
    CharacterDrained,
    Item,
    ItemAdded,
    ItemRemoved,
    ItemUsesSet,
    load_state,
    name_key,
    record,
)

FATIGUE_NAME = "Fatigue"


def used_slots(character: Character) -> int:
    return sum(item.slots for item in character.items)


def character_named(state: CampaignState, name: str) -> Character:
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    return character


def carry_item(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    name: str,
    item: Item,
    cause: str,
) -> dict[str, Any]:
    """Add an item and drain the full stat if this fills the inventory. The item is added even
    when that takes the inventory over capacity, and the result says so."""
    character = character_named(load_state(db, campaign_id), name)
    before = used_slots(character)
    after = before + item.slots
    record(
        db,
        campaign_id,
        ItemAdded(
            name=character.name,
            item=item.name,
            size=item.size,
            slots=item.slots,
            kind_of_item=item.kind,
            text=item.text,
            uses=item.uses,
            max_uses=item.max_uses,
            cause=cause,
        ),
    )
    result: dict[str, Any] = {
        "name": character.name,
        "item": item.name,
        "slots_used": after,
        "capacity": rules.capacity,
    }
    if before < rules.capacity <= after:
        record(
            db,
            campaign_id,
            CharacterDrained(
                name=character.name, stat=rules.full_stat, cause="the inventory is full"
            ),
        )
        result["drained"] = rules.full_stat
    if after > rules.capacity:
        result["over_capacity"] = True
    return result


def give_item(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    item_sizes: Mapping[str, int],
    name: str,
    item: str,
    size: str,
) -> dict[str, Any]:
    if size not in item_sizes:
        raise InvalidInput(f"there is no item size named {size!r}")
    slots = item_sizes[size]
    result = carry_item(db, campaign_id, rules, name, Item(item, size, slots), cause="")
    db.commit()
    return result


def add_fatigue(
    db: Session, campaign_id: int, rules: InventoryRules, name: str, cause: str
) -> dict[str, Any]:
    fatigue = Item(FATIGUE_NAME, "", rules.fatigue_slots, kind=rules.fatigue_item)
    result = carry_item(db, campaign_id, rules, name, fatigue, cause=cause)
    db.commit()
    return result


def drop_item(
    db: Session, campaign_id: int, rules: InventoryRules, name: str, item: str
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    character = character_named(state, name)
    held = next((i for i in character.items if name_key(i.name) == name_key(item)), None)
    if held is None:
        raise InvalidInput(f"{character.name} carries no {item!r}")
    if held.kind == rules.fatigue_item:
        raise InvalidInput(f"{character.name}'s Fatigue lasts until a full night's rest")
    record(
        db,
        campaign_id,
        ItemRemoved(name=character.name, item=held.name, kind_of_item=held.kind),
    )
    db.commit()
    return {
        "name": character.name,
        "item": held.name,
        "slots_used": used_slots(character) - held.slots,
        "capacity": rules.capacity,
    }


def use_item(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    name: str,
    item: str,
    relic_kind: str = "",
) -> dict[str, Any]:
    """Spend one use of a consumable the character carries. Its last use uses it up, which removes
    it. Relics are spent with use_relic, so they are refused here."""
    state = load_state(db, campaign_id)
    character = character_named(state, name)
    held = next((i for i in character.items if name_key(i.name) == name_key(item)), None)
    if held is None:
        raise InvalidInput(f"{character.name} carries no {item!r}")
    if held.max_uses < 1:
        raise InvalidInput(f"{held.name} is not a consumable and has no uses to spend")
    if relic_kind and held.kind == relic_kind:
        raise InvalidInput(f"{held.name} is a relic; spend it with use_relic")
    if held.uses < 1:
        raise InvalidInput(f"{character.name}'s {held.name} has no uses left")
    left = held.uses - 1
    if left > 0:
        record(
            db,
            campaign_id,
            ItemUsesSet(name=character.name, item=held.name, uses=left, cause="used"),
        )
    else:
        record(
            db,
            campaign_id,
            ItemRemoved(name=character.name, item=held.name, kind_of_item=held.kind),
        )
    db.commit()
    return {
        "name": character.name,
        "item": held.name,
        "uses_left": left,
        "used_up": left == 0,
        "slots_used": used_slots(character) - (held.slots if left == 0 else 0),
        "capacity": rules.capacity,
    }


def clear_items(db: Session, campaign_id: int, names: Iterable[str], kinds: Iterable[str]) -> None:
    """Remove every item of the given kinds from each named character. It does not commit."""
    wanted = set(kinds)
    state = load_state(db, campaign_id)
    for name in names:
        character = state.characters[name_key(name)]
        for held in [i for i in character.items if i.kind in wanted]:
            record(
                db,
                campaign_id,
                ItemRemoved(name=character.name, item=held.name, kind_of_item=held.kind),
            )
