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
    *,
    plus: str,
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
        roll = roll_total(table.dice, rng, plus=plus)
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


def roll_creative(
    db: Session,
    campaign_id: int,
    table_ids: Sequence[str] | None,
    tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str,
) -> dict[str, Any]:
    """Roll on the named creative tables, or on every creative table when none are named."""
    creative = [table.id for table in tables.values() if table.creative]
    if table_ids:
        for table_id in table_ids:
            if table_id not in creative:
                raise InvalidInput(f"there is no creative table named {table_id!r}")
        chosen = list(table_ids)
    else:
        chosen = creative
    if not chosen:
        raise InvalidInput("no creative tables are defined")
    return roll_tables(db, campaign_id, chosen, tables, rng, plus=plus)
