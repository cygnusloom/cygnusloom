"""Friends, Rivals and other named links between a character and someone else. The rule set's JSON
gives each relation's limit, so this module only checks the request and records the change."""

from __future__ import annotations

from sqlmodel import Session

from cygnusforge.content.library import RelationRules
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import RelationChanged, load_state, name_key, record


def set_relation(
    db: Session,
    campaign_id: int,
    name: str,
    relation: RelationRules | None,
    other: str,
    *,
    added: bool,
) -> dict[str, object]:
    """Make `other` one of a character's relations, or end that relation when `added` is false."""
    if relation is None:
        raise InvalidInput("this rule set has no relations")
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    held = character.relations.get(relation.id, [])
    if added:
        if other in held:
            raise InvalidInput(f"{character.name} already has {other} as a {relation.label}")
        if relation.limit is not None and len(held) >= relation.limit:
            raise InvalidInput(
                f"{character.name} can have at most {relation.limit} {relation.label}"
                f"{'' if relation.limit == 1 else 's'}; {', '.join(held)} already"
            )
    elif other not in held:
        raise InvalidInput(f"{other} is not a {relation.label} of {character.name}")
    record(
        db,
        campaign_id,
        RelationChanged(
            name=character.name,
            relation=relation.id,
            label=relation.label,
            other=other,
            added=added,
        ),
    )
    db.commit()
    return {
        "character": character.name,
        "relation": relation.label,
        "other": other,
        "added": added,
        "held": [*held, other] if added else [h for h in held if h != other],
    }
