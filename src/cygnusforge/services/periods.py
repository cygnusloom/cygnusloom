"""Ending a stretch of play, such as a school year. Every player character gets the period's effects,
which the rule set's JSON lists."""

from __future__ import annotations

import random
from collections.abc import Mapping

from sqlmodel import Session

from cygnusforge.content.character import CharacterSpec
from cygnusforge.content.library import (
    CombatRules,
    ConditionRules,
    CounterRules,
    PeriodRules,
    TallyRules,
)
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import PC_KIND, PeriodEnded, TallyReset, load_state, record
from cygnusforge.services.injuries import EffectContext, run_effects


def end_period(
    db: Session,
    campaign_id: int,
    period: PeriodRules | None,
    *,
    counters: Mapping[str, CounterRules],
    conditions: Mapping[str, ConditionRules],
    combat: CombatRules | None,
    tallies: Mapping[str, TallyRules],
    rng: random.Random,
    plus: str,
    spec: CharacterSpec | None = None,
) -> dict[str, object]:
    """End the period for every living player character, running its effects for each of them. The
    period's tallies start over first, and their final totals are logged."""
    if period is None:
        raise InvalidInput("this rule set has no periods to end")
    state = load_state(db, campaign_id)
    pcs = [c for c in state.characters.values() if c.kind == PC_KIND and not c.retired]
    record(db, campaign_id, PeriodEnded(period=period.id, label=period.label))
    for tally_id in period.tallies:
        tally = tallies[tally_id]
        final = {entry: state.tallies.get(tally_id, {}).get(entry, 0) for entry in tally.entries}
        record(
            db,
            campaign_id,
            TallyReset(tally=tally.id, label=tally.label, final=final, labels=dict(tally.entries)),
        )
    cx = EffectContext(rng=rng, combat=combat, counters=counters, conditions=conditions, spec=spec)
    for character in pcs:
        run_effects(
            db,
            campaign_id,
            character.name,
            period.effects,
            cx,
            cause=f"the {period.label} ends",
            plus=plus,
        )
    db.commit()
    return {"period": period.label, "characters": [c.name for c in pcs]}
