"""Named campaign clocks: progress tracks such as noise, a horde's attention, or a faction
front. A clock is stored in the campaign state and advanced by events; crossing a threshold in
its definition runs that threshold's effects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.library import CampaignClockDefinition
from cygnusloom.services.effects import EffectContext, run_effects
from cygnusloom.services.events import (
    CampaignClockAdvanced,
    CampaignClockEnded,
    CampaignClockStarted,
    load_state,
    record,
)
from cygnusloom.services.state import CampaignClock, alias_key


def _find(state: Any, key: str) -> CampaignClock | None:
    wanted = alias_key(key)
    clock = state.campaign_clocks.get(wanted)
    if clock is not None:
        return clock
    for clock in state.campaign_clocks.values():
        if alias_key(clock.name) == wanted or alias_key(clock.definition) == wanted:
            return clock
    return None


def definition_for(
    key: str, definitions: Mapping[str, CampaignClockDefinition]
) -> CampaignClockDefinition | None:
    wanted = alias_key(key)
    definition = definitions.get(key)
    if definition is not None:
        return definition
    for candidate in definitions.values():
        if alias_key(candidate.id) == wanted or alias_key(candidate.label) == wanted:
            return candidate
    return None


def ensure_clock(
    db: Session,
    campaign_id: int,
    name: str,
    definition: CampaignClockDefinition,
    *,
    filled: int | None = None,
    visible: bool | None = None,
) -> CampaignClock:
    """Start a clock if it is not already running, and return it."""
    state = load_state(db, campaign_id)
    existing = _find(state, name)
    if existing is not None:
        return existing
    start = definition.initial if filled is None else filled
    start = max(0, min(definition.segments, start))
    record(
        db,
        campaign_id,
        CampaignClockStarted(
            id=definition.id,
            name=definition.label,
            definition=definition.id,
            segments=definition.segments,
            filled=start,
            visible=definition.visible if visible is None else visible,
            description=definition.description,
        ),
    )
    db.commit()
    return load_state(db, campaign_id).campaign_clocks[alias_key(definition.id)]


def advance_clock(
    db: Session,
    campaign_id: int,
    name: str,
    amount: int,
    *,
    cause: str,
    definitions: Mapping[str, CampaignClockDefinition],
    cx: EffectContext,
    plus: str,
    set_to: int | None = None,
) -> dict[str, Any]:
    """Advance or set a named clock, firing each threshold crossed upward."""
    state = load_state(db, campaign_id)
    clock = _find(state, name)
    if clock is None:
        definition = definition_for(name, definitions)
        if definition is None:
            raise ValueError(f"there is no clock named {name!r}")
        clock = ensure_clock(db, campaign_id, name, definition)
        state = load_state(db, campaign_id)
    definition = definitions.get(clock.definition) or definition_for(clock.definition, definitions)
    segments = clock.segments or (definition.segments if definition else 6)
    before = clock.filled
    after = set_to if set_to is not None else before + amount
    after = max(0, min(segments, after))
    if after != before:
        record(
            db,
            campaign_id,
            CampaignClockAdvanced(
                id=clock.id,
                name=clock.name,
                filled=after,
                segments=segments,
                reason=cause,
            ),
        )
        db.commit()
    if definition is not None:
        for threshold in sorted(definition.thresholds):
            if before < threshold <= after:
                run_effects(
                    db,
                    campaign_id,
                    name,
                    definition.thresholds[threshold],
                    cx,
                    f"{clock.name} reaches {threshold}",
                    plus=plus,
                )
    return clock_view(clock, after, segments)


def end_clock(
    db: Session,
    campaign_id: int,
    name: str,
    *,
    cause: str = "",
) -> bool:
    state = load_state(db, campaign_id)
    clock = _find(state, name)
    if clock is None:
        return False
    record(
        db,
        campaign_id,
        CampaignClockEnded(id=clock.id, name=clock.name, reason=cause),
    )
    db.commit()
    return True


def clock_view(clock: CampaignClock, filled: int | None = None, segments: int | None = None) -> dict[str, Any]:
    return {
        "id": clock.id,
        "name": clock.name,
        "definition": clock.definition,
        "filled": clock.filled if filled is None else filled,
        "segments": clock.segments if segments is None else segments,
        "visible": clock.visible,
        "description": clock.description,
    }


def clock_views(state: Any, *, include_hidden: bool = False) -> list[dict[str, Any]]:
    return [
        clock_view(clock)
        for clock in sorted(state.campaign_clocks.values(), key=lambda c: (c.name, c.id))
        if include_hidden or clock.visible
    ]
