"""The dungeon cycle and reaction rolls. They log their results and never move the clock."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import DiceTable
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import DungeonEventRolled, ReactionRolled, record

DUNGEON_EVENTS = "dungeon_events"
REACTION = "dungeon_reaction"
TRIGGERS = ("new_area", "loud", "hasty", "lingering")

ENCOUNTER_NOTE = (
    "Roll the location's encounter table with roll_tables, then roll_reaction if needed."
)


def _table(tables: Mapping[str, DiceTable], table_id: str) -> DiceTable:
    if table_id not in tables:
        raise InvalidInput(f"this rule set has no dice table named {table_id!r}")
    return tables[table_id]


def dungeon_cycle(
    db: Session,
    campaign_id: int,
    triggers: Sequence[str],
    reason: str,
    tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str,
) -> dict[str, Any]:
    """Roll once on the rule set's dungeon events table. The clock is left alone."""
    unknown = [t for t in triggers if t not in TRIGGERS]
    if unknown:
        raise InvalidInput(f"unknown trigger {unknown[0]!r}; use one of {', '.join(TRIGGERS)}")
    table = _table(tables, DUNGEON_EVENTS)
    roll = roll_total(table.dice, rng, plus=plus)
    row = table.row_for(roll)
    note = ""
    if row.result == "Encounter":
        note = ENCOUNTER_NOTE
    result = {
        "roll": roll,
        "event": row.result,
        "text": row.text,
        "triggers": list(triggers),
        "reason": reason,
        "note": note,
    }
    record(
        db,
        campaign_id,
        DungeonEventRolled(
            roll=roll,
            event=row.result,
            text=row.text,
            triggers=list(triggers),
            reason=reason,
        ),
    )
    db.commit()
    return result


def roll_reaction(
    db: Session,
    campaign_id: int,
    reason: str,
    tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str,
) -> dict[str, Any]:
    """Roll 2d6 for an NPC's reaction and log the band."""
    table = _table(tables, REACTION)
    roll = roll_total(table.dice, rng, plus=plus)
    band = table.row_for(roll).result
    record(db, campaign_id, ReactionRolled(roll=roll, band=band, reason=reason))
    db.commit()
    return {"roll": roll, "band": band, "reason": reason}
