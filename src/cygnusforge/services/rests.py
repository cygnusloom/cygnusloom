"""Rests. A rest takes time and, for the party, restores what the rule set says it restores.
A character a rule set holds back (see the held_back hook) recovers nothing until that ends."""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusforge.content.character import CharacterSpec
from cygnusforge.content.library import (
    ClockRules,
    CombatRules,
    ConditionRules,
    Hooks,
    InventoryRules,
    RestLength,
)
from cygnusforge.services.clock import absolute, advance_to, rest_target
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.conditions import end_condition
from cygnusforge.services.events import (
    PC_KIND,
    Recovered,
    RestTaken,
    load_state,
    record,
)
from cygnusforge.services.hooks import HookContext, call_hook
from cygnusforge.services.inventory import clear_items
from cygnusforge.services.recovery import recover

HISTORY_LIMIT = 20


def take_rest(
    db: Session,
    campaign_id: int,
    clock_rules: ClockRules,
    rests: Mapping[str, RestLength],
    interrupted_rest: str,
    hooks: Hooks | None,
    inventory: InventoryRules | None,
    length: str,
    interrupted: bool,
    reason: str,
    *,
    combat: CombatRules | None = None,
    conditions: Mapping[str, ConditionRules] | None = None,
    rng: random.Random | None = None,
    plus: str = "sum",
    spec: CharacterSpec | None = None,
) -> dict[str, Any]:
    """The party rests. An interrupted rest counts as the rule set's interrupted length. A rest
    with upkeep calls the before_rest hook first. Time passes, then each party member the rule set
    does not hold back recovers what the rest restores. A full rest also clears the item kinds it
    lists. A recovering member's conditions end when this rest is a trigger they list, such as a
    full rest or an extended one."""
    chosen_id = interrupted_rest if interrupted else length
    if chosen_id not in rests:
        raise InvalidInput(f"there is no rest length named {length!r}")
    rest = rests[chosen_id]
    parts_per_day = len(clock_rules.parts)
    hours_per_part = clock_rules.hours_per_part
    state = load_state(db, campaign_id)
    start = absolute(state.clock, parts_per_day, hours_per_part)
    record(
        db,
        campaign_id,
        RestTaken(
            length=rest.id,
            full=rest.full,
            interrupted=interrupted,
            reason=reason,
            day=state.clock.day,
            part=clock_rules.parts[state.clock.part],
        ),
    )
    hc = HookContext(db, campaign_id, hooks, inventory)
    result: dict[str, Any] = {"length": rest.id, "interrupted": interrupted}
    if rest.upkeep:
        call_hook(hooks, "before_rest", hc, rest.id, start, hours_per_part, default=None)
    result.update(
        advance_to(
            db,
            campaign_id,
            clock_rules,
            rest_target(start, rest, clock_rules),
            hooks,
            inventory,
            passed="the rest is over and time has passed",
        )
    )

    # Recovery uses the state after time has passed, so someone held back during the rest gets none.
    state = load_state(db, campaign_id)
    party = [c for c in state.characters.values() if c.kind == PC_KIND]
    held = {c.name: call_hook(hooks, "held_back", hc, c.name, default="") for c in party}
    recovering = [c for c in party if not held[c.name]]
    result["held_back"] = [c.name for c in party if held[c.name]]
    for character in recovering:
        for stat in rest.restores:
            restored = recover(character, stat, character.harm.get(stat, 0))
            if restored:
                record(
                    db,
                    campaign_id,
                    Recovered(
                        name=character.name,
                        stat=stat,
                        amount=restored,
                        restores=list(rest.restores),
                    ),
                )
    if rest.full and rest.clears:
        clear_items(db, campaign_id, [c.name for c in recovering], rest.clears)
    if combat is not None and conditions and rng is not None:
        # The rest definition names the condition triggers it counts as, such as "rest" or
        # "extended_rest"; the engine does not infer them from the length or fullness.
        triggers = rest.ends
        for character in recovering:
            for cid in list(character.conditions):
                cond_rules = conditions.get(cid)
                if cond_rules is None:
                    continue
                how = next((t for t in triggers if t in cond_rules.ends), None)
                if how is not None:
                    end_condition(
                        db,
                        campaign_id,
                        character.name,
                        cid,
                        how,
                        combat,
                        conditions,
                        rng,
                        plus=plus,
                        spec=spec,
                    )
    result["recovered"] = [c.name for c in recovering]
    db.commit()
    return result


def rest_history(db: Session, campaign_id: int) -> dict[str, Any]:
    """The party's most recent rests, newest first."""
    rests = load_state(db, campaign_id).rests[-HISTORY_LIMIT:]
    return {
        "rests": [
            {
                "day": rest.day,
                "part": rest.part,
                "length": rest.length,
                "full": rest.full,
                "interrupted": rest.interrupted,
                "reason": rest.reason,
            }
            for rest in reversed(rests)
        ]
    }
