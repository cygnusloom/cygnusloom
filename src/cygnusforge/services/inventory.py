"""What a character carries, the inventory's capacity, and the penalty item that a rule set names.
Each item takes slots, and a full inventory drains the rule set's full stat, so it is shared by
every rule set that tracks items."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlmodel import Session

from cygnusforge.content.character import CharacterSpec
from cygnusforge.content.library import InventoryRules, ShopItem
from cygnusforge.rules.dice import is_formula
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CampaignState,
    Character,
    CharacterDrained,
    Item,
    ItemAdded,
    ItemRemoved,
    ItemUsesSet,
    current,
    load_state,
    name_key,
    record,
    same_item_name,
)


def capacity_of(
    character: Character, rules: InventoryRules, spec: CharacterSpec | None = None
) -> int:
    """The slots a character's inventory holds. A rule set with no `capacity_stat` gives every
    character its fixed `capacity`. One that names a stat uses the character's raw score in it,
    times `capacity_multiplier`: no stat modifier is applied. `spec` is the rule set's character
    spec; it is optional because the score is read from the character, not the bands."""
    if not rules.capacity_stat:
        return rules.capacity
    if spec is not None and rules.capacity_stat not in spec.stats:
        raise InvalidInput(f"{rules.capacity_stat!r} is no stat of this rule set")
    return max(0, current(character, rules.capacity_stat) * rules.capacity_multiplier)


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
    capacity = capacity_of(character, rules)
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
            damage=item.damage,
            rules=list(item.rules),
            cause=cause,
        ),
    )
    result: dict[str, Any] = {
        "name": character.name,
        "item": item.name,
        "slots_used": after,
        "capacity": capacity,
    }
    if rules.full_stat and before < capacity <= after:
        record(
            db,
            campaign_id,
            CharacterDrained(
                name=character.name, stat=rules.full_stat, cause="the inventory is full"
            ),
        )
        result["drained"] = rules.full_stat
    if after > capacity:
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
    listed: ShopItem | None = None,
    damage: str = "",
) -> dict[str, Any]:
    """Give a character an item. A catalogue item keeps its listed uses, description, and damage,
    as a purchase does. An item not in the catalogue is given the damage the GM names, if it
    is a weapon."""
    if listed is not None and size == "normal":
        size = listed.size
    if size not in item_sizes:
        raise InvalidInput(f"there is no item size named {size!r}")
    slots = item_sizes[size]
    uses = listed.uses if listed is not None else 0
    text = listed.description if listed is not None else ""
    if listed is not None and damage:
        raise InvalidInput(f"{listed.name} is in the catalogue, so its damage comes from there")
    if damage and not is_formula(damage):
        raise InvalidInput(f"{damage!r} is not a dice formula such as 'd6'")
    given = Item(
        item if listed is None else listed.name,
        size,
        slots,
        text=text,
        damage=(listed.damage or "") if listed is not None else damage,
        uses=uses,
        max_uses=uses,
    )
    result = carry_item(db, campaign_id, rules, name, given, cause="")
    db.commit()
    return result


def carry_listed(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    item_sizes: Mapping[str, int],
    name: str,
    listed: ShopItem,
) -> dict[str, Any]:
    """Carry one copy of a catalogue item with its listed size, uses, and description. It does
    not commit, so a wilderness effect can grant items inside its own transaction."""
    if listed.size not in item_sizes:
        raise InvalidInput(f"there is no item size named {listed.size!r}")
    item = Item(
        listed.name,
        listed.size,
        item_sizes[listed.size],
        text=listed.description,
        damage=listed.damage or "",
        uses=listed.uses,
        max_uses=listed.uses,
    )
    return carry_item(db, campaign_id, rules, name, item, cause="")


def fits_listed(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    item_sizes: Mapping[str, int],
    name: str,
    listed: ShopItem,
) -> bool:
    """Whether one copy of a catalogue item fits in the character's pack without going over
    capacity. Nothing is recorded."""
    if listed.size not in item_sizes:
        raise InvalidInput(f"there is no item size named {listed.size!r}")
    character = character_named(load_state(db, campaign_id), name)
    return used_slots(character) + item_sizes[listed.size] <= capacity_of(character, rules)


def add_penalty(
    db: Session, campaign_id: int, rules: InventoryRules, name: str, cause: str
) -> dict[str, Any]:
    penalty = Item(rules.penalty_name, "", rules.penalty_slots, kind=rules.penalty_item)
    result = carry_item(db, campaign_id, rules, name, penalty, cause=cause)
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
    if held.kind == rules.penalty_item:
        raise InvalidInput(f"{character.name}'s {rules.penalty_name} lasts {rules.penalty_lasts}")
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
        "capacity": capacity_of(character, rules),
    }


def use_item(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    name: str,
    item: str,
    charged_kind: str = "",
) -> dict[str, Any]:
    """Spend one use of a consumable the character carries. Its last use uses it up, which removes
    it. Charged items are spent with use_charge, so they are refused here."""
    state = load_state(db, campaign_id)
    character = character_named(state, name)
    held = next((i for i in character.items if same_item_name(i.name, item)), None)
    if held is None:
        raise InvalidInput(f"{character.name} carries no {item!r}")
    if held.max_uses < 1:
        raise InvalidInput(f"{held.name} is not a consumable and has no uses to spend")
    if charged_kind and held.kind == charged_kind:
        raise InvalidInput(f"{held.name} is a charged item; spend it with use_charge")
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
        "capacity": capacity_of(character, rules),
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
