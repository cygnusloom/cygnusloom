"""Threats: non-character dangers such as zombies, hordes, and horrors. A threat is a campaign
object with harm capacity, armor, tags, and moves; it never takes a turn or rolls to attack."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.library import ThreatDefinition
from cygnusloom.services.events import (
    ThreatAdded,
    ThreatAdvanced,
    ThreatHarmed,
    ThreatRemoved,
    load_state,
    record,
)
from cygnusloom.services.state import Threat, alias_key


def _definition(name: str, definitions: Mapping[str, ThreatDefinition]) -> ThreatDefinition:
    definition = definitions.get(name)
    if definition is not None:
        return definition
    wanted = alias_key(name)
    for candidate in definitions.values():
        if alias_key(candidate.id) == wanted or alias_key(candidate.name) == wanted:
            return candidate
    raise ValueError(f"there is no threat definition named {name!r}")


def _find(state: Any, name: str) -> Threat | None:
    key = alias_key(name)
    threat = state.threats.get(key)
    if threat is not None:
        return threat
    return next((t for t in state.threats.values() if alias_key(t.name) == key), None)


def threat_view(threat: Threat) -> dict[str, Any]:
    return {
        "id": threat.id,
        "name": threat.name,
        "kind": threat.kind,
        "definition": threat.definition,
        "description": threat.description,
        "harm": threat.harm,
        "harm_max": threat.harm_max,
        "armor": threat.armor,
        "attack": threat.attack,
        "tags": list(threat.tags),
        "moves": list(threat.moves),
        "magnitude": threat.magnitude,
        "location": threat.location,
        "conditions": list(threat.conditions),
        "notes": list(threat.notes),
        "broken": threat.broken,
        "visible": threat.visible,
    }


def threat_views(state: Any, *, include_hidden: bool = False) -> list[dict[str, Any]]:
    return [
        threat_view(threat)
        for threat in sorted(state.threats.values(), key=lambda t: (t.name, t.id))
        if include_hidden or threat.visible
    ]


def create_threat(
    db: Session,
    campaign_id: int,
    definition_name: str,
    definitions: Mapping[str, ThreatDefinition],
    *,
    name: str = "",
    magnitude: int | None = None,
    location: str = "",
    notes: str = "",
    visible: bool = True,
) -> dict[str, Any]:
    definition = _definition(definition_name, definitions)
    display = name.strip() or definition.name
    magnitude_value = definition.magnitude if magnitude is None else magnitude
    magnitude_value = max(0, min(5, magnitude_value))
    capacity = definition.harm
    if definition.kind == "horde" and magnitude_value > 1:
        capacity += magnitude_value - 1
    record(
        db,
        campaign_id,
        ThreatAdded(
            id=alias_key(display),
            name=display,
            kind_of_threat=definition.kind,
            definition=definition.id,
            description=definition.description,
            harm=capacity,
            harm_max=capacity,
            armor=definition.armor,
            attack=definition.attack,
            tags=list(definition.tags),
            moves=list(definition.moves),
            magnitude=magnitude_value,
            location=location,
            visible=visible,
        ),
    )
    if notes:
        record(
            db,
            campaign_id,
            ThreatAdvanced(id=alias_key(display), name=display, magnitude=magnitude_value, note=notes),
        )
    db.commit()
    state = load_state(db, campaign_id)
    return threat_view(state.threats[alias_key(display)])


def harm_threat(
    db: Session,
    campaign_id: int,
    name: str,
    amount: int,
    *,
    cause: str = "",
    armor: int | None = None,
    piercing: bool = False,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    threat = _find(state, name)
    if threat is None:
        raise ValueError(f"there is no threat named {name!r}")
    used_armor = threat.armor if armor is None else armor
    if piercing:
        used_armor = 0
    taken = max(0, amount - used_armor)
    after = max(0, threat.harm - taken)
    record(
        db,
        campaign_id,
        ThreatHarmed(
            id=threat.id,
            name=threat.name,
            amount=taken,
            harm=after,
            harm_max=threat.harm_max,
            cause=cause,
            broken=after <= 0,
        ),
    )
    db.commit()
    return {
        "threat": threat_view(load_state(db, campaign_id).threats[threat.id]),
        "amount": amount,
        "armor": used_armor,
        "taken": taken,
        "harm": after,
        "broken": after <= 0,
    }


def advance_threat(
    db: Session,
    campaign_id: int,
    name: str,
    *,
    magnitude: int | None = None,
    note: str = "",
    location: str | None = None,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    threat = _find(state, name)
    if threat is None:
        raise ValueError(f"there is no threat named {name!r}")
    record(
        db,
        campaign_id,
        ThreatAdvanced(
            id=threat.id,
            name=threat.name,
            magnitude=threat.magnitude if magnitude is None else max(0, min(5, magnitude)),
            note=note,
            location=location,
        ),
    )
    db.commit()
    return threat_view(load_state(db, campaign_id).threats[threat.id])


def remove_threat(db: Session, campaign_id: int, name: str, *, reason: str = "") -> bool:
    state = load_state(db, campaign_id)
    threat = _find(state, name)
    if threat is None:
        return False
    record(db, campaign_id, ThreatRemoved(id=threat.id, name=threat.name, reason=reason))
    db.commit()
    return True


def split_horde(
    db: Session,
    campaign_id: int,
    name: str,
    definitions: Mapping[str, ThreatDefinition],
    *,
    zombie: str = "walker",
    count: int = 3,
    reason: str = "",
) -> dict[str, Any]:
    """Break a horde into individual zombies and lower or remove the horde."""
    state = load_state(db, campaign_id)
    horde = _find(state, name)
    if horde is None:
        raise ValueError(f"there is no threat named {name!r}")
    if horde.kind != "horde":
        raise ValueError(f"{horde.name} is not a horde")
    definition = _definition(zombie, definitions)
    count = max(1, min(12, count))
    created = []
    for index in range(count):
        display = f"{definition.name} {index + 1}"
        record(
            db,
            campaign_id,
            ThreatAdded(
                id=alias_key(f"{horde.id}-{display}"),
                name=display,
                kind_of_threat=definition.kind,
                definition=definition.id,
                description=definition.description,
                harm=definition.harm,
                harm_max=definition.harm,
                armor=definition.armor,
                attack=definition.attack,
                tags=list(definition.tags),
                moves=list(definition.moves),
                location=horde.location,
            ),
        )
        created.append(display)
    if horde.magnitude > 1:
        record(
            db,
            campaign_id,
            ThreatAdvanced(
                id=horde.id,
                name=horde.name,
                magnitude=horde.magnitude - 1,
                note=reason or "splits into individual zombies",
            ),
        )
    else:
        record(
            db,
            campaign_id,
            ThreatRemoved(
                id=horde.id,
                name=horde.name,
                reason=reason or "breaks into individual zombies",
            ),
        )
    db.commit()
    return {"horde": horde.name, "zombies": created}


def merge_horde(
    db: Session,
    campaign_id: int,
    names: list[str],
    definitions: Mapping[str, ThreatDefinition],
    *,
    horde: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Merge several threats into a horde."""
    state = load_state(db, campaign_id)
    found = []
    for name in names:
        threat = _find(state, name)
        if threat is not None:
            found.append(threat)
    if not found:
        raise ValueError("there are no threats to merge")
    horde_definition = (
        _definition(horde, definitions)
        if horde
        else next((d for d in definitions.values() if d.kind == "horde"), None)
    )
    if horde_definition is None:
        raise ValueError("this rule set has no horde threat definition")
    magnitude = max(1, min(5, sum(max(1, t.magnitude) for t in found)))
    display = f"{horde_definition.name} ({len(found)} merged)"
    for threat in found:
        record(db, campaign_id, ThreatRemoved(id=threat.id, name=threat.name, reason=reason or "merges"))
    record(
        db,
        campaign_id,
        ThreatAdded(
            id=alias_key(display),
            name=display,
            kind_of_threat=horde_definition.kind,
            definition=horde_definition.id,
            description=horde_definition.description,
            harm=horde_definition.harm + magnitude - 1,
            harm_max=horde_definition.harm + magnitude - 1,
            armor=horde_definition.armor,
            attack=horde_definition.attack,
            tags=list(horde_definition.tags),
            moves=list(horde_definition.moves),
            magnitude=magnitude,
            location=found[0].location,
        ),
    )
    db.commit()
    return {"horde": display, "magnitude": magnitude, "merged": [t.name for t in found]}
