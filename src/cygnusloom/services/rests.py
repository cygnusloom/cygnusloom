"""Rests. A rest takes time and, for the party, restores what the rule set says it restores.
A character a rule set holds back (see the held_back hook) recovers nothing until that ends."""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import (
    ClockRules,
    CombatRules,
    ConditionRules,
    Hooks,
    InventoryRules,
    RestLength,
)
from cygnusloom.services.clock import absolute, advance_to, rest_target
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.conditions import end_condition
from cygnusloom.services.events import (
    PC_KIND,
    Recovered,
    RestTaken,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.hooks import HookContext, call_hook
from cygnusloom.services.inventory import clear_items
from cygnusloom.services.recovery import recover

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
    rule_data: Mapping[str, object] | None = None,
    scope: str = "party",
    character: str = "",
    advance_clock: bool | None = None,
) -> dict[str, Any]:
    """The party rests. An interrupted rest counts as the rule set's interrupted length. A rest
    with upkeep calls the before_rest hook first. Time passes, then each party member the rule set
    does not hold back recovers what the rest restores. A full rest also clears the item kinds it
    lists. A recovering member's conditions end when this rest is a trigger they list, such as a
    full rest or an extended one."""
    chosen_id = interrupted_rest if interrupted else length
    if chosen_id not in rests:
        raise InvalidInput(f"there is no rest length named {length!r}")
    if scope not in ("party", "character"):
        raise InvalidInput(f"there is no rest scope named {scope!r}")
    rest = rests[chosen_id]
    parts_per_day = len(clock_rules.parts)
    hours_per_part = clock_rules.hours_per_part
    state = load_state(db, campaign_id)
    if scope == "character":
        target = state.characters.get(name_key(character)) if character else None
        if target is None or target.kind != PC_KIND:
            raise InvalidInput(f"there is no player character named {character!r}")
        targets = [target]
    else:
        targets = [c for c in state.characters.values() if c.kind == PC_KIND]
    if advance_clock is None:
        # A per-character recovery must not make the party sleep once per character. A caller
        # that wants the clock moved for a single character can ask for it explicitly.
        advance_clock = scope == "party"
    start = absolute(state.clock, parts_per_day, hours_per_part)
    took = RestTaken(
        length=rest.id,
        full=rest.full,
        interrupted=interrupted,
        reason=reason,
        day=state.clock.day,
        part=clock_rules.parts[state.clock.part],
        scope=scope,
        name=targets[0].name if scope == "character" else "",
    )
    record(db, campaign_id, took)
    hc = HookContext(db, campaign_id, hooks, inventory, rule_data or {})
    result: dict[str, Any] = {
        "length": rest.id,
        "interrupted": interrupted,
        "scope": scope,
    }
    if rest.upkeep:
        call_hook(hooks, "before_rest", hc, rest.id, start, hours_per_part, default=None)
    end = rest_target(start, rest, clock_rules)
    if advance_clock:
        result.update(
            advance_to(
                db,
                campaign_id,
                clock_rules,
                end,
                hooks,
                inventory,
                passed="the rest is over and time has passed",
            )
        )

    # Recovery uses the state after time has passed, so someone held back during the rest gets none.
    state = load_state(db, campaign_id)
    names = {c.name for c in targets}
    party = [c for c in state.characters.values() if c.name in names]
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
    for target in recovering:
        call_hook(
            hooks,
            "after_rest",
            hc,
            target.name,
            rest.id,
            start,
            end,
            interrupted,
            default=None,
        )
    if conditions is not None and rng is not None:
        # The rest definition names the condition triggers it counts as, such as "rest" or
        # "extended_rest"; the engine does not infer them from the length or fullness. A
        # rule set without combat rules, such as a school game, still ends conditions this way.
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
                "scope": rest.scope,
                "name": rest.name,
            }
            for rest in reversed(rests)
        ]
    }
