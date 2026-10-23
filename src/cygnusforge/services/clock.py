"""The campaign clock. Time is counted in hours, and a rest or the Warden can advance it. A part of
the day holds several hours. Each new day gives deprived characters a need penalty, a place
not visited for a while is flagged so the Warden can update it, and a dying character dies when its
hours run out."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import ClockRules, InventoryRules, NeedRules, RestLength
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CharacterDied,
    Clock,
    ClockAdvanced,
    Item,
    load_state,
    record,
)
from cygnusforge.services.hirelings import pay_wages
from cygnusforge.services.inventory import carry_item

MAX_ADVANCE_DAYS = 365


def absolute(clock: Clock, parts_per_day: int, hours_per_part: int) -> int:
    """The clock's position as a count of hours since day 1, hour 0."""
    return ((clock.day - 1) * parts_per_day + clock.part) * hours_per_part + clock.hour


def clock_at(index: int, parts_per_day: int, hours_per_part: int) -> Clock:
    hours_per_day = parts_per_day * hours_per_part
    part, hour = divmod(index % hours_per_day, hours_per_part)
    return Clock(day=index // hours_per_day + 1, part=part, hour=hour)


def rest_target(start: int, rest: RestLength, clock: ClockRules) -> int:
    """When a rest that starts at `start` ends, in hours. A rest that runs until morning ends at the
    first morning after it begins, and always takes at least a part of a day, unless `stop_at` sends
    it to a later part of the day instead."""
    parts_per_day = len(clock.parts)
    hours_per_part = clock.hours_per_part
    day_hours = parts_per_day * hours_per_part
    if rest.until_morning:
        stop = rest.stop_at.get(clock.parts[(start // hours_per_part) % parts_per_day])
        if stop is not None:
            target = start - start % day_hours + clock.parts.index(stop) * hours_per_part
            return target if target > start else target + day_hours
    target = start + rest.hours + (rest.days * parts_per_day + rest.parts) * hours_per_part
    if rest.until_morning:
        target = max(target, start + hours_per_part)
        target += (-target) % day_hours
    return target


def advance_to(
    db: Session,
    campaign_id: int,
    clock_rules: ClockRules,
    target: int,
    need: NeedRules | None,
    inventory: InventoryRules | None,
    passed: str | None = None,
) -> dict[str, Any]:
    """Move the clock forward to `target`, which counts hours from day 1. It records the change,
    gives each newly started day's deprived characters the need penalty, reports places that have gone
    stale, and kills a dying character whose hours have run out. `passed` replaces the day count in
    the notice, for time that is not a plain count of days, such as a night's rest, and a notice is
    given for any such time that passes, even inside one day. It does not commit."""
    parts_per_day = len(clock_rules.parts)
    hours_per_part = clock_rules.hours_per_part
    before = load_state(db, campaign_id).clock
    start = absolute(before, parts_per_day, hours_per_part)
    new = clock_at(target, parts_per_day, hours_per_part)
    record(
        db,
        campaign_id,
        ClockAdvanced(
            day=new.day,
            part=new.part,
            part_name=clock_rules.parts[new.part],
            hour=new.hour,
            elapsed_hours=max(0, target - start),
        ),
    )
    result: dict[str, Any] = {
        "day": new.day,
        "part": clock_rules.parts[new.part],
        "hour": new.hour,
    }
    for character in load_state(db, campaign_id).characters.values():
        if character.dying_hours == 0 and not character.dead:
            record(
                db,
                campaign_id,
                CharacterDied(name=character.name, cause="bled out before anyone aided them"),
            )
            result.setdefault("deaths", []).append(character.name)
    if clock_rules.wage_currency:
        for _ in range(before.day + 1, new.day + 1):
            pay_wages(db, campaign_id, clock_rules.wage_currency)
    if need is not None and inventory is not None:
        for day in range(before.day + 1, new.day + 1):
            state = load_state(db, campaign_id)
            for character in state.characters.values():
                since = character.unmet_since
                if since is None or day - since <= need.after_days:
                    continue
                penalty = Item(
                    inventory.penalty_name, "", inventory.penalty_slots, kind=inventory.penalty_item
                )
                carry_item(db, campaign_id, inventory, character.name, penalty, cause="need")
    if new.day > before.day or (passed is not None and target > start):
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
    need: NeedRules | None,
    inventory: InventoryRules | None,
    days: int,
    parts: int,
    hours: int = 0,
) -> dict[str, Any]:
    parts_per_day = len(clock_rules.parts)
    if days == 0 and parts == 0 and hours == 0:
        raise InvalidInput("advance the clock by at least one hour")
    hours_per_part = clock_rules.hours_per_part
    start = absolute(load_state(db, campaign_id).clock, parts_per_day, hours_per_part)
    target = start + (days * parts_per_day + parts) * hours_per_part + hours
    if days > MAX_ADVANCE_DAYS or parts > parts_per_day * MAX_ADVANCE_DAYS:
        raise InvalidInput("advance the clock by at most a year at a time")
    if hours > parts_per_day * hours_per_part * MAX_ADVANCE_DAYS:
        raise InvalidInput("advance the clock by at most a year at a time")
    result = advance_to(db, campaign_id, clock_rules, target, need, inventory)
    db.commit()
    return result
