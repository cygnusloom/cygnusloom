"""Ending a stretch of play, such as a school year. Every player character gets the period's effects,
which the rule set's JSON lists."""

from __future__ import annotations

import random
from collections.abc import Mapping

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import (
    CombatRules,
    ConditionRules,
    CounterRules,
    PeriodRules,
    TallyRules,
)
from cygnusloom.services import reactions
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import PC_KIND, PeriodEnded, TallyReset, load_state, record
from cygnusloom.services.injuries import EffectContext, run_effects


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
        labels = dict(tally.entries)
        if tally.dynamic:
            for faction in state.factions.values():
                if faction.tally == tally_id:
                    labels[faction.entry] = faction.name
            for entry in state.tallies.get(tally_id, {}):
                labels.setdefault(entry, entry)
        final = {entry: state.tallies.get(tally_id, {}).get(entry, 0) for entry in labels}
        record(
            db,
            campaign_id,
            TallyReset(tally=tally.id, label=tally.label, final=final, labels=labels),
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
    for reset in period.resets:
        reactions.reset(db, campaign_id, reset, label=period.label)
    db.commit()
    return {"period": period.label, "characters": [c.name for c in pcs]}
