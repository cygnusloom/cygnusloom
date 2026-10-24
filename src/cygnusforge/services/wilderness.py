"""The wilderness calendar, weather, Wilderness Actions, and lost rolls. They log their results and never move the clock."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session, select

from cygnusforge.content.library import DiceTable, Wilderness
from cygnusforge.db.models import Event
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CampMade,
    LostRolled,
    SupplyGathered,
    WeatherRolled,
    WildernessEventRolled,
    load_state,
    record,
)

WEATHER_KIND = WeatherRolled.kind
MAX_BOUNTY_SIDES = 12


def _table(tables: Mapping[str, DiceTable], table_id: str) -> DiceTable:
    if table_id not in tables:
        raise InvalidInput(f"this rule set has no dice table named {table_id!r}")
    return tables[table_id]


def start_season_for(wilderness: Wilderness, requested: str | None) -> str:
    """The season a campaign starts in: the world's own, else the rule set's."""
    season = requested or wilderness.start_season
    if season not in wilderness.seasons:
        raise InvalidInput(f"there is no season named {season!r}")
    return season


def season_for_day(wilderness: Wilderness, start_season: str, day: int) -> str:
    """The season of a day, where day 1 is the first day of `start_season`."""
    if start_season not in wilderness.seasons:
        raise InvalidInput(f"there is no season named {start_season!r}")
    start = wilderness.seasons.index(start_season)
    index = (start + (day - 1) // wilderness.season_days) % len(wilderness.seasons)
    return wilderness.seasons[index]


def _stored_weather(db: Session, campaign_id: int) -> dict[str, Any]:
    """The weather result of the newest WeatherRolled event in the log."""
    row = db.exec(
        select(Event)
        .where(Event.campaign_id == campaign_id, Event.kind == WEATHER_KIND)
        .order_by(Event.seq.desc())
        .limit(1)
    ).one()
    payload = json.loads(row.payload)
    return {
        "day": payload["day"],
        "season": payload["season"],
        "roll": payload["roll"],
        "weather": payload["weather"],
        "catastrophe": payload["catastrophe"],
    }


def _roll_weather(
    db: Session,
    campaign_id: int,
    wilderness: Wilderness,
    tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str | None,
    day: int,
    season: str,
) -> dict[str, Any]:
    """Roll the day's weather without committing. A day that already has weather returns it."""
    state = load_state(db, campaign_id)
    if state.weather_day == day:
        return _stored_weather(db, campaign_id)

    table = _table(tables, wilderness.weather_tables[season])
    roll = roll_total(table.dice, rng, plus=plus)
    weather = table.row_for(roll).result

    extreme = weather == wilderness.catastrophe_weather
    catastrophe = extreme and state.weather_streak + 1 >= wilderness.catastrophe_streak
    if catastrophe:
        weather = wilderness.catastrophe_result

    event = WeatherRolled(
        day=day,
        season=season,
        roll=roll,
        weather=weather,
        catastrophe=catastrophe,
        extreme=extreme,
    )
    record(db, campaign_id, event)
    return {
        "day": day,
        "season": season,
        "roll": roll,
        "weather": weather,
        "catastrophe": catastrophe,
    }


def roll_weather(
    db: Session,
    campaign_id: int,
    wilderness: Wilderness,
    tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str | None,
    day: int,
    season: str,
) -> dict[str, Any]:
    """Roll the weather for a day once. Later calls for the same day return the same result."""
    result = _roll_weather(
        db, campaign_id, wilderness, tables, rng, plus=plus, day=day, season=season
    )
    db.commit()
    return result


def wilderness_action(
    db: Session,
    campaign_id: int,
    action: str,
    reason: str,
    wilderness: Wilderness,
    tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str | None,
    day: int,
    season: str,
    party_size: int,
    night: bool,
) -> dict[str, Any]:
    """Take a Wilderness Action for one watch: the day's weather, then the events roll."""
    if action not in wilderness.actions:
        names = ", ".join(sorted(wilderness.actions))
        raise InvalidInput(f"there is no wilderness action named {action!r}; use one of {names}")
    chosen = wilderness.actions[action]

    weather = _roll_weather(
        db, campaign_id, wilderness, tables, rng, plus=plus, day=day, season=season
    )

    events_table = _table(tables, wilderness.events_table)
    # Only travel at night rolls the events twice; resting or scouting at night rolls once.
    rolls = wilderness.night_event_rolls if night and chosen.effect == "travel" else 1
    events: list[dict[str, Any]] = []
    for _ in range(rolls):
        roll = roll_total(events_table.dice, rng, plus=plus)
        row = events_table.row_for(roll)
        record(
            db,
            campaign_id,
            WildernessEventRolled(
                action=action,
                roll=roll,
                event=row.result,
                text=row.text,
                night=night,
                reason=reason,
            ),
        )
        events.append({"roll": roll, "event": row.result, "text": row.text})

    rations_found: int | None = None
    if chosen.effect == "supply":
        sides = min(MAX_BOUNTY_SIDES, 2 + 2 * party_size)
        rations_found = rng.randint(1, sides)
        record(
            db,
            campaign_id,
            SupplyGathered(
                rations=rations_found, sides=sides, party_size=party_size, reason=reason
            ),
        )
    elif chosen.effect == "make_camp":
        record(db, campaign_id, CampMade(party_size=party_size, reason=reason))

    db.commit()
    return {
        "action": action,
        "name": chosen.name,
        "text": chosen.text,
        "night": night,
        "reason": reason,
        "weather": weather,
        "events": events,
        "rations_found": rations_found,
    }


def roll_lost(
    db: Session,
    campaign_id: int,
    path: str,
    wilderness: Wilderness,
    rng: random.Random,
    *,
    plus: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Roll 1d6 to see whether the party gets lost on a path. A lost value of 0 never loses it."""
    if path not in wilderness.lost_on:
        raise InvalidInput(f"there is no path named {path!r}")
    value = wilderness.lost_on[path]
    if value == 0:
        roll: int | None = None
        lost = False
    else:
        roll = rng.randint(1, 6)
        lost = roll <= value
    record(db, campaign_id, LostRolled(path=path, roll=roll, lost=lost, reason=reason))
    db.commit()
    return {"path": path, "roll": roll, "lost": lost}
