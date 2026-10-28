"""Counters a character keeps, changed by the GM. A counter's change, its bounds, and its full effects
come from the rule set's JSON, so this module only checks the request and hands it on."""

from __future__ import annotations

import random
from collections.abc import Mapping

from sqlmodel import Session

from cygnusforge.content.library import CombatRules, ConditionRules, CounterRules
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import load_state, name_key
from cygnusforge.services.injuries import EffectContext, change_counter


def adjust_counter(
    db: Session,
    campaign_id: int,
    name: str,
    counter: str,
    change: int,
    *,
    counters: Mapping[str, CounterRules],
    conditions: Mapping[str, ConditionRules],
    combat: CombatRules | None,
    rng: random.Random,
    plus: str,
    reason: str = "",
) -> dict[str, object]:
    """Add `change` to a character's counter, within its bounds, and report its value after."""
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    rules = counters.get(counter)
    if rules is None:
        raise InvalidInput(
            f"there is no counter named {counter!r}; choose one of {sorted(counters)}"
        )
    change_counter(
        db,
        campaign_id,
        character.name,
        rules,
        change,
        absolute=False,
        cause=reason or "the GM's adjustment",
        cx=EffectContext(rng=rng, combat=combat, counters=counters, conditions=conditions),
        plus=plus,
    )
    value = (
        load_state(db, campaign_id).characters[name_key(name)].counters.get(counter, rules.initial)
    )
    return {"character": character.name, "counter": rules.label, "value": value}
