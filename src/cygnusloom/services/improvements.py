"""Shelter improvements: projects against a site, with defense, security, comfort, and storage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import ImprovementDefinition
from cygnusloom.services.crafting import _consume  # shared input spending
from cygnusloom.services.events import (
    ImprovementAdded,
    ImprovementAdvanced,
    ImprovementRemoved,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.state import SiteImprovement, alias_key


def _definition(name: str, definitions: Mapping[str, ImprovementDefinition]) -> ImprovementDefinition:
    definition = definitions.get(name)
    if definition is not None:
        return definition
    wanted = alias_key(name)
    for candidate in definitions.values():
        if alias_key(candidate.id) == wanted or alias_key(candidate.name) == wanted:
            return candidate
    raise ValueError(f"there is no improvement named {name!r}")


def _find(state: Any, name: str) -> SiteImprovement | None:
    key = alias_key(name)
    found = state.improvements.get(key)
    if found is not None:
        return found
    return next((i for i in state.improvements.values() if alias_key(i.name) == key), None)


def improvement_view(improvement: SiteImprovement) -> dict[str, Any]:
    return {
        "id": improvement.id,
        "site": improvement.site,
        "improvement": improvement.improvement,
        "name": improvement.name,
        "progress": improvement.progress,
        "target": improvement.target,
        "completed": improvement.completed,
        "condition": improvement.condition,
        "notes": list(improvement.notes),
    }


def improvement_views(state: Any, site: str = "") -> list[dict[str, Any]]:
    wanted = alias_key(site)
    return [
        improvement_view(improvement)
        for improvement in sorted(state.improvements.values(), key=lambda i: (i.site, i.name))
        if not wanted or alias_key(improvement.site) == wanted
    ]


def improve_site(
    db: Session,
    campaign_id: int,
    site: str,
    improvement_name: str,
    character_name: str,
    *,
    total: int,
    spec: CharacterSpec,
    definitions: Mapping[str, ImprovementDefinition],
) -> dict[str, Any]:
    definition = _definition(improvement_name, definitions)
    character = load_state(db, campaign_id).characters[name_key(character_name)]
    state = load_state(db, campaign_id)
    for required in definition.requires:
        found = next(
            (
                i
                for i in state.improvements.values()
                if alias_key(i.site) == alias_key(site)
                and alias_key(i.improvement) == alias_key(required)
                and i.completed
            ),
            None,
        )
        if found is None:
            raise ValueError(f"{definition.name} needs {required!r} at {site} first")
    existing = next(
        (
            i
            for i in state.improvements.values()
            if alias_key(i.site) == alias_key(site)
            and alias_key(i.improvement) == alias_key(definition.id)
        ),
        None,
    )
    if existing is not None and existing.completed:
        raise ValueError(f"{definition.name} is already finished at {site}")
    if existing is None:
        for entry in definition.cost:
            from cygnusloom.services.crafting import _held

            if _held(character, spec, str(entry["item"])) is None:
                raise ValueError(
                    f"{character.name} lacks {entry['item']!r} for {definition.name}"
                )
        for entry in definition.cost:
            _consume(
                db,
                campaign_id,
                character.name,
                spec,
                str(entry["item"]),
                int(entry.get("uses", 1)),
                f"used in {definition.name}",
            )
        target = max(1, definition.hours // 4)
        record(
            db,
            campaign_id,
            ImprovementAdded(
                id=alias_key(f"{site}-{definition.id}"),
                site=site,
                improvement=definition.id,
                name=definition.name,
                target=target,
            ),
        )
        existing = load_state(db, campaign_id).improvements[
            alias_key(f"{site}-{definition.id}")
        ]
    success = total >= definition.difficulty
    progress = existing.progress + (1 if success else 0)
    completed = progress >= existing.target
    record(
        db,
        campaign_id,
        ImprovementAdvanced(
            id=existing.id,
            name=existing.name,
            progress=progress,
            completed=completed,
            note=f"{character.name} works on it" if success else "work stalls",
        ),
    )
    db.commit()
    return improvement_view(load_state(db, campaign_id).improvements[existing.id])


def damage_base(
    db: Session,
    campaign_id: int,
    site: str,
    amount: int,
    *,
    cause: str = "",
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    found = [
        improvement
        for improvement in state.improvements.values()
        if alias_key(improvement.site) == alias_key(site)
    ]
    for improvement in found:
        record(
            db,
            campaign_id,
            ImprovementAdvanced(
                id=improvement.id,
                name=improvement.name,
                condition=max(0, improvement.condition - amount),
                note=cause or f"takes {amount} damage",
            ),
        )
    db.commit()
    return {"site": site, "damaged": len(found)}


def repair_base(
    db: Session,
    campaign_id: int,
    site: str,
    amount: int,
    *,
    cause: str = "",
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    found = [
        improvement
        for improvement in state.improvements.values()
        if alias_key(improvement.site) == alias_key(site)
    ]
    for improvement in found:
        record(
            db,
            campaign_id,
            ImprovementAdvanced(
                id=improvement.id,
                name=improvement.name,
                condition=min(6, improvement.condition + amount),
                note=cause or f"repaired by {amount}",
            ),
        )
    db.commit()
    return {"site": site, "repaired": len(found)}


def remove_improvement(
    db: Session, campaign_id: int, name: str, *, reason: str = ""
) -> bool:
    state = load_state(db, campaign_id)
    improvement = _find(state, name)
    if improvement is None:
        return False
    record(
        db,
        campaign_id,
        ImprovementRemoved(id=improvement.id, name=improvement.name, reason=reason),
    )
    db.commit()
    return True
