"""Hordes rule hooks. A safe night clears one harm; a full day clears two."""

from __future__ import annotations

from cygnusloom.services.events import CounterChanged, record
from cygnusloom.services.hooks import HookContext
from cygnusloom.services.state import PC_KIND

HARM_RECOVERY = {"night": 1, "day": 2}


def after_rest(
    hc: HookContext,
    name: str,
    length: str,
    start: int,
    end: int,
    interrupted: bool,
) -> None:
    """Clear harm after a full rest. An interrupted rest clears none."""
    if interrupted:
        return
    amount = HARM_RECOVERY.get(length, 0)
    if not amount:
        return
    character = hc.character(name)
    if character.kind != PC_KIND:
        return
    before = character.counters.get("harm", 0)
    after = max(0, before - amount)
    if after == before:
        return
    db, campaign_id = hc.session()
    record(
        db,
        campaign_id,
        CounterChanged(
            name=character.name,
            counter="harm",
            label="Harm",
            before=before,
            after=after,
            cause=f"a {length} rest",
        ),
    )
