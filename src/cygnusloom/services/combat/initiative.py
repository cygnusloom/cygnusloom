"""Initiative and group breaks: who acts first and when a side scatters."""

from typing import Any

from sqlmodel import Session

from cygnusloom.content.library import (
    CombatRules,
    GroupRules,
)
from cygnusloom.services.combat.damage import _end_if_one_side_left
from cygnusloom.services.combat.save import resolve_save
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.encounters import (
    awaiting_initiative,
    initiative_open,
    settle_initiative,
)
from cygnusloom.services.events import (
    CampaignState,
    GroupBreakResolved,
    InitiativeRolled,
    current,
    defeat_status,
    load_state,
    name_key,
    record,
)


def _no_initiative_pending(state: CampaignState) -> None:
    if initiative_open(state):
        waiting = ", ".join(awaiting_initiative(state))
        if waiting:
            raise InvalidInput(f"initiative is not set yet; {waiting} must roll for it first")
        raise InvalidInput("initiative is not set yet; roll it before acting")


def resolve_group_break(
    db: Session, campaign_id: int, name: str, outcome: str, combat: CombatRules
) -> dict[str, Any]:
    """The GM's decision after a group's Critical Damage save failed. "defeat" breaks and defeats it,
    "flee" breaks it up so its members run from the fight. Until this is called, the group cannot
    attack or be attacked. The rule set's groups list the decisions a break may take."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if not character.large_group:
        raise InvalidInput(f"{character.name} is not a group")
    if not character.critical_due:
        raise InvalidInput(f"{character.name} has no failed critical damage save to resolve")
    outcomes = (combat.groups or GroupRules()).break_outcomes
    if outcome not in outcomes:
        raise InvalidInput(f"outcome must be {' or '.join(repr(o) for o in outcomes)}")
    status = defeat_status(character, combat.defeat) if outcome == "defeat" else ""
    record(
        db,
        campaign_id,
        GroupBreakResolved(name=character.name, outcome=outcome, status=status),
    )
    db.commit()
    _end_if_one_side_left(db, campaign_id)
    return {"character": character.name, "outcome": outcome}


def player_initiative(
    db: Session, campaign_id: int, roll: int, combat: CombatRules
) -> dict[str, Any]:
    """A player character's initiative save, on the rule set's initiative stat. Once every player
    character has rolled, the order of round one is set."""
    state = load_state(db, campaign_id)
    waiting = awaiting_initiative(state)
    if not waiting:
        raise InvalidInput("no one is rolling for initiative right now")
    character = state.characters[name_key(waiting[0])]
    roll = whole_number(roll, "roll", 1, combat.check_sides)
    result = resolve_save(combat, current(character, combat.initiative_stat), roll=roll)
    record(
        db,
        campaign_id,
        InitiativeRolled(
            name=character.name,
            passed=result.passed,
            roll=result.roll,
            score=result.score,
            dice=combat.check_die,
        ),
    )
    db.commit()
    settle_initiative(db, campaign_id, combat.initiative_order)
    return {
        "character": character.name,
        "roll": result.roll,
        "score": result.score,
        "passed": result.passed,
        "still_waiting": awaiting_initiative(load_state(db, campaign_id)),
    }
