"""Timed effects on a character: a burning torch, a poison, a spell's duration, or anything else
with a clock. The engine stores when each timer ends so the player and GM can see it, and ends
expired timers when the campaign clock passes them."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from cygnusloom.content.library import ClockRules
from cygnusloom.services.clock import absolute, clock_at
from cygnusloom.services.common import InvalidInput, bounded_text, whole_number
from cygnusloom.services.events import TimerEnded, TimerStarted, load_state, name_key, record

MAX_TIMER_ITEM = 120
MAX_TIMER_LABEL = 120
MAX_TIMER_HOURS = 24 * 365


def start_timer(
    db: Session,
    campaign_id: int,
    clock_rules: ClockRules,
    name: str,
    item: str,
    *,
    hours: int,
    label: str = "",
    key: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Start or refresh a timer on a character. The timer ends after `hours` on the campaign
    clock. `key` defaults to the item's name key; a different item on the same key needs
    `force`, which is the story's override of an existing timer."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    text = bounded_text(item, "item", MAX_TIMER_ITEM)
    timer_key = key.strip() or name_key(text)
    existing = character.timers.get(timer_key)
    if existing is not None and existing.item.casefold() != text.casefold() and not force:
        raise InvalidInput(
            f"{character.name} already has {existing.item} ({existing.label or 'timer'}) running; "
            "end it first or pass force true to replace it"
        )
    duration = whole_number(hours, "hours", 1, MAX_TIMER_HOURS)
    parts = len(clock_rules.parts)
    per_part = clock_rules.hours_per_part
    start = absolute(state.clock, parts, per_part)
    end = clock_at(start + duration, parts, per_part)
    until = f"Day {end.day}, {clock_rules.parts[end.part].title()}"
    if per_part > 1:
        until += f", hour {end.hour + 1} of {per_part}"
    if (
        existing is not None
        and existing.item.casefold() == text.casefold()
        and (existing.until_day, existing.until_part, existing.until_hour)
        == (end.day, end.part, end.hour)
        and not force
    ):
        # A retried or redundant call for the same item and ending is one timer, not a refresh:
        # keep the first label and log line.
        return {
            "character": character.name,
            "item": existing.item,
            "key": timer_key,
            "hours": duration,
            "until": existing.until_text,
            "already_running": True,
            "note": f"{existing.item} ({existing.label or 'timer'}) is already running.",
        }
    record(
        db,
        campaign_id,
        TimerStarted(
            name=character.name,
            key=timer_key,
            item=text,
            label=bounded_text(label, "label", MAX_TIMER_LABEL) if label else "",
            until_day=end.day,
            until_part=end.part,
            until_hour=end.hour,
            until_text=until,
        ),
    )
    return {
        "character": character.name,
        "item": text,
        "key": timer_key,
        "hours": duration,
        "until": until,
    }


def end_timer(
    db: Session, campaign_id: int, name: str, item: str, *, force: bool = False
) -> dict[str, Any]:
    """End one timer by its item name or key. `force` leaves a note when no such timer exists."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    wanted = item.strip()
    key = next(
        (
            candidate
            for candidate, timer in character.timers.items()
            if candidate == name_key(wanted)
            or timer.item.casefold() == wanted.casefold()
            or timer.label.casefold() == wanted.casefold()
        ),
        "",
    )
    if not key:
        if force:
            return {"character": character.name, "ended": False, "note": f"no timer {wanted!r}"}
        raise InvalidInput(f"{character.name} has no timer named {wanted!r}")
    timer = character.timers[key]
    record(
        db,
        campaign_id,
        TimerEnded(name=character.name, key=key, item=timer.item, label=timer.label),
    )
    return {"character": character.name, "ended": True, "item": timer.item}


def expire_timers(
    db: Session,
    campaign_id: int,
    clock: Any,
) -> list[dict[str, str]]:
    """End every timer whose clock time the campaign clock has reached. Returns what ended."""
    ended: list[dict[str, str]] = []
    for character in load_state(db, campaign_id).characters.values():
        for key, timer in list(character.timers.items()):
            if (clock.day, clock.part, clock.hour) < (
                timer.until_day,
                timer.until_part,
                timer.until_hour,
            ):
                continue
            record(
                db,
                campaign_id,
                TimerEnded(
                    name=character.name,
                    key=key,
                    item=timer.item,
                    label=timer.label,
                    expired=True,
                ),
            )
            ended.append({"character": character.name, "item": timer.item, "label": timer.label})
    return ended
