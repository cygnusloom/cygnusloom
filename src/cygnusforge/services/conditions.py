"""Conditions a character has. A stored condition is recorded on the character and ends when a
trigger the rule set names happens. A derived condition holds while the character's stats say so,
and has nothing stored to end."""

from __future__ import annotations

import random
from collections.abc import Mapping

from sqlmodel import Session

from cygnusforge.content.library import CONDITION_ENDS, CombatRules, ConditionRules
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    Character,
    ConditionEnded,
    current,
    load_state,
    name_key,
    record,
)
from cygnusforge.services.scars import EffectContext, run_effects


def derived_holds(character: Character, rules: ConditionRules) -> bool:
    """Whether a derived condition holds: its stat is at the set score and, if it needs a maximum,
    the stat's maximum is above 0."""
    derived = rules.derived
    assert derived is not None
    if current(character, derived.stat) != derived.current:
        return False
    return not derived.needs_max or character.stats.get(derived.stat, 0) > 0


def active_conditions(
    character: Character, conditions: Mapping[str, ConditionRules]
) -> list[ConditionRules]:
    """The conditions a character has now: the stored ones in the order gained, then the derived
    ones that hold."""
    stored = [conditions[cid] for cid in character.conditions if cid in conditions]
    derived = [
        rules
        for rules in conditions.values()
        if rules.derived is not None and derived_holds(character, rules)
    ]
    return stored + derived


def end_condition(
    db: Session,
    campaign_id: int,
    name: str,
    condition: str,
    how: str,
    combat: CombatRules,
    conditions: Mapping[str, ConditionRules],
    rng: random.Random,
) -> dict[str, object]:
    """End a stored condition. `how` is the trigger that ends it, which must be one the condition
    lists, or `gm` for the GM ending it by decision. Then the condition's on-end effects run."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    rules = conditions.get(condition)
    if rules is None:
        raise InvalidInput(f"there is no condition named {condition!r}")
    if rules.derived is not None:
        raise InvalidInput(f"{rules.label} holds while the stats say so and cannot be ended")
    if condition not in character.conditions:
        raise InvalidInput(f"{character.name} does not have the condition {rules.label}")
    if how not in CONDITION_ENDS:
        raise InvalidInput(f"how must be one of {', '.join(CONDITION_ENDS)}")
    if how != "gm" and how not in rules.ends:
        allowed = ", ".join(rules.ends) or "none"
        raise InvalidInput(f"{rules.label} ends by {allowed}, not by {how}")
    record(db, campaign_id, ConditionEnded(name=character.name, condition=condition, how=how))
    db.commit()
    run_effects(
        db,
        campaign_id,
        character.name,
        rules.on_end,
        EffectContext(rng=rng, combat=combat),
        f"{rules.label} ended",
    )
    return {"character": character.name, "condition": condition, "ended_by": how}
