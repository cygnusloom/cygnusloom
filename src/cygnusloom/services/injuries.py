"""Injuries and the rule effects they carry. An injury table is JSON; when a character gains an
entry, the effect ops it lists run through services.effects."""

from __future__ import annotations

from sqlmodel import Session

from cygnusloom.content.library import InjuryEntry, InjuryTable
from cygnusloom.services.effects import (
    EffectContext,
    change_counter,
    gain_condition,
    run_effects,
)
from cygnusloom.services.events import InjuryGained, record

__all__ = [
    "EffectContext",
    "change_counter",
    "gain_condition",
    "gain_injury",
    "run_effects",
]


def gain_injury(
    db: Session,
    campaign_id: int,
    name: str,
    table: InjuryTable,
    roll: int,
    cx: EffectContext,
    *,
    plus: str,
) -> InjuryEntry:
    """Give a character the injury at `roll` on the table, then run the injury's effects."""
    entry = table.entries[roll - 1]
    record(
        db,
        campaign_id,
        InjuryGained(name=name, injury=entry.name, roll=entry.roll, text=entry.text),
    )
    db.commit()
    run_effects(db, campaign_id, name, entry.effects, cx, f"injury {entry.name}", plus=plus)
    return entry
