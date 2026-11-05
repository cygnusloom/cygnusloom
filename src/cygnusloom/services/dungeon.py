"""The dungeon cycle and reaction rolls. They log their results and never move the clock."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from sqlmodel import Session

from cygnusloom.content.definitions import ContentError, required_text
from cygnusloom.content.library import DiceTable
from cygnusloom.rules.dice import roll_total
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import DungeonEventRolled, ReactionRolled, record

if TYPE_CHECKING:
    from cygnusloom.content.character import CharacterSpec


@dataclass(frozen=True)
class DungeonRules:
    event_table: str | None
    reaction_table: str | None
    triggers: tuple[str, ...]
    notes: Mapping[str, str]


def configure_dungeon(
    data: Mapping[str, Any], _character: CharacterSpec, label: str
) -> DungeonRules:
    """Validate a ruleset's dungeon event and reaction settings."""
    unknown = set(data) - {
        "type",
        "id",
        "python_module",
        "configure",
        "event_table",
        "reaction_table",
        "triggers",
        "notes",
    }
    if unknown:
        raise ContentError(f"{label}: unknown settings {sorted(unknown)!r}")
    event_table = required_text(data, "event_table", label) if "event_table" in data else None
    reaction_table = (
        required_text(data, "reaction_table", label) if "reaction_table" in data else None
    )
    if event_table is None and reaction_table is None:
        raise ContentError(f"{label}: an event_table or reaction_table is required")
    triggers = data.get("triggers", [])
    if (
        not isinstance(triggers, list)
        or any(not isinstance(item, str) or not item.strip() for item in triggers)
        or len(set(triggers)) != len(triggers)
    ):
        raise ContentError(f"{label}: 'triggers' must be a list of distinct names")
    notes = data.get("notes", {})
    if not isinstance(notes, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, str)
        or not value.strip()
        for key, value in notes.items()
    ):
        raise ContentError(f"{label}: 'notes' must map result names to non-empty text")
    if event_table is None and (triggers or notes):
        raise ContentError(f"{label}: triggers and notes require an event_table")
    return DungeonRules(event_table, reaction_table, tuple(triggers), MappingProxyType(dict(notes)))


def dungeon_rules(data: Mapping[str, object]) -> DungeonRules:
    rules = data.get("dungeon")
    if not isinstance(rules, DungeonRules):
        raise InvalidInput("this rule set has no valid dungeon roll configuration")
    return rules


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
    rules: DungeonRules,
) -> dict[str, Any]:
    """Roll once on the rule set's dungeon events table. The clock is left alone."""
    unknown = [t for t in triggers if t not in rules.triggers]
    if unknown:
        choices = ", ".join(rules.triggers) or "none"
        raise InvalidInput(f"unknown trigger {unknown[0]!r}; use one of {choices}")
    if rules.event_table is None:
        raise InvalidInput("this rule set has no dungeon event table")
    table = _table(tables, rules.event_table)
    roll = roll_total(table.dice, rng, plus=plus)
    row = table.row_for(roll)
    note = rules.notes.get(row.result, "")
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
    rules: DungeonRules,
) -> dict[str, Any]:
    """Roll on the configured reaction table and log the band."""
    if rules.reaction_table is None:
        raise InvalidInput("this rule set has no reaction table")
    table = _table(tables, rules.reaction_table)
    roll = roll_total(table.dice, rng, plus=plus)
    band = table.row_for(roll).result
    record(db, campaign_id, ReactionRolled(roll=roll, band=band, reason=reason))
    db.commit()
    return {"roll": roll, "band": band, "reason": reason}
