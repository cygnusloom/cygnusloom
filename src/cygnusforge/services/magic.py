"""Spellbooks, scrolls, and charged items. A spellbook holds one spell and a cast costs the rule
set's penalty item; a scroll is spent by one cast; a charged item has uses that a recharge restores,
as a wand has charges. The rule set names the item kinds and sizes, and its `combat` supplies the
save every magic roll uses."""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import CharacterSpec, CombatRules, InventoryRules, MagicRules
from cygnusforge.rules.dice import roll_total
from cygnusforge.services import inventory as inventory_service
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CampaignState,
    CharacterSaved,
    Item,
    ItemRemoved,
    ItemUsesSet,
    SpellCast,
    current,
    load_state,
    name_key,
    record,
)


def _slots(item_sizes: Mapping[str, int], size: str) -> int:
    if size not in item_sizes:
        raise InvalidInput(f"there is no item size named {size!r}")
    return item_sizes[size]


def _held(state: CampaignState, name: str, item: str, kinds: tuple[str, ...]) -> tuple[str, Item]:
    """The character's name and the first item named `item` of one of `kinds`, in that order."""
    character = inventory_service.character_named(state, name)
    for kind in kinds:
        for held in character.items:
            if held.kind == kind and name_key(held.name) == name_key(item):
                return character.name, held
    raise InvalidInput(f"{character.name} carries no {item!r}")


def give_spellbook(
    db: Session,
    campaign_id: int,
    magic: MagicRules,
    inventory: InventoryRules,
    item_sizes: Mapping[str, int],
    name: str,
    spell: str,
    description: str,
) -> dict[str, Any]:
    item = Item(
        spell,
        magic.spellbook_size,
        _slots(item_sizes, magic.spellbook_size),
        kind=magic.spellbook_kind,
        text=description,
    )
    result = inventory_service.carry_item(
        db, campaign_id, inventory, name, item, cause=f"a {magic.spellbook_kind}"
    )
    db.commit()
    return result


def give_scroll(
    db: Session,
    campaign_id: int,
    magic: MagicRules,
    inventory: InventoryRules,
    item_sizes: Mapping[str, int],
    name: str,
    spell: str,
    description: str,
) -> dict[str, Any]:
    item = Item(
        spell,
        magic.scroll_size,
        _slots(item_sizes, magic.scroll_size),
        kind=magic.scroll_kind,
        text=description,
    )
    result = inventory_service.carry_item(
        db, campaign_id, inventory, name, item, cause=f"a {magic.scroll_kind}"
    )
    db.commit()
    return result


def give_charged_item(
    db: Session,
    campaign_id: int,
    magic: MagicRules,
    inventory: InventoryRules,
    item_sizes: Mapping[str, int],
    name: str,
    item: str,
    uses: int,
    recharge: str,
    size: str,
) -> dict[str, Any]:
    if uses < 1:
        raise InvalidInput(f"a charged item needs at least one use, not {uses}")
    charged = Item(
        item,
        size,
        _slots(item_sizes, size),
        kind=magic.charged_kind,
        text=recharge,
        uses=uses,
        max_uses=uses,
    )
    result = inventory_service.carry_item(
        db, campaign_id, inventory, name, charged, cause=f"a {magic.charged_kind}"
    )
    db.commit()
    return result


def cast_spell(
    db: Session,
    campaign_id: int,
    magic: MagicRules,
    inventory: InventoryRules,
    name: str,
    spell: str,
) -> dict[str, Any]:
    """Cast from a spellbook when one is held, or else from a scroll, which is then spent. Only a
    spellbook cast pays the penalty item."""
    state = load_state(db, campaign_id)
    character_name, held = _held(state, name, spell, (magic.spellbook_kind, magic.scroll_kind))
    if held.kind == magic.scroll_kind:
        record(
            db,
            campaign_id,
            SpellCast(name=character_name, spell=held.name, source="scroll"),
        )
        record(
            db,
            campaign_id,
            ItemRemoved(name=character_name, item=held.name, kind_of_item=magic.scroll_kind),
        )
        db.commit()
        return {"name": character_name, "spell": held.name, "source": "scroll", "penalty": False}

    record(db, campaign_id, SpellCast(name=character_name, spell=held.name, source="spellbook"))
    penalty = Item(
        inventory.penalty_name,
        "",
        inventory.penalty_slots,
        kind=inventory.penalty_item,
    )
    result: dict[str, Any] = {}
    for _ in range(magic.cast_penalty):
        result = inventory_service.carry_item(
            db, campaign_id, inventory, character_name, penalty, cause=f"casting {held.name}"
        )
    db.commit()
    return {
        **result,
        "name": character_name,
        "spell": held.name,
        "source": "spellbook",
        "penalty": magic.cast_penalty > 0,
    }


def use_charge(
    db: Session, campaign_id: int, magic: MagicRules, name: str, item: str
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    character_name, held = _held(state, name, item, (magic.charged_kind,))
    if held.uses < 1:
        raise InvalidInput(
            f"{character_name}'s {held.name} has no uses left and must recharge first"
        )
    record(
        db,
        campaign_id,
        ItemUsesSet(name=character_name, item=held.name, uses=held.uses - 1, cause="used"),
    )
    db.commit()
    return {"name": character_name, "item": held.name, "uses_left": held.uses - 1}


def recharge_charged_item(
    db: Session, campaign_id: int, magic: MagicRules, name: str, item: str
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    character_name, held = _held(state, name, item, (magic.charged_kind,))
    if held.uses == held.max_uses:
        raise InvalidInput(f"{held.name} is already fully charged")
    if held.spent_on == state.clock:
        raise InvalidInput(f"{held.name} cannot recharge until time passes")
    record(
        db,
        campaign_id,
        ItemUsesSet(name=character_name, item=held.name, uses=held.max_uses, cause="recharged"),
    )
    db.commit()
    return {"name": character_name, "item": held.name, "uses": held.max_uses}


def roll_save(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    spec: CharacterSpec,
    rng: random.Random,
    name: str,
    stat: str,
    reason: str,
) -> dict[str, Any]:
    """An engine-rolled save against a stat, recorded with its reason. A failure only records the
    result; the Warden applies what it costs."""
    if stat not in spec.stats:
        raise InvalidInput(f"there is no stat named {stat!r}")
    state = load_state(db, campaign_id)
    character = inventory_service.character_named(state, name)
    roll = roll_total(combat.check_die, rng)
    score = current(character, stat)
    passed = combat.module.save_succeeds(roll, score)
    record(
        db,
        campaign_id,
        CharacterSaved(
            name=character.name,
            stat=stat,
            roll=roll,
            score=score,
            passed=passed,
            cause=reason,
        ),
    )
    db.commit()
    return {
        "name": character.name,
        "stat": stat,
        "roll": roll,
        "score": score,
        "passed": passed,
    }
