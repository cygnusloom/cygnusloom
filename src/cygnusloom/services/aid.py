"""Aid for a dying player character. Stabilizing it stops its clock: an aid item used on it, or the
GM's decision that aid has held. A dying condition that ends by aid ends with it."""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import CombatRules, ConditionRules, InventoryRules
from cygnusloom.services import inventory as inventory_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.conditions import end_condition
from cygnusloom.services.events import (
    PC_KIND,
    CharacterStabilized,
    load_state,
    name_key,
    record,
    same_item_name,
)


def stabilize(
    db: Session,
    campaign_id: int,
    name: str,
    by: str,
    combat: CombatRules,
    conditions: Mapping[str, ConditionRules],
    rng: random.Random,
    *,
    plus: str,
    spec: CharacterSpec | None = None,
) -> dict[str, Any]:
    """Stop a dying player character's clock. It stays out of action until the GM recovers it.
    A lasting condition held now ends by aid, which runs its on-end effects."""
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if character.kind != PC_KIND:
        raise InvalidInput(f"{character.name} is not a player character")
    if character.dead:
        raise InvalidInput(f"{character.name} is dead and cannot be stabilized")
    if character.dying_hours is None and character.dying_rounds is None:
        raise InvalidInput(f"{character.name} is not dying")
    record(db, campaign_id, CharacterStabilized(name=character.name, by=by))
    db.commit()
    ended = []
    for cid in combat.dying_conditions:
        rules = conditions.get(cid)
        if cid in character.conditions and rules is not None and "aid" in rules.ends:
            end_condition(
                db,
                campaign_id,
                character.name,
                cid,
                "aid",
                combat,
                conditions,
                rng,
                plus=plus,
                spec=spec,
            )
            ended.append(cid)
    return {"character": character.name, "stabilized": True, "ended": ended}


def use_item(
    db: Session,
    campaign_id: int,
    inventory: InventoryRules,
    combat: CombatRules | None,
    conditions: Mapping[str, ConditionRules],
    rng: random.Random,
    name: str,
    item: str,
    target: str | None,
    charged_kind: str = "",
    *,
    plus: str,
    spec: CharacterSpec | None = None,
    ignore_ready: bool = False,
) -> dict[str, Any]:
    """Spend one use of a consumable the character carries. An aid item the rule set lists may be
    used on a dying character, and using it stabilizes them. Any other item is spent on the holder.
    `ignore_ready` spends a stowed item, for an automatic use such as a day's rations."""
    aid = combat is not None and any(same_item_name(item, a) for a in combat.aid_items)
    if target is None or name_key(target) == name_key(name):
        target = name
    elif not aid:
        raise InvalidInput(f"{item!r} can only be used on the character carrying it")
    if aid:
        patient = load_state(db, campaign_id).characters.get(name_key(target))
        if patient is None:
            raise InvalidInput(f"there is no character named {target!r}")
        if patient.dying_hours is None and patient.dying_rounds is None:
            raise InvalidInput(f"{patient.name} is not dying, so {item} has nothing to stop")
    result = inventory_service.use_item(
        db, campaign_id, inventory, name, item, charged_kind, ignore_ready=ignore_ready
    )
    if aid and combat is not None:
        result["stabilized"] = stabilize(
            db,
            campaign_id,
            target,
            f"{name} uses {item} on {target}",
            combat,
            conditions,
            rng,
            plus=plus,
            spec=spec,
        )
    return result
