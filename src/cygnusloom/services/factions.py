"""Campaign factions, such as a magic school's colleges.

A faction is a named group the GM invents. Its points live in a dynamic tally, so the existing
award, repeat, and year-end reset paths keep the cup without a second score store.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.library import TallyRules
from cygnusloom.services import tallies as tallies_service
from cygnusloom.services.common import InvalidInput, bounded_text, whole_number
from cygnusloom.services.events import (
    FactionAdded,
    FactionRemoved,
    FactionUpdated,
    load_state,
    record,
)
from cygnusloom.services.state import CampaignState, Faction

MAX_NAME = 120
MAX_TEXT = 2000
MAX_FACTIONS = 5
_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.casefold()).strip("-") or "faction"


def _optional(value: object, label: str, limit: int = MAX_TEXT) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    return bounded_text(value, label, limit)


def _dynamic_tally(tallies: Mapping[str, TallyRules], tally: str) -> TallyRules:
    if tally:
        found = tallies.get(tally)
        if found is None:
            raise InvalidInput(f"there is no tally named {tally!r}")
        if not found.dynamic:
            raise InvalidInput(f"{found.label} does not take entries from the campaign")
        return found
    choices = [candidate for candidate in tallies.values() if candidate.dynamic]
    if not choices:
        raise InvalidInput("this rule set has no dynamic faction tally")
    if len(choices) > 1:
        names = ", ".join(sorted(candidate.id for candidate in choices))
        raise InvalidInput(f"this rule set has more than one faction tally; choose one of {names}")
    return choices[0]


def _unique_id(state: CampaignState, name: str) -> str:
    base = _slug(name)
    candidate = base
    index = 2
    while candidate in state.factions:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def create_faction(
    db: Session,
    campaign_id: int,
    tallies: Mapping[str, TallyRules],
    name: str,
    description: str,
    trait: str,
    *,
    points: int = 0,
    tally: str = "",
) -> dict[str, Any]:
    """Create a faction, with its cup entry starting at `points`."""
    state = load_state(db, campaign_id)
    faction_name = bounded_text(name, "name", MAX_NAME)
    if any(
        existing.name.casefold() == faction_name.casefold() for existing in state.factions.values()
    ):
        raise InvalidInput(f"there is already a faction named {faction_name!r}")
    chosen = _dynamic_tally(tallies, tally)
    members = [existing for existing in state.factions.values() if existing.tally == chosen.id]
    if len(members) >= MAX_FACTIONS:
        raise InvalidInput(
            f"{chosen.label} already has {MAX_FACTIONS} factions; rename or remove one first"
        )
    faction_id = _unique_id(state, faction_name)
    record(
        db,
        campaign_id,
        FactionAdded(
            faction_id=faction_id,
            name=faction_name,
            description=bounded_text(description, "description", MAX_TEXT),
            trait=_optional(trait, "trait", MAX_NAME),
            tally=chosen.id,
            entry=faction_id,
            points=whole_number(points, "points", 0, 9999),
        ),
    )
    db.commit()
    state = load_state(db, campaign_id)
    return _view(state.factions[faction_id], state, tallies)


def update_faction(
    db: Session,
    campaign_id: int,
    faction: str,
    *,
    name: str | None = None,
    description: str | None = None,
    trait: str | None = None,
) -> dict[str, Any]:
    """Change a faction's name, description, or trait. Its id and cup entry stay the same."""
    state = load_state(db, campaign_id)
    found = find_faction(state, faction)
    if found is None:
        raise InvalidInput(f"there is no faction named {faction!r}")
    new_name = bounded_text(name, "name", MAX_NAME) if name is not None else found.name
    if any(
        other.id != found.id and other.name.casefold() == new_name.casefold()
        for other in state.factions.values()
    ):
        raise InvalidInput(f"there is already a faction named {new_name!r}")
    new_description = (
        bounded_text(description, "description", MAX_TEXT)
        if description is not None
        else found.description
    )
    new_trait = _optional(trait, "trait", MAX_NAME) if trait is not None else found.trait
    record(
        db,
        campaign_id,
        FactionUpdated(
            faction_id=found.id,
            name=new_name,
            description=new_description,
            trait=new_trait,
        ),
    )
    db.commit()
    return {
        "id": found.id,
        "name": new_name,
        "description": new_description,
        "trait": new_trait,
    }


def remove_faction(db: Session, campaign_id: int, faction: str) -> dict[str, Any]:
    """Remove a faction and its cup entry."""
    state = load_state(db, campaign_id)
    found = find_faction(state, faction)
    if found is None:
        raise InvalidInput(f"there is no faction named {faction!r}")
    record(db, campaign_id, FactionRemoved(faction_id=found.id, name=found.name))
    db.commit()
    return {"faction": found.name}


def adjust_faction_points(
    db: Session,
    campaign_id: int,
    tallies: Mapping[str, TallyRules],
    faction: str,
    change: int,
    *,
    reason: str = "",
    award: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Add to or deduct from a faction's cup points, with the tally's award protection."""
    state = load_state(db, campaign_id)
    found = find_faction(state, faction)
    if found is None:
        raise InvalidInput(f"there is no faction named {faction!r}")
    tally = tallies.get(found.tally)
    if tally is None:
        raise InvalidInput(f"the tally {found.tally!r} is no longer in this rule set")
    result = tallies_service.adjust_tally(
        db,
        campaign_id,
        tally,
        found.entry,
        change,
        entry_label=found.name,
        reason=reason,
        award=award,
        force=force,
    )
    return {"faction": found.name, **result}


def list_factions(
    db: Session, campaign_id: int, tallies: Mapping[str, TallyRules]
) -> list[dict[str, Any]]:
    state = load_state(db, campaign_id)
    return faction_views(state, tallies)


def faction_views(state: CampaignState, tallies: Mapping[str, TallyRules]) -> list[dict[str, Any]]:
    return [
        _view(faction, state, tallies)
        for faction in sorted(state.factions.values(), key=lambda item: item.name.casefold())
    ]


def find_faction(state: CampaignState, faction: str) -> Faction | None:
    wanted = faction.strip()
    found = state.factions.get(wanted)
    if found is not None:
        return found
    return next(
        (
            candidate
            for candidate in state.factions.values()
            if candidate.name.casefold() == wanted.casefold()
        ),
        None,
    )


def _view(
    faction: Faction, state: CampaignState, tallies: Mapping[str, TallyRules]
) -> dict[str, Any]:
    tally = tallies.get(faction.tally)
    return {
        "id": faction.id,
        "name": faction.name,
        "description": faction.description,
        "trait": faction.trait,
        "tally": faction.tally,
        "entry": faction.entry,
        "cup": tally.label if tally is not None else faction.tally,
        "points": state.tallies.get(faction.tally, {}).get(faction.entry, 0),
    }
