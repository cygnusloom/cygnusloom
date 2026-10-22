"""The campaign clock. Time is counted in parts of a day, and a rest or the Warden can advance it.
Each new day gives deprived characters a Fatigue, and a place not visited for a while is flagged
so the Warden can update it."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import ClockRules, DeprivationRules, InventoryRules, RestLength
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import Clock, ClockAdvanced, Item, load_state, record
from cygnusforge.services.hirelings import pay_wages
from cygnusforge.services.inventory import FATIGUE_NAME, carry_item

MAX_ADVANCE_DAYS = 365


def absolute(clock: Clock, parts_per_day: int) -> int:
    """The clock's position as a count of parts since day 1, part 0."""
    return (clock.day - 1) * parts_per_day + clock.part


def clock_at(index: int, parts_per_day: int) -> Clock:
    return Clock(day=index // parts_per_day + 1, part=index % parts_per_day)


def rest_target(start: int, rest: RestLength, parts_per_day: int) -> int:
    """When a rest that starts at `start` ends. A rest that runs until morning ends at the first
    morning after it begins, and always takes at least a part of a day."""
    target = start + rest.days * parts_per_day + rest.parts
    if rest.until_morning:
        target = max(target, start + 1)
        target += (-target) % parts_per_day
    return target


def advance_to(
    db: Session,
    campaign_id: int,
    clock_rules: ClockRules,
    target: int,
    deprivation: DeprivationRules | None,
    inventory: InventoryRules | None,
    passed: str | None = None,
) -> dict[str, Any]:
    """Move the clock forward to `target`, which counts parts from day 1. It records the change,
    gives each newly started day's deprived characters their Fatigue, and reports places that have
    gone stale. `passed` replaces the day count in the notice, for time that is not a plain count
    of days, such as a night's rest. It does not commit."""
    parts_per_day = len(clock_rules.parts)
    before = load_state(db, campaign_id).clock
    new = clock_at(target, parts_per_day)
    record(
        db,
        campaign_id,
        ClockAdvanced(day=new.day, part=new.part, part_name=clock_rules.parts[new.part]),
    )
    result: dict[str, Any] = {"day": new.day, "part": clock_rules.parts[new.part]}
    if clock_rules.wage_currency:
        for _ in range(before.day + 1, new.day + 1):
            pay_wages(db, campaign_id, clock_rules.wage_currency)
    if deprivation is not None and inventory is not None:
        for day in range(before.day + 1, new.day + 1):
            state = load_state(db, campaign_id)
            for character in state.characters.values():
                since = character.deprived_since
                if since is None or day - since <= deprivation.after_days:
                    continue
                fatigue = Item(
                    FATIGUE_NAME, "", inventory.fatigue_slots, kind=inventory.fatigue_item
                )
                carry_item(db, campaign_id, inventory, character.name, fatigue, cause="deprivation")
    if new.day > before.day:
        elapsed = new.day - before.day
        if passed is None:
            passed = f"{elapsed} day{'s' if elapsed != 1 else ''} passed"
        notices = [f"{passed}; update anything that should be updated."]
        state = load_state(db, campaign_id)
        stale = sorted(
            place.name
            for place in state.places.values()
            if place.discovered and new.day - (place.visited_day or 1) >= clock_rules.stale_days
        )
        if stale:
            result["stale_places"] = stale
            notices.extend(
                f"{name} has not been visited in {clock_rules.stale_days} days, update it."
                for name in stale
            )
        result["notice"] = " ".join(notices)
    return result


def advance_time(
    db: Session,
    campaign_id: int,
    clock_rules: ClockRules,
    deprivation: DeprivationRules | None,
    inventory: InventoryRules | None,
    days: int,
    parts: int,
) -> dict[str, Any]:
    parts_per_day = len(clock_rules.parts)
    if days == 0 and parts == 0:
        raise InvalidInput("advance the clock by at least one part of a day")
    start = absolute(load_state(db, campaign_id).clock, parts_per_day)
    target = start + days * parts_per_day + parts
    if days > MAX_ADVANCE_DAYS or parts > parts_per_day * MAX_ADVANCE_DAYS:
        raise InvalidInput("advance the clock by at most a year at a time")
    result = advance_to(db, campaign_id, clock_rules, target, deprivation, inventory)
    db.commit()
    return result
