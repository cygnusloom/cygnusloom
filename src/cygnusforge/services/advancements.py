"""Spending an Advancement. The rule set's JSON says what each option costs and does, so this module
only checks the request, spends the point, and runs the option's effects."""

from __future__ import annotations

import random
from collections.abc import Mapping

from sqlmodel import Session

from cygnusforge.content.library import (
    AdvancementRules,
    CombatRules,
    ConditionRules,
    CounterRules,
)
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import AdvancementTaken, load_state, name_key, record
from cygnusforge.services.injuries import EffectContext, change_counter, run_effects


def take_advancement(
    db: Session,
    campaign_id: int,
    name: str,
    option_id: str,
    *,
    advancements: AdvancementRules | None,
    counters: Mapping[str, CounterRules],
    conditions: Mapping[str, ConditionRules],
    combat: CombatRules | None,
    rng: random.Random,
    plus: str,
    stat: str | None = None,
) -> dict[str, object]:
    """Spend one point of the Advancement counter on `option_id`, and run the option's effects. `stat`
    names the stat an option that picks one raises; other options take none."""
    if advancements is None:
        raise InvalidInput("this rule set has no Advancements")
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    option = advancements.options.get(option_id)
    if option is None:
        raise InvalidInput(
            f"there is no Advancement {option_id!r}; choose one of {sorted(advancements.options)}"
        )
    if option.limit is not None and character.advancements_taken.get(option_id, 0) >= option.limit:
        raise InvalidInput(f"{character.name} has already taken {option.label} as often as allowed")
    if option.picks and stat not in character.stats:
        raise InvalidInput(f"{option.label} needs a stat; choose one of {sorted(character.stats)}")
    if not option.picks and stat is not None:
        raise InvalidInput(f"{option.label} does not take a stat")
    counter = counters[advancements.counter]
    points = character.counters.get(advancements.counter, counter.initial)
    if points < 1:
        raise InvalidInput(f"{character.name} has no {advancements.label} to spend")

    record(
        db,
        campaign_id,
        AdvancementTaken(
            name=character.name, option=option_id, label=option.label, stat=stat or ""
        ),
    )
    cx = EffectContext(
        rng=rng, combat=combat, picked=stat, counters=counters, conditions=conditions
    )
    change_counter(
        db,
        campaign_id,
        character.name,
        counter,
        -1,
        absolute=False,
        cause=f"{character.name} takes {option.label}",
        cx=cx,
        plus=plus,
    )
    run_effects(
        db,
        campaign_id,
        character.name,
        option.effects,
        cx,
        cause=f"{character.name} takes {option.label}",
        plus=plus,
    )
    value = (
        load_state(db, campaign_id)
        .characters[name_key(name)]
        .counters.get(advancements.counter, counter.initial)
    )
    db.commit()
    return {
        "character": character.name,
        "advancement": option.label,
        "stat": stat,
        "remaining": value,
    }
