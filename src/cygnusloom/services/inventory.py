"""What a character carries, the inventory's capacity, and the penalty item that a rule set names.
Each item takes slots, and a full inventory drains the rule set's full stat, so it is shared by
every rule set that tracks items."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import InventoryPool, InventoryRules, ShopItem
from cygnusloom.db.models import Campaign
from cygnusloom.rules.dice import is_formula
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    CampaignState,
    Character,
    CharacterDrained,
    Item,
    ItemAdded,
    ItemReadied,
    ItemRemoved,
    ItemUsesSet,
    current,
    load_state,
    name_key,
    record,
    same_item_name,
)


def tracks_inventory(db: Session, campaign_id: int) -> bool:
    """Whether this campaign enforces the rule set's inventory space. A campaign whose row is
    missing keeps the rule set's limits, so a caller outside a stored campaign is unaffected."""
    campaign = db.get(Campaign, campaign_id)
    return campaign is None or campaign.use_inventory_space


def capacity_of(
    character: Character, rules: InventoryRules, spec: CharacterSpec | None = None
) -> int:
    """The slots a character's inventory holds. A rule set with no `capacity_stat` gives every
    character its fixed `capacity`. One that names a stat uses the character's raw score in it,
    times `capacity_multiplier`: no stat modifier is applied. `spec` is the rule set's character
    spec; it is optional because the score is read from the character, not the bands. A rule set
    with named pools adds their capacities."""
    if rules.pools:
        return sum(pool_capacity(character, rules, pool.id) for pool in rules.pools)
    if not rules.capacity_stat:
        return rules.capacity
    if spec is not None and rules.capacity_stat not in spec.stats:
        raise InvalidInput(f"{rules.capacity_stat!r} is no stat of this rule set")
    return max(0, int(current(character, rules.capacity_stat) * rules.capacity_multiplier))


def readied_pool(rules: InventoryRules) -> InventoryPool | None:
    """The named Readied pool, or None when the rule set has none."""
    return next((pool for pool in rules.pools if pool.ready), None)


def stowed_pool(rules: InventoryRules) -> InventoryPool | None:
    """The named Stowed pool, or None when the rule set has none."""
    return next((pool for pool in rules.pools if not pool.ready), None)


def pool_named(rules: InventoryRules, pool_id: str) -> InventoryPool | None:
    return next((pool for pool in rules.pools if pool.id == pool_id), None)


def pool_for_size(rules: InventoryRules, size: str) -> InventoryPool | None:
    """The named pool an item of `size` joins."""
    if not rules.pools:
        return None
    wanted = rules.size_pools.get(size, rules.default_pool)
    return pool_named(rules, wanted) if wanted else rules.pools[0]


def item_pool_id(character: Character, rules: InventoryRules, item: Item) -> str:
    """The named pool an item occupies. A readied item is in the Readied pool; a stowed item
    is in the pool its size names, or the Stowed pool."""
    if not rules.pools:
        return ""
    if item.ready:
        readied = readied_pool(rules)
        return readied.id if readied is not None else rules.pools[0].id
    by_size = pool_for_size(rules, item.size)
    if by_size is not None and not by_size.ready:
        return by_size.id
    stowed = stowed_pool(rules)
    return stowed.id if stowed is not None else rules.pools[0].id


def pool_usage(character: Character, rules: InventoryRules, pool_id: str) -> int:
    return sum(
        item.slots for item in character.items if item_pool_id(character, rules, item) == pool_id
    )


def pool_capacity(character: Character, rules: InventoryRules, pool_id: str) -> int:
    pool = pool_named(rules, pool_id)
    if pool is None:
        raise InvalidInput(f"there is no inventory pool named {pool_id!r}")
    score = current(character, pool.stat) if pool.stat else 0
    return pool.capacity_for(score)


def used_slots(character: Character) -> int:
    return sum(item.slots for item in character.items)


def readied_slots(character: Character, rules: InventoryRules | None = None) -> int:
    if rules is not None and rules.pools:
        readied = readied_pool(rules)
        return pool_usage(character, rules, readied.id) if readied is not None else 0
    return sum(item.slots for item in character.items if item.ready)


def readied_capacity(character: Character, rules: InventoryRules) -> int:
    """How many slots the character may keep readied. Zero when the rule set has no readied pool."""
    readied = readied_pool(rules)
    if readied is not None:
        return pool_capacity(character, rules, readied.id)
    if not rules.readied_stat:
        return 0
    return max(0, current(character, rules.readied_stat) // rules.readied_divisor)


def penalty_item(rules: InventoryRules) -> Item:
    """The item a rule set uses to mark its penalty, such as Overloaded or Fatigue."""
    return Item(rules.penalty_name, "", rules.penalty_slots, kind=rules.penalty_item)


def carries_penalty(character: Character, rules: InventoryRules) -> bool:
    """Whether the character already carries the rule set's penalty item."""
    return any(item.kind == rules.penalty_item for item in character.items)


def _add_penalty_event(
    db: Session, campaign_id: int, character: Character, rules: InventoryRules, cause: str
) -> None:
    """Record the penalty item directly, without routing through `carry_item`, so adding it
    cannot recurse into the capacity check that asked for it."""
    penalty = penalty_item(rules)
    record(
        db,
        campaign_id,
        ItemAdded(
            name=character.name,
            item=penalty.name,
            size=penalty.size,
            slots=penalty.slots,
            kind_of_item=penalty.kind,
            cause=cause,
            ready=False,
        ),
    )


def _remove_penalty_event(
    db: Session, campaign_id: int, character: Character, rules: InventoryRules
) -> None:
    record(
        db,
        campaign_id,
        ItemRemoved(name=character.name, item=rules.penalty_name, kind_of_item=rules.penalty_item),
    )


def set_ready(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    name: str,
    item: str,
    ready: bool,
) -> dict[str, Any]:
    """Ready or stow one carried item. Readying is refused when the Readied pool has no room;
    a size that belongs in the Stowed pool cannot be readied. A campaign that does not track
    inventory space skips the capacity checks but keeps the physical size rule."""
    character = character_named(load_state(db, campaign_id), name)
    tracked = tracks_inventory(db, campaign_id)
    held = next((i for i in character.items if same_item_name(i.name, item)), None)
    if held is None:
        raise InvalidInput(f"{character.name} carries no {item!r}")
    if rules.pools:
        target = readied_pool(rules) if ready else stowed_pool(rules)
        if target is None:
            raise InvalidInput(f"this rule set has no {'Readied' if ready else 'Stowed'} pool")
        mapped = pool_for_size(rules, held.size)
        if ready and held.size in rules.size_pools and mapped is not None and not mapped.ready:
            raise InvalidInput(f"{held.name} is too bulky to keep readied")
        current = item_pool_id(character, rules, held)
        if (
            tracked
            and current != target.id
            and pool_usage(character, rules, target.id) + held.slots
            > pool_capacity(character, rules, target.id)
        ):
            raise InvalidInput(
                f"{character.name}'s {target.name} pool has no room for {held.name}; "
                f"it holds {pool_capacity(character, rules, target.id)} slots"
            )
    elif tracked and ready and not held.ready:
        capacity = readied_capacity(character, rules)
        if rules.readied_stat and readied_slots(character) + held.slots > capacity:
            raise InvalidInput(
                f"{character.name} can keep {capacity} slots readied, and {held.name} "
                f"takes {held.slots}; stow another item first"
            )
    record(db, campaign_id, ItemReadied(name=character.name, item=held.name, ready=ready))
    changed = character_named(load_state(db, campaign_id), name)
    result: dict[str, Any] = {
        "character": changed.name,
        "item": held.name,
        "ready": ready,
        "readied_slots": readied_slots(changed, rules),
        "readied_capacity": readied_capacity(changed, rules),
    }
    if rules.pools:
        pool_id = item_pool_id(
            changed, rules, next(i for i in changed.items if same_item_name(i.name, held.name))
        )
        result["pool"] = pool_id
        result["pool_slots"] = pool_usage(changed, rules, pool_id)
        result["pool_capacity"] = pool_capacity(changed, rules, pool_id)
    return result


def character_named(state: CampaignState, name: str) -> Character:
    entry = state.named(name)
    if not isinstance(entry, Character):
        raise InvalidInput(f"there is no character named {name!r}")
    return entry


def carry_item(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    name: str,
    item: Item,
    cause: str,
    *,
    overflow: bool = False,
) -> dict[str, Any]:
    """Add an item and drain the full stat if this fills the inventory. A rule set with named
    pools refuses an item its pool cannot hold, unless `overflow` allows it, which the starting kit
    and a wilderness bounty use so gear the book hands out is never lost to a full pack. A single-pool rule
    set adds it over capacity and says so. A campaign that does not track inventory space records
    the item without any capacity refusal, penalty item, or full-pack drain."""
    character = character_named(load_state(db, campaign_id), name)
    tracked = tracks_inventory(db, campaign_id)
    capacity = capacity_of(character, rules)
    before = used_slots(character)
    after = before + item.slots
    ready = item.ready
    pool: InventoryPool | None = None
    over_pool = False
    if rules.pools:
        equipment = bool(item.damage or {"shield", "armor"} & set(item.flags))
        pool = readied_pool(rules) if equipment else None
        if pool is None:
            pool = pool_for_size(rules, item.size)
        if pool is None:
            raise InvalidInput(f"there is no inventory pool for {item.size!r}")
        used = pool_usage(character, rules, pool.id)
        # An item that takes no slots always fits, even in a pool that is already over capacity.
        if (
            tracked
            and item.slots > 0
            and used + item.slots > pool_capacity(character, rules, pool.id)
        ):
            if not overflow:
                raise InvalidInput(
                    f"{character.name}'s {pool.name} pool is full: {used} of "
                    f"{pool_capacity(character, rules, pool.id)} slots used, and {item.name} "
                    f"takes {item.slots}"
                )
            over_pool = True
            ready = False
        ready = pool.ready if not over_pool else False
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
            flags=list(item.flags),
            ready=ready,
            cause=cause,
        ),
    )
    # A named-pool character over total capacity gains the rule set's penalty item. The check is
    # made once here so the starting kit's `overflow` items trigger it too. Adding the penalty
    # itself never triggers another.
    penalized = False
    if (
        tracked
        and rules.pools
        and item.kind != rules.penalty_item
        and after > capacity
        and not carries_penalty(character, rules)
    ):
        _add_penalty_event(db, campaign_id, character, rules, "carrying too much")
        penalized = True
    result: dict[str, Any] = {
        "name": character.name,
        "item": item.name,
        "slots_used": after + (rules.penalty_slots if penalized else 0),
        "capacity": capacity,
    }
    if rules.pools and pool is not None:
        result.update(
            pool=pool.id,
            pool_slots=pool_usage(character, rules, pool.id) + item.slots,
            pool_capacity=pool_capacity(character, rules, pool.id),
        )
        if over_pool:
            result["over_capacity"] = True
    if tracked and not rules.pools and rules.full_stat and before < capacity <= after:
        record(
            db,
            campaign_id,
            CharacterDrained(
                name=character.name, stat=rules.full_stat, cause="the inventory is full"
            ),
        )
        result["drained"] = rules.full_stat
    if tracked and not rules.pools and after > capacity:
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
    flags: tuple[str, ...] = ()
    if listed is not None:
        if listed.armor_mode == "shield":
            flags = ("shield",)
        elif listed.armor > 0:
            flags = ("armor",)
    given = Item(
        item if listed is None else listed.name,
        size,
        slots,
        text=text,
        damage=(listed.damage or "") if listed is not None else damage,
        uses=uses,
        max_uses=uses,
        flags=flags,
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
    cause: str = "",
    *,
    overflow: bool = False,
) -> dict[str, Any]:
    """Carry one copy of a catalogue item with its listed size, uses, and description. It does
    not commit, so a wilderness effect or a starting kit can grant items inside its own
    transaction."""
    if listed.size not in item_sizes:
        raise InvalidInput(f"there is no item size named {listed.size!r}")
    flags: tuple[str, ...] = ()
    if listed.armor_mode == "shield":
        flags = ("shield",)
    elif listed.armor > 0:
        flags = ("armor",)
    item = Item(
        listed.name,
        listed.size,
        item_sizes[listed.size],
        text=listed.description,
        damage=listed.damage or "",
        uses=listed.uses,
        max_uses=listed.uses,
        flags=flags,
    )
    return carry_item(db, campaign_id, rules, name, item, cause=cause, overflow=overflow)


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
    if not tracks_inventory(db, campaign_id):
        return True
    character = character_named(load_state(db, campaign_id), name)
    if rules.pools:
        pool = pool_for_size(rules, listed.size)
        if pool is None:
            return False
        return pool_usage(character, rules, pool.id) + item_sizes[listed.size] <= pool_capacity(
            character, rules, pool.id
        )
    return used_slots(character) + item_sizes[listed.size] <= capacity_of(character, rules)


def add_penalty(
    db: Session, campaign_id: int, rules: InventoryRules, name: str, cause: str
) -> dict[str, Any]:
    """Add the rule set's penalty item, once. Calling it again for a character who already
    carries the penalty changes nothing, so a penalty applied twice does not stack."""
    character = character_named(load_state(db, campaign_id), name)
    if carries_penalty(character, rules):
        result: dict[str, Any] = {
            "name": character.name,
            "item": rules.penalty_name,
            "slots_used": used_slots(character),
            "capacity": capacity_of(character, rules),
            "already": True,
        }
        if rules.pools:
            penalty = penalty_item(rules)
            pool_id = item_pool_id(character, rules, penalty)
            result.update(
                pool=pool_id,
                pool_slots=pool_usage(character, rules, pool_id),
                pool_capacity=pool_capacity(character, rules, pool_id),
            )
        return result
    penalty = penalty_item(rules)
    result = carry_item(db, campaign_id, rules, name, penalty, cause=cause)
    db.commit()
    return result


def drop_item(
    db: Session, campaign_id: int, rules: InventoryRules, name: str, item: str
) -> dict[str, Any]:
    """Drop one carried item. Dropping gear that brings a named-pool inventory back within its
    total capacity also clears the penalty item. The penalty itself cannot be dropped."""
    state = load_state(db, campaign_id)
    character = character_named(state, name)
    held = next((i for i in character.items if name_key(i.name) == name_key(item)), None)
    if held is None:
        raise InvalidInput(f"{character.name} carries no {item!r}")
    if held.kind == rules.penalty_item:
        raise InvalidInput(f"{character.name}'s {rules.penalty_name} lasts {rules.penalty_lasts}")
    capacity = capacity_of(character, rules)
    remaining = used_slots(character) - held.slots
    record(
        db,
        campaign_id,
        ItemRemoved(name=character.name, item=held.name, kind_of_item=held.kind),
    )
    if rules.pools and remaining <= capacity and carries_penalty(character, rules):
        _remove_penalty_event(db, campaign_id, character, rules)
        remaining -= rules.penalty_slots
    return {
        "name": character.name,
        "item": held.name,
        "slots_used": remaining,
        "capacity": capacity,
    }


def transfer_item(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    giver_name: str,
    receiver_name: str,
    item: str,
) -> dict[str, Any]:
    """Move one carried item from one character to another, keeping its uses and description.
    The receiver's pack must have room: a full pool refuses the move rather than making a copy.
    Money is not an item; move it with transfer_currency."""
    state = load_state(db, campaign_id)
    giver = character_named(state, giver_name)
    receiver = character_named(state, receiver_name)
    if name_key(giver.name) == name_key(receiver.name):
        raise InvalidInput("the giver and the receiver must be different characters")
    held = next((i for i in giver.items if same_item_name(i.name, item)), None)
    if held is None:
        raise InvalidInput(f"{giver.name} carries no {item!r}")
    if held.kind == rules.penalty_item:
        raise InvalidInput(f"{giver.name}'s {rules.penalty_name} lasts {rules.penalty_lasts}")
    record(db, campaign_id, ItemRemoved(name=giver.name, item=held.name, kind_of_item=held.kind))
    giver_slots = used_slots(giver) - held.slots
    if rules.pools and giver_slots <= capacity_of(giver, rules) and carries_penalty(giver, rules):
        _remove_penalty_event(db, campaign_id, giver, rules)
        giver_slots -= rules.penalty_slots
    result = carry_item(db, campaign_id, rules, receiver.name, held, cause=f"from {giver.name}")
    result["giver"] = giver.name
    result["receiver"] = receiver.name
    result["giver_slots_used"] = giver_slots
    return result


def use_item(
    db: Session,
    campaign_id: int,
    rules: InventoryRules,
    name: str,
    item: str,
    charged_kind: str = "",
    *,
    ignore_ready: bool = False,
) -> dict[str, Any]:
    """Spend one use of a consumable the character carries. Its last use uses it up, which removes
    it. Charged items are spent with use_charge, so they are refused here. An automatic
    consumption, such as a night's rations, passes `ignore_ready`: it does not need the item in
    hand."""
    state = load_state(db, campaign_id)
    character = character_named(state, name)
    held = next((i for i in character.items if same_item_name(i.name, item)), None)
    if held is None:
        raise InvalidInput(f"{character.name} carries no {item!r}")
    if rules.pools and not held.ready and not ignore_ready:
        raise InvalidInput(f"{held.name} is stowed; ready it before use")
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
