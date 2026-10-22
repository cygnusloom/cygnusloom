"""Rolls on the rule set's dice tables, such as a reaction or a monster trait, and logs each roll."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import DiceTable
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import TablesRolled, record


def roll_tables(
    db: Session,
    campaign_id: int,
    table_ids: Sequence[str],
    tables: Mapping[str, DiceTable],
    rng: random.Random,
) -> dict[str, Any]:
    """Roll once on each named table, in order, and log all the results together."""
    if not table_ids:
        raise InvalidInput("name at least one dice table to roll")
    for table_id in table_ids:
        if table_id not in tables:
            raise InvalidInput(f"there is no dice table named {table_id!r}")
    rolls: list[dict[str, Any]] = []
    for table_id in table_ids:
        table = tables[table_id]
        roll = roll_total(table.dice, rng)
        rolls.append(
            {
                "table": table.id,
                "name": table.name,
                "roll": roll,
                "result": table.row_for(roll).result,
            }
        )
    record(db, campaign_id, TablesRolled(rolls=rolls))
    db.commit()
    return {"rolls": rolls}
