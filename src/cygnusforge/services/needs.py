"""Rests and need. A rest takes time and, for the party, restores what the rule set says it
restores. A deprived character recovers nothing until the need ends."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import (
    ClockRules,
    FoodRules,
    InventoryRules,
    NeedRules,
    RestLength,
)
from cygnusforge.services.clock import absolute, advance_to, rest_target
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    PC_KIND,
    CampaignState,
    CampFedSet,
    ItemRemoved,
    ItemUsesSet,
    LodgingSet,
    NeedSet,
    RestRecovered,
    RestTaken,
    load_state,
    name_key,
    record,
)
from cygnusforge.services.inventory import clear_items

HISTORY_LIMIT = 20


def take_rest(
    db: Session,
    campaign_id: int,
    clock_rules: ClockRules,
    rests: Mapping[str, RestLength],
    interrupted_rest: str,
    need: NeedRules | None,
    inventory: InventoryRules | None,
    food: FoodRules | None,
    length: str,
    interrupted: bool,
    reason: str,
) -> dict[str, Any]:
    """The party rests. An interrupted rest counts as the rule set's interrupted length. A rest
    that eats food feeds the party first, and a member with none becomes deprived. Time passes,
    then each party member who is not deprived by then recovers what the rest restores. A full
    rest also clears the item kinds it lists."""
    chosen_id = interrupted_rest if interrupted else length
    if chosen_id not in rests:
        raise InvalidInput(f"there is no rest length named {length!r}")
    rest = rests[chosen_id]
    parts_per_day = len(clock_rules.parts)
    hours_per_part = clock_rules.hours_per_part
    state = load_state(db, campaign_id)
    start = absolute(state.clock, parts_per_day, hours_per_part)
    record(
        db,
        campaign_id,
        RestTaken(
            length=rest.id,
            full=rest.full,
            interrupted=interrupted,
            reason=reason,
            day=state.clock.day,
            part=clock_rules.parts[state.clock.part],
        ),
    )
    result: dict[str, Any] = {"length": rest.id, "interrupted": interrupted}
    if rest.food and food is not None:
        _feed_party(db, campaign_id, food, need, state, start, clock_rules.hours_per_part)
    result.update(
        advance_to(
            db,
            campaign_id,
            clock_rules,
            rest_target(start, rest, clock_rules),
            need,
            inventory,
            passed="the rest is over and time has passed",
        )
    )

    # Recovery uses the state after time has passed, so someone deprived during the rest gets none.
    state = load_state(db, campaign_id)
    party = [c for c in state.characters.values() if c.kind == PC_KIND]
    recovering = [c for c in party if c.unmet_since is None]
    result["deprived"] = [c.name for c in party if c.unmet_since is not None]
    for character in recovering:
        if any(character.harm.get(stat, 0) for stat in rest.restores):
            record(
                db, campaign_id, RestRecovered(name=character.name, restores=list(rest.restores))
            )
    if rest.full and rest.clears:
        clear_items(db, campaign_id, [c.name for c in recovering], rest.clears)
    result["recovered"] = [c.name for c in recovering]
    db.commit()
    return result


def _feed_party(
    db: Session,
    campaign_id: int,
    food: FoodRules,
    need: NeedRules | None,
    state: CampaignState,
    start: int,
    hours_per_part: int,
) -> None:
    """Each party member who is not lodged eats one use of the food. A member who has none is
    deprived, if the rule set tracks need. A lodged member's bed is used up instead. A member who
    ate a camp meal in the watch just before this rest, which started `start` hours in, is fed by
    that meal and eats nothing more."""
    day = state.clock.day
    for character in [c for c in state.characters.values() if c.kind == PC_KIND]:
        camp_fed_at = character.camp_fed_at
        if camp_fed_at is not None:
            record(db, campaign_id, CampFedSet(name=character.name, at=None))
        if character.lodged:
            record(db, campaign_id, LodgingSet(name=character.name, lodged=False))
            continue
        if camp_fed_at is not None and start - camp_fed_at == hours_per_part:
            continue
        held = next((i for i in character.items if name_key(i.name) == name_key(food.item)), None)
        if held is None:
            if need is not None and character.unmet_since is None:
                record(
                    db,
                    campaign_id,
                    NeedSet(name=character.name, deprived=True, cause="no food", day=day),
                )
            continue
        if held.max_uses and held.uses > 1:
            record(
                db,
                campaign_id,
                ItemUsesSet(name=character.name, item=held.name, uses=held.uses - 1, cause="eaten"),
            )
        else:
            record(db, campaign_id, ItemRemoved(name=character.name, item=held.name))


def rest_history(db: Session, campaign_id: int) -> dict[str, Any]:
    """The party's most recent rests, newest first, so the GM can judge need."""
    rests = load_state(db, campaign_id).rests[-HISTORY_LIMIT:]
    return {
        "rests": [
            {
                "day": rest.day,
                "part": rest.part,
                "length": rest.length,
                "full": rest.full,
                "interrupted": rest.interrupted,
                "reason": rest.reason,
            }
            for rest in reversed(rests)
        ]
    }


def set_condition(
    db: Session,
    campaign_id: int,
    rules: NeedRules | None,
    name: str,
    deprived: bool,
    cause: str,
) -> dict[str, Any]:
    if rules is None:
        raise InvalidInput("this rule set does not track need")
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if deprived and not cause.strip():
        raise InvalidInput("say what the character is deprived of")
    record(
        db,
        campaign_id,
        NeedSet(name=character.name, deprived=deprived, cause=cause.strip(), day=state.clock.day),
    )
    db.commit()
    return {
        "name": character.name,
        "deprived": deprived,
        "since_day": (character.unmet_since or state.clock.day) if deprived else None,
    }
