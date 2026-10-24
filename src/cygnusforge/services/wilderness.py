"""The wilderness calendar, weather, Wilderness Actions, and lost rolls. They log their results and never move the clock."""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from cygnusforge.content.library import DiceTable, Wilderness, WildernessEffect
from cygnusforge.db.models import Event
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CampaignState,
    CampFedSet,
    Character,
    Item,
    ItemRemoved,
    ItemUsesSet,
    LostRolled,
    WeatherRolled,
    WildernessEffectApplied,
    WildernessEventRolled,
    load_state,
    name_key,
    record,
)
from cygnusforge.services.inventory import clear_items

WEATHER_KIND = WeatherRolled.kind

# Gives one copy of a catalogue item to a named character. The caller owns the transaction.
GiveItem = Callable[[Session, str, str], None]


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


@dataclass
class _Watch:
    """The state of one Wilderness Action while its hooks run. Effects append their results here."""

    db: Session
    campaign_id: int
    action: str
    reason: str
    night: bool
    # The hour the watch begins, in hours since day 1. A camp meal is marked with it.
    at: int
    party: tuple[str, ...]
    wilderness: Wilderness
    tables: Mapping[str, DiceTable]
    rng: random.Random
    plus: str | None
    give: GiveItem
    events: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)

    def roll_events(self, times: int) -> None:
        table = _table(self.tables, self.wilderness.events_table)
        for _ in range(times):
            roll = roll_total(table.dice, self.rng, plus=self.plus)
            row = table.row_for(roll)
            record(
                self.db,
                self.campaign_id,
                WildernessEventRolled(
                    action=self.action,
                    roll=roll,
                    event=row.result,
                    text=row.text,
                    night=self.night,
                    reason=self.reason,
                ),
            )
            self.events.append({"roll": roll, "event": row.result, "text": row.text})

    def run(self, effects: Iterable[WildernessEffect]) -> None:
        for effect in effects:
            text = self._apply(effect)
            record(
                self.db,
                self.campaign_id,
                WildernessEffectApplied(
                    action=self.action, do=effect.do, text=text, reason=self.reason
                ),
            )
            self.effects.append({"do": effect.do, "text": text})

    def _apply(self, effect: WildernessEffect) -> str:
        if effect.do == "roll_events":
            before = len(self.events)
            self.roll_events(effect.times)
            results = ", ".join(e["event"] for e in self.events[before:])
            return f"Extra Wilderness Events roll: {results}."
        if effect.do == "consume":
            return self._consume(effect)
        if effect.do == "grant":
            return self._grant(effect)
        if effect.do == "clear":
            return self._clear(effect)
        raise InvalidInput(f"a wilderness hook cannot do {effect.do!r}")

    def _consume(self, effect: WildernessEffect) -> str:
        item = effect.item or ""
        state = load_state(self.db, self.campaign_id)
        if effect.per == "party":
            spender = next((n for n in self.party if _held(state, n, item)), None)
            if spender is None:
                return f"No {item} in the party."
            _spend_use(self.db, self.campaign_id, state, spender, item)
            return f"One {item} used by {spender}."
        used = [n for n in self.party if _spend_use(self.db, self.campaign_id, state, n, item)]
        short = [n for n in self.party if n not in used]
        if effect.meal:
            for name in used:
                record(self.db, self.campaign_id, CampFedSet(name=name, at=self.at))
        text = f"One {item} used by {_join(used)}." if used else f"No {item} used."
        if short:
            text += f" No {item} for {_join(short)}."
        return text

    def _grant(self, effect: WildernessEffect) -> str:
        item = effect.item or ""
        if not self.party:
            return f"No one in the party to receive {item}."
        count = effect.count
        if count is None:
            raise InvalidInput(f"a grant of {item!r} needs a count")
        sides = min(count.max, count.base + count.per_member * len(self.party))
        found = self.rng.randint(1, sides)
        payer = self.party[0]
        for _ in range(found):
            self.give(self.db, payer, item)
        return f"{payer} finds {found} {item} (d{sides})."

    def _clear(self, effect: WildernessEffect) -> str:
        item = effect.item or ""
        state = load_state(self.db, self.campaign_id)
        had = [n for n in self.party if _has_kind(state, n, item)]
        clear_items(self.db, self.campaign_id, self.party, [item])
        if not had:
            return f"No {item} to clear."
        return f"{item.capitalize()} cleared from {_join(had)}."


def _held(state: CampaignState, name: str, item: str) -> Item | None:
    character: Character = state.characters[name_key(name)]
    return next((i for i in character.items if name_key(i.name) == name_key(item)), None)


def _has_kind(state: CampaignState, name: str, kind: str) -> bool:
    character: Character = state.characters[name_key(name)]
    return any(i.kind == kind for i in character.items)


def _spend_use(db: Session, campaign_id: int, state: CampaignState, name: str, item: str) -> bool:
    """Spend one use of an item the character holds. A charged item keeps its remaining uses, and
    an item with no uses, or its last use, is removed. Returns False when they hold no such item."""
    held = _held(state, name, item)
    if held is None:
        return False
    if held.max_uses and held.uses > 1:
        record(
            db,
            campaign_id,
            ItemUsesSet(name=name, item=held.name, uses=held.uses - 1, cause="used"),
        )
    else:
        record(db, campaign_id, ItemRemoved(name=name, item=held.name))
    return True


def _join(names: Sequence[str]) -> str:
    return ", ".join(names) if names else "nobody"


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
    at: int,
    party: Sequence[str],
    night: bool,
    give: GiveItem,
) -> dict[str, Any]:
    """Take a Wilderness Action for one watch. The day's weather rolls first, then the events
    roll once, then the hooks apply: the night hooks on a night watch, then the resolve hooks.
    Rule set hooks at the top level apply to every action, and an action's own hooks to it alone."""
    if action not in wilderness.actions:
        names = ", ".join(sorted(wilderness.actions))
        raise InvalidInput(f"there is no wilderness action named {action!r}; use one of {names}")
    chosen = wilderness.actions[action]

    weather = _roll_weather(
        db, campaign_id, wilderness, tables, rng, plus=plus, day=day, season=season
    )

    watch = _Watch(
        db=db,
        campaign_id=campaign_id,
        action=action,
        reason=reason,
        night=night,
        at=at,
        party=tuple(party),
        wilderness=wilderness,
        tables=tables,
        rng=rng,
        plus=plus,
        give=give,
    )
    watch.roll_events(1)
    if night:
        watch.run(wilderness.hooks.night + chosen.hooks.night)
    watch.run(wilderness.hooks.resolve + chosen.hooks.resolve)

    db.commit()
    return {
        "action": action,
        "name": chosen.name,
        "text": chosen.text,
        "night": night,
        "reason": reason,
        "weather": weather,
        "events": watch.events,
        "effects": watch.effects,
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
