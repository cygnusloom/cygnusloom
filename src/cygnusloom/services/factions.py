"""Campaign factions, such as a magic school's colleges, and their assets.

A faction is a named group the GM invents. A rule set with a dynamic tally keeps the faction's
points in that tally, so the existing award, repeat, and year-end reset paths keep the cup
without a second score store. A rule set with no faction tally, such as Worlds Without Number,
makes tally-less factions whose assets and turn are their whole record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.library import FactionAssetDefinition, TallyRules
from cygnusloom.services import tallies as tallies_service
from cygnusloom.services.common import InvalidInput, bounded_text, whole_number
from cygnusloom.services.events import (
    FactionAdded,
    FactionAssetActed,
    FactionAssetAdded,
    FactionAssetDamaged,
    FactionAssetRemoved,
    FactionAssetRepaired,
    FactionAssetUpdated,
    FactionPointsChanged,
    FactionRemoved,
    FactionTurnTaken,
    FactionUpdated,
    load_state,
    record,
)
from cygnusloom.services.state import CampaignState, Faction, FactionAsset

MAX_NAME = 120
MAX_TEXT = 2000
_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.casefold()).strip("-") or "faction"


def _optional(value: object, label: str, limit: int = MAX_TEXT) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    return bounded_text(value, label, limit)


def _dynamic_tally(tallies: Mapping[str, TallyRules], tally: str) -> TallyRules | None:
    """The tally a faction scores in, or None for a tally-less faction. An explicit tally is
    always validated; with none, a rule set that offers no dynamic tally makes tally-less
    factions instead of refusing every faction."""
    if tally:
        found = tallies.get(tally)
        if found is None:
            raise InvalidInput(f"there is no tally named {tally!r}")
        if not found.dynamic:
            raise InvalidInput(f"{found.label} does not take entries from the campaign")
        return found
    choices = [candidate for candidate in tallies.values() if candidate.dynamic]
    if not choices:
        return None
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
    starting_points = whole_number(points, "points", 0, 9999)
    chosen = _dynamic_tally(tallies, tally)
    faction_id = _unique_id(state, faction_name)
    if chosen is not None:
        members = [existing for existing in state.factions.values() if existing.tally == chosen.id]
        if chosen.max_entries is not None and len(members) >= chosen.max_entries:
            raise InvalidInput(
                f"{chosen.label} already has {chosen.max_entries} factions; "
                "rename or remove one first"
            )
        tally_id = chosen.id
        entry = faction_id
    else:
        tally_id, entry = "", ""
    record(
        db,
        campaign_id,
        FactionAdded(
            faction_id=faction_id,
            name=faction_name,
            description=bounded_text(description, "description", MAX_TEXT),
            trait=_optional(trait, "trait", MAX_NAME),
            tally=tally_id,
            entry=entry,
            points=starting_points,
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
    if not found.tally:
        before = found.points
        after = before + change
        record(
            db,
            campaign_id,
            FactionPointsChanged(
                faction_id=found.id,
                before=before,
                after=after,
                cause=reason.strip(),
            ),
        )
        db.commit()
        return {"faction": found.name, "before": before, "value": after}
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


def _find_asset(faction: Faction, asset: str) -> FactionAsset | None:
    wanted = asset.strip()
    found = faction.assets.get(wanted)
    if found is not None:
        return found
    return next(
        (
            candidate
            for candidate in faction.assets.values()
            if candidate.name.casefold() == wanted.casefold()
        ),
        None,
    )


def _asset_definition(
    definitions: Mapping[str, FactionAssetDefinition], kind: str
) -> FactionAssetDefinition:
    wanted = kind.strip()
    found = definitions.get(wanted)
    if found is None:
        found = next(
            (
                candidate
                for candidate in definitions.values()
                if candidate.label.casefold() == wanted.casefold()
            ),
            None,
        )
    if found is None:
        raise InvalidInput(f"there is no faction asset kind named {kind!r}")
    return found


def _unique_asset_id(faction: Faction, name: str) -> str:
    base = _slug(name)
    candidate = base
    index = 2
    while candidate in faction.assets:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _qualities(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise InvalidInput("qualities must be a list of non-empty strings")
    return [item.strip() for item in value]


def _asset_view(faction: Faction, asset: FactionAsset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "kind": asset.kind,
        "name": asset.name,
        "category": asset.category,
        "hp": asset.hp,
        "hp_max": asset.hp_max,
        "magic": asset.magic,
        "attack": asset.attack,
        "counter": asset.counter,
        "qualities": list(asset.qualities),
        "notes": list(asset.notes),
        "acted_turn": asset.acted_turn,
        "ready": faction.asset_ready(asset),
    }


def _faction_for(state: CampaignState, faction: str) -> Faction:
    found = find_faction(state, faction)
    if found is None:
        raise InvalidInput(f"there is no faction named {faction!r}")
    return found


def _asset_for(faction: Faction, asset: str) -> FactionAsset:
    found = _find_asset(faction, asset)
    if found is None:
        raise InvalidInput(f"there is no asset named {asset!r} in {faction.name}")
    return found


def add_faction_asset(
    db: Session,
    campaign_id: int,
    definitions: Mapping[str, FactionAssetDefinition],
    faction_name: str,
    asset_kind: str,
    name: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Buy a rule set's asset for a faction, resolving its stats from the definition."""
    state = load_state(db, campaign_id)
    faction = _faction_for(state, faction_name)
    definition = _asset_definition(definitions, asset_kind)
    asset_name = bounded_text(name, "name", MAX_NAME) if name.strip() else definition.label
    asset_id = _unique_asset_id(faction, asset_name)
    note = _optional(notes, "notes")
    record(
        db,
        campaign_id,
        FactionAssetAdded(
            faction_id=faction.id,
            asset_id=asset_id,
            asset_kind=definition.id,
            name=asset_name,
            category=definition.category,
            hp_max=definition.hp,
            hp=definition.hp,
            magic=definition.magic,
            attack=definition.attack,
            counter=definition.counter,
            qualities=list(definition.qualities),
            notes=[note] if note else [],
        ),
    )
    db.commit()
    state = load_state(db, campaign_id)
    faction = state.factions[faction.id]
    return {
        "faction": faction.name,
        "turn": faction.turn,
        **_asset_view(faction, faction.assets[asset_id]),
    }


def update_faction_asset(
    db: Session,
    campaign_id: int,
    faction_name: str,
    asset_name: str,
    *,
    name: str | None = None,
    notes: str | None = None,
    qualities: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Change an asset's display name, qualities, or notes. A note is appended, not replaced."""
    state = load_state(db, campaign_id)
    faction = _faction_for(state, faction_name)
    asset = _asset_for(faction, asset_name)
    new_name = bounded_text(name, "name", MAX_NAME) if name is not None else asset.name
    new_notes = list(asset.notes)
    if notes is not None:
        note = _optional(notes, "notes")
        if note:
            new_notes.append(note)
    new_qualities = list(asset.qualities) if qualities is None else _qualities(qualities)
    record(
        db,
        campaign_id,
        FactionAssetUpdated(
            faction_id=faction.id,
            asset_id=asset.id,
            name=new_name,
            hp=asset.hp,
            qualities=new_qualities,
            notes=new_notes,
        ),
    )
    db.commit()
    state = load_state(db, campaign_id)
    faction = state.factions[faction.id]
    return {
        "faction": faction.name,
        "turn": faction.turn,
        **_asset_view(faction, faction.assets[asset.id]),
    }


def remove_faction_asset(
    db: Session, campaign_id: int, faction_name: str, asset_name: str
) -> dict[str, Any]:
    """Remove an asset from a faction."""
    state = load_state(db, campaign_id)
    faction = _faction_for(state, faction_name)
    asset = _asset_for(faction, asset_name)
    record(
        db,
        campaign_id,
        FactionAssetRemoved(faction_id=faction.id, asset_id=asset.id, name=asset.name),
    )
    db.commit()
    return {"faction": faction.name, "asset": asset.name}


def faction_asset_acted(
    db: Session, campaign_id: int, faction_name: str, asset_name: str, note: str = ""
) -> dict[str, Any]:
    """Use an asset's action for this faction turn. It cannot act again until the turn ends."""
    state = load_state(db, campaign_id)
    faction = _faction_for(state, faction_name)
    asset = _asset_for(faction, asset_name)
    if asset.acted_turn == faction.turn:
        raise InvalidInput(f"{asset.name} has already acted this faction turn")
    record(
        db,
        campaign_id,
        FactionAssetActed(
            faction_id=faction.id,
            asset_id=asset.id,
            name=asset.name,
            note=_optional(note, "note"),
        ),
    )
    db.commit()
    state = load_state(db, campaign_id)
    faction = state.factions[faction.id]
    return {
        "faction": faction.name,
        "turn": faction.turn,
        **_asset_view(faction, faction.assets[asset.id]),
    }


def damage_faction_asset(
    db: Session, campaign_id: int, faction_name: str, asset_name: str, amount: int
) -> dict[str, Any]:
    """Reduce an asset's hit points, never below zero."""
    state = load_state(db, campaign_id)
    faction = _faction_for(state, faction_name)
    asset = _asset_for(faction, asset_name)
    damage = whole_number(amount, "damage", 1, 9999)
    record(
        db,
        campaign_id,
        FactionAssetDamaged(
            faction_id=faction.id, asset_id=asset.id, amount=damage, name=asset.name
        ),
    )
    db.commit()
    state = load_state(db, campaign_id)
    faction = state.factions[faction.id]
    return {
        "faction": faction.name,
        "turn": faction.turn,
        **_asset_view(faction, faction.assets[asset.id]),
    }


def repair_faction_asset(
    db: Session, campaign_id: int, faction_name: str, asset_name: str, amount: int
) -> dict[str, Any]:
    """Restore an asset's hit points, never above its maximum."""
    state = load_state(db, campaign_id)
    faction = _faction_for(state, faction_name)
    asset = _asset_for(faction, asset_name)
    repair = whole_number(amount, "repair", 1, 9999)
    record(
        db,
        campaign_id,
        FactionAssetRepaired(
            faction_id=faction.id, asset_id=asset.id, amount=repair, name=asset.name
        ),
    )
    db.commit()
    state = load_state(db, campaign_id)
    faction = state.factions[faction.id]
    return {
        "faction": faction.name,
        "turn": faction.turn,
        **_asset_view(faction, faction.assets[asset.id]),
    }


def take_faction_turn(
    db: Session, campaign_id: int, faction_name: str, note: str = ""
) -> dict[str, Any]:
    """Begin the faction's next turn, readying every asset that acted in the last one."""
    state = load_state(db, campaign_id)
    faction = _faction_for(state, faction_name)
    turn = faction.turn + 1
    record(
        db,
        campaign_id,
        FactionTurnTaken(
            faction_id=faction.id,
            turn=turn,
            name=faction.name,
            note=_optional(note, "note"),
        ),
    )
    db.commit()
    state = load_state(db, campaign_id)
    faction = state.factions[faction.id]
    return {
        "faction": faction.name,
        "turn": faction.turn,
        "assets": [
            _asset_view(faction, asset)
            for asset in sorted(faction.assets.values(), key=lambda item: item.name.casefold())
        ],
    }


def _view(
    faction: Faction, state: CampaignState, tallies: Mapping[str, TallyRules]
) -> dict[str, Any]:
    tally = tallies.get(faction.tally)
    points = (
        state.tallies.get(faction.tally, {}).get(faction.entry, 0)
        if faction.tally
        else faction.points
    )
    return {
        "id": faction.id,
        "name": faction.name,
        "description": faction.description,
        "trait": faction.trait,
        "tally": faction.tally,
        "entry": faction.entry,
        "cup": tally.label if tally is not None else faction.tally,
        "points": points,
        "turn": faction.turn,
        "assets": [
            _asset_view(faction, asset)
            for asset in sorted(faction.assets.values(), key=lambda item: item.name.casefold())
        ],
    }
