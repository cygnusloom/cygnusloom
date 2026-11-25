"""Party vehicles: fuel, condition, cargo, and repairs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.library import VehicleDefinition
from cygnusloom.services.events import (
    VehicleAdded,
    VehicleRemoved,
    VehicleUpdated,
    load_state,
    record,
)
from cygnusloom.services.state import Vehicle, alias_key


def _definition(name: str, definitions: Mapping[str, VehicleDefinition]) -> VehicleDefinition:
    definition = definitions.get(name)
    if definition is not None:
        return definition
    wanted = alias_key(name)
    for candidate in definitions.values():
        if alias_key(candidate.id) == wanted or alias_key(candidate.name) == wanted:
            return candidate
    raise ValueError(f"there is no vehicle definition named {name!r}")


def _find(state: Any, name: str) -> Vehicle | None:
    key = alias_key(name)
    vehicle = state.vehicles.get(key)
    if vehicle is not None:
        return vehicle
    return next((v for v in state.vehicles.values() if alias_key(v.name) == key), None)


def vehicle_view(vehicle: Vehicle) -> dict[str, Any]:
    return {
        "id": vehicle.id,
        "name": vehicle.name,
        "definition": vehicle.definition,
        "description": vehicle.description,
        "speed": vehicle.speed,
        "handling": vehicle.handling,
        "armor": vehicle.armor,
        "condition": vehicle.condition,
        "condition_max": vehicle.condition_max,
        "fuel": vehicle.fuel,
        "fuel_max": vehicle.fuel_max,
        "seats": vehicle.seats,
        "cargo": vehicle.cargo,
        "noise": vehicle.noise,
        "location": vehicle.location,
        "notes": list(vehicle.notes),
    }


def vehicle_views(state: Any) -> list[dict[str, Any]]:
    return [
        vehicle_view(vehicle)
        for vehicle in sorted(state.vehicles.values(), key=lambda v: (v.name, v.id))
    ]


def create_vehicle(
    db: Session,
    campaign_id: int,
    definition_name: str,
    definitions: Mapping[str, VehicleDefinition],
    *,
    name: str = "",
    location: str = "",
    note: str = "",
) -> dict[str, Any]:
    definition = _definition(definition_name, definitions)
    display = name.strip() or definition.name
    record(
        db,
        campaign_id,
        VehicleAdded(
            id=alias_key(display),
            name=display,
            definition=definition.id,
            description=definition.description,
            speed=definition.speed,
            handling=definition.handling,
            armor=definition.armor,
            condition=definition.condition_max,
            condition_max=definition.condition_max,
            fuel=definition.fuel_max,
            fuel_max=definition.fuel_max,
            seats=definition.seats,
            cargo=definition.cargo,
            noise=definition.noise,
            location=location,
        ),
    )
    if note:
        record(
            db,
            campaign_id,
            VehicleUpdated(id=alias_key(display), name=display, note=note),
        )
    db.commit()
    return vehicle_view(load_state(db, campaign_id).vehicles[alias_key(display)])


def damage_vehicle(
    db: Session,
    campaign_id: int,
    name: str,
    amount: int,
    *,
    cause: str = "",
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    vehicle = _find(state, name)
    if vehicle is None:
        raise ValueError(f"there is no vehicle named {name!r}")
    after = max(0, vehicle.condition - max(0, amount))
    record(
        db,
        campaign_id,
        VehicleUpdated(
            id=vehicle.id,
            name=vehicle.name,
            condition=after,
            note=cause or f"takes {amount} damage",
        ),
    )
    db.commit()
    return vehicle_view(load_state(db, campaign_id).vehicles[vehicle.id])


def refuel_vehicle(
    db: Session,
    campaign_id: int,
    name: str,
    amount: int,
    *,
    cause: str = "",
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    vehicle = _find(state, name)
    if vehicle is None:
        raise ValueError(f"there is no vehicle named {name!r}")
    after = max(0, min(vehicle.fuel_max, vehicle.fuel + amount))
    record(
        db,
        campaign_id,
        VehicleUpdated(
            id=vehicle.id,
            name=vehicle.name,
            fuel=after,
            note=cause or f"refuels to {after}",
        ),
    )
    db.commit()
    return vehicle_view(load_state(db, campaign_id).vehicles[vehicle.id])


def repair_vehicle(
    db: Session,
    campaign_id: int,
    name: str,
    amount: int,
    *,
    cause: str = "",
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    vehicle = _find(state, name)
    if vehicle is None:
        raise ValueError(f"there is no vehicle named {name!r}")
    after = max(0, min(vehicle.condition_max, vehicle.condition + amount))
    record(
        db,
        campaign_id,
        VehicleUpdated(
            id=vehicle.id,
            name=vehicle.name,
            condition=after,
            note=cause or f"repairs to {after}",
        ),
    )
    db.commit()
    return vehicle_view(load_state(db, campaign_id).vehicles[vehicle.id])


def remove_vehicle(db: Session, campaign_id: int, name: str, *, reason: str = "") -> bool:
    state = load_state(db, campaign_id)
    vehicle = _find(state, name)
    if vehicle is None:
        return False
    record(db, campaign_id, VehicleRemoved(id=vehicle.id, name=vehicle.name, reason=reason))
    db.commit()
    return True
