"""Overland journey arithmetic and journey events, driven by a rule set's JourneyRules.

The terrain-day model computes a march from the rule set's day length, terrain speeds, road
multiplier, weather factors, and supply needs. Both journey models share one event evaluator:
an event rolls once per travel day or watch when its `when` filters match, and a chance check
uses the campaign's RNG before the event's table is rolled.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from cygnusloom.content.library import DiceTable, JourneyEvent, JourneyRules
from cygnusloom.rules.dice import roll_total
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import RollMade, TablesRolled, record


@dataclass(frozen=True)
class DayPlan:
    """What one terrain-day journey costs: its pace, distance, and supplies."""

    speed_mph: float
    miles_per_day: float
    miles: float
    hours: int
    food: int
    water: int
    firewood: int


def plan_days(
    journey: JourneyRules,
    terrain: str,
    *,
    road: bool,
    weather: str,
    days: int,
    travelers: int,
) -> DayPlan:
    """Validate a terrain-day journey and work out its pace, distance, and supplies, by the
    same arithmetic the source book uses."""
    if terrain not in journey.terrain_speeds:
        raise InvalidInput(
            f"there is no terrain {terrain!r}; use one of {sorted(journey.terrain_speeds)}"
        )
    if weather not in journey.weather_factors:
        raise InvalidInput(
            f"there is no weather {weather!r}; use one of {sorted(journey.weather_factors)}"
        )
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= journey.max_days:
        raise InvalidInput(f"days must be a whole number from 1 to {journey.max_days}")
    if (
        isinstance(travelers, bool)
        or not isinstance(travelers, int)
        or not 1 <= travelers <= journey.max_travelers
    ):
        raise InvalidInput(f"travelers must be a whole number from 1 to {journey.max_travelers}")
    speed = journey.terrain_speeds[terrain]
    if road:
        speed *= journey.road_multiplier
        if journey.road_speed_cap > 0:
            speed = min(speed, journey.road_speed_cap)
    speed *= journey.weather_factors[weather]
    miles_per_day = round(speed * journey.day_hours, 2)
    miles = round(miles_per_day * days, 2)
    return DayPlan(
        speed_mph=round(speed, 2),
        miles_per_day=miles_per_day,
        miles=miles,
        hours=journey.day_hours * days,
        food=days * travelers * journey.food_per_traveler_day,
        water=days * travelers * journey.water_per_traveler_day,
        firewood=days * journey.firewood_per_day,
    )


def roll_day_events(
    db: Session,
    campaign_id: int,
    journey: JourneyRules | None,
    tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str | None,
    terrain: str | None = None,
    weather: str | None = None,
    road: bool | None = None,
    days: int,
    reason: str = "",
) -> list[dict[str, Any]]:
    """Roll the journey's "day" events once for each travel day, in listed order."""
    events: list[dict[str, Any]] = []
    for _ in range(days):
        events.extend(
            evaluate_events(
                db,
                campaign_id,
                journey,
                "day",
                tables,
                rng,
                plus=plus,
                terrain=terrain,
                weather=weather,
                road=road,
                reason=reason,
            )
        )
    return events


def roll_watch_events(
    db: Session,
    campaign_id: int,
    journey: JourneyRules | None,
    tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str | None,
    terrain: str | None = None,
    weather: str | None = None,
    road: bool | None = None,
    path: str | None = None,
    reason: str = "",
) -> list[dict[str, Any]]:
    """Roll the journey's "watch" events once for the current watch, in listed order."""
    return evaluate_events(
        db,
        campaign_id,
        journey,
        "watch",
        tables,
        rng,
        plus=plus,
        terrain=terrain,
        weather=weather,
        road=road,
        path=path,
        reason=reason,
    )


def evaluate_events(
    db: Session,
    campaign_id: int,
    journey: JourneyRules | None,
    on: str,
    tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str | None,
    terrain: str | None = None,
    weather: str | None = None,
    road: bool | None = None,
    path: str | None = None,
    reason: str = "",
) -> list[dict[str, Any]]:
    """The one journey-event evaluator. It walks the configured events in order, skips the
    ones for another lifecycle point or whose filters do not match, rolls a configured chance
    on 1d6 first, then rolls the event's table. Both rolls are recorded in the event log: a
    chance as RollMade and a table as TablesRolled."""
    if journey is None:
        return []
    results: list[dict[str, Any]] = []
    for event in journey.events:
        if event.on != on or not _matches(
            event, terrain=terrain, weather=weather, road=road, path=path
        ):
            continue
        if event.chance is not None and not _chance_passes(
            db, campaign_id, event, rng, on=on, reason=reason
        ):
            continue
        table = tables[event.table]
        roll = roll_total(table.dice, rng, plus=plus)
        row = table.row_for(roll)
        record(
            db,
            campaign_id,
            TablesRolled(
                rolls=[
                    {
                        "table": table.id,
                        "name": table.name,
                        "roll": roll,
                        "result": row.result,
                    }
                ]
            ),
        )
        entry: dict[str, Any] = {
            "on": event.on,
            "table": table.id,
            "name": table.name,
            "roll": roll,
            "result": row.result,
        }
        if row.text:
            entry["text"] = row.text
        if event.reason:
            entry["reason"] = event.reason
        results.append(entry)
    return results


def _matches(
    event: JourneyEvent,
    *,
    terrain: str | None,
    weather: str | None,
    road: bool | None,
    path: str | None,
) -> bool:
    """Whether an event's optional filters admit the journey's current conditions."""
    return not (
        (event.terrain and terrain not in event.terrain)
        or (event.weather and weather not in event.weather)
        or (event.road and road not in event.road)
        or (event.path and path not in event.path)
    )


def _chance_passes(
    db: Session,
    campaign_id: int,
    event: JourneyEvent,
    rng: random.Random,
    *,
    on: str,
    reason: str,
) -> bool:
    """Roll the event's 1d6 chance. The event fires on a roll at or under its chance."""
    roll = rng.randint(1, 6)
    passed = roll <= (event.chance or 0)
    record(
        db,
        campaign_id,
        RollMade(
            name=f"Journey event chance ({on})",
            roll="1d6",
            total=roll,
            cause=event.reason or reason,
        ),
    )
    return passed
