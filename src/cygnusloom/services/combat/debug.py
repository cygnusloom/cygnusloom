"""Debug tools for setting stats, currency, and invincibility."""

from typing import Any

from sqlmodel import Session

from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.events import (
    PC_KIND,
    DebugCurrencyAdded,
    DebugStatSet,
    InvincibilitySet,
    load_state,
    name_key,
    record,
)


def set_invincibility(db: Session, campaign_id: int, name: str, active: bool) -> dict[str, Any]:
    """Turn the debug invincibility buff on or off for a player character."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if character.kind != PC_KIND:
        raise InvalidInput(f"{character.name} is not a player character")
    record(db, campaign_id, InvincibilitySet(name=character.name, active=active))
    db.commit()
    return {"character": character.name, "invincible": active}


def set_debug_stat(
    db: Session, campaign_id: int, name: str, stat: str, remaining: int
) -> dict[str, Any]:
    """Set a player character's current score for one stat, for testing damage rules."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if character.kind != PC_KIND:
        raise InvalidInput(f"{character.name} is not a player character")
    if stat not in character.stats:
        raise InvalidInput(f"{character.name} has no stat {stat!r}")
    value = whole_number(remaining, "remaining", 0, character.stats[stat])
    record(db, campaign_id, DebugStatSet(name=character.name, stat=stat, remaining=value))
    db.commit()
    return {"character": character.name, "stat": stat, "remaining": value}


def add_debug_currency(
    db: Session, campaign_id: int, name: str, feature: str, amount: int
) -> dict[str, Any]:
    """Give a player character some of the rule set's currency, for testing purchases."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if character.kind != PC_KIND:
        raise InvalidInput(f"{character.name} is not a player character")
    value = whole_number(amount, "amount", 1, 10_000)
    record(db, campaign_id, DebugCurrencyAdded(name=character.name, feature=feature, amount=value))
    db.commit()
    return {"character": character.name, "feature": feature, "gained": value}
