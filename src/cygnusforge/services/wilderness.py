"""The wilderness calendar, weather, Wilderness Actions, and lost rolls. They log their results and never move the clock."""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from cygnusforge.content.library import (
    DiceTable,
    Hooks,
    WeatherPenalty,
    Wilderness,
    WildernessEffect,
)
from cygnusforge.db.models import Event
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CampaignState,
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
    same_item_name,
)
from cygnusforge.services.hooks import HookContext, call_hook
from cygnusforge.services.inventory import clear_items

WEATHER_KIND = WeatherRolled.kind

# Gives one copy of a catalogue item to a named character. The caller owns the transaction.
# Gives one copy of a catalogue item to a character. It returns False, and records nothing, when
# the item does not fit in the character's pack.
GiveItem = Callable[[Session, str, str], bool]


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
    # The hour the watch begins, in hours since day 1. A meal is marked with it.
    at: int
    party: tuple[str, ...]
    wilderness: Wilderness
    tables: Mapping[str, DiceTable]
    rng: random.Random
    plus: str | None
    give: GiveItem
    events: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    hooks: Hooks | None = None
    # The day the watch falls on. The after_consume hook may use it.
    day: int = 1
    buy: Callable[[Session, str, str], dict[str, Any]] | None = None
    purchases: Sequence[Mapping[str, Any]] = ()
    # Set by a consume that found nothing to spend and has an on_shortage status, for this effect only.
    shortage: str | None = None

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
            self.shortage = None
            text = self._apply(effect)
            record(
                self.db,
                self.campaign_id,
                WildernessEffectApplied(
                    action=self.action, do=effect.do, text=text, reason=self.reason
                ),
            )
            entry: dict[str, Any] = {"do": effect.do, "text": text}
            if self.shortage is not None:
                entry["status"] = self.shortage
                if self.shortage == "panicking":
                    entry["panicking"] = True
            self.effects.append(entry)

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
        if effect.do == "buy":
            return self._buy(effect)
        raise InvalidInput(f"a wilderness hook cannot do {effect.do!r}")

    def _consume(self, effect: WildernessEffect) -> str:
        item = effect.item or ""
        state = load_state(self.db, self.campaign_id)
        if effect.per == "party":
            return self._consume_party(effect, state)
        used = [n for n in self.party if _spend_use(self.db, self.campaign_id, state, n, item)]
        short = [n for n in self.party if n not in used]
        text = f"One {item} used by {_join(used)}." if used else f"No {item} used."
        if short:
            text += f" No {item} for {_join(short)}."
        if effect.track:
            hc = HookContext(self.db, self.campaign_id, self.hooks, None)
            extra = call_hook(
                self.hooks,
                "after_consume",
                hc,
                item,
                used,
                short,
                self.at,
                self.day,
                default="",
            )
            if extra:
                text += f" {extra}"
        return text

    def _consume_party(self, effect: WildernessEffect, state: CampaignState) -> str:
        """Spend one party-wide item: the first member holding it, else the first alternative the
        party can use. With nothing to spend the effect may name a shortage status."""
        item = effect.item or ""
        spender = next((n for n in self.party if _held(state, n, item)), None)
        if spender is not None:
            _spend_use(self.db, self.campaign_id, state, spender, item)
            return f"One {item} used by {spender}."
        for alt in effect.alternatives:
            if alt.requires is not None and not any(
                _held(state, n, alt.requires) is not None for n in self.party
            ):
                continue
            alt_spender = next((n for n in self.party if _held(state, n, alt.item)), None)
            if alt_spender is not None:
                _spend_use(self.db, self.campaign_id, state, alt_spender, alt.item)
                return f"One {alt.item} used by {alt_spender}."
        if effect.on_shortage:
            self.shortage = effect.on_shortage
            return f"No {item} in the party ({effect.on_shortage})."
        return f"No {item} in the party."

    def _buy(self, effect: WildernessEffect) -> str:
        """Buy the purchases a resupply names, each one of the effect's items. The whole list is
        checked before anything is bought."""
        if not self.purchases:
            raise InvalidInput("resupply needs purchases naming the basic supplies to buy")
        if self.buy is None:
            raise InvalidInput("resupply needs a marketplace to buy from")
        planned: list[tuple[str, str, int]] = []
        for entry in self.purchases:
            if not isinstance(entry, Mapping):
                raise InvalidInput("each purchase must be an object with an item")
            requested = entry.get("item")
            if not isinstance(requested, str) or not requested.strip():
                raise InvalidInput("each purchase needs a non-empty item")
            canonical = next((a for a in effect.items if same_item_name(requested, a)), None)
            if canonical is None:
                raise InvalidInput(
                    f"resupply can only buy {', '.join(effect.items)}, not {requested!r}"
                )
            buyer = entry.get("name") or (self.party[0] if self.party else "")
            if not isinstance(buyer, str) or not buyer.strip():
                raise InvalidInput("a purchase needs a buyer in the party")
            if name_key(buyer) not in {name_key(n) for n in self.party}:
                raise InvalidInput(f"{buyer!r} is not in the party and cannot buy")
            quantity = entry.get("quantity", 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise InvalidInput("a purchase quantity must be a whole number, at least 1")
            planned.append((buyer, canonical, quantity))
        bought: dict[str, dict[str, int]] = {}
        for buyer, canonical, quantity in planned:
            for _ in range(quantity):
                self.buy(self.db, buyer, canonical)
            counts = bought.setdefault(buyer, {})
            counts[canonical] = counts.get(canonical, 0) + quantity
        return " ".join(
            f"{buyer} buys " + ", ".join(f"{count} {item}" for item, count in counts.items()) + "."
            for buyer, counts in bought.items()
        )

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
        carried = 0
        for _ in range(found):
            if not self.give(self.db, payer, item):
                break
            carried += 1
        text = f"{payer} finds {found} {item} (d{sides})."
        if carried == 0:
            return f"{text} None fit in the pack, so none are kept."
        if carried < found:
            return f"{text} Only {carried} fit in the pack; the rest are left behind."
        return text

    def _clear(self, effect: WildernessEffect) -> str:
        item = effect.item or ""
        state = load_state(self.db, self.campaign_id)
        # A character held back cannot recover item slots, so their items stay.
        hc = HookContext(self.db, self.campaign_id, self.hooks, None)
        eligible = [
            n for n in self.party if not call_hook(self.hooks, "held_back", hc, n, default="")
        ]
        had = [n for n in eligible if _has_kind(state, n, item)]
        if eligible:
            clear_items(self.db, self.campaign_id, eligible, [item])
        if not had:
            return f"No {item} to clear."
        return f"{item.capitalize()} cleared from {_join(had)}."


def _held(state: CampaignState, name: str, item: str) -> Item | None:
    character: Character = state.characters[name_key(name)]
    return next((i for i in character.items if same_item_name(i.name, item)), None)


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


# The action every overland travel watch takes. A GM cannot pick it: it runs only inside move_actor.
TRAVEL_ACTION = "travel"


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
    travel_watch: bool = False,
    hooks: Hooks | None = None,
    buy: Callable[[Session, str, str], dict[str, Any]] | None = None,
    purchases: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Take a Wilderness Action for one watch. The day's weather rolls first, then the events
    roll once, then the hooks apply: the night hooks on a night watch, then the resolve hooks.
    Rule set hooks at the top level apply to every action, and an action's own hooks to it alone.
    A resupply names its purchases, and only an action with a buy hook takes them."""
    if action not in wilderness.actions:
        names = ", ".join(sorted(wilderness.actions))
        raise InvalidInput(f"there is no wilderness action named {action!r}; use one of {names}")
    if action == TRAVEL_ACTION and not travel_watch:
        raise InvalidInput("travel is made with move_actor in overland mode, with a route")
    chosen = wilderness.actions[action]
    effects = wilderness.hooks.resolve + chosen.hooks.resolve
    if night:
        effects = wilderness.hooks.night + chosen.hooks.night + effects
    has_buy = any(e.do == "buy" for e in effects)
    if has_buy and not purchases:
        raise InvalidInput(f"{action} needs purchases naming the basic supplies to buy")
    if not has_buy and purchases:
        raise InvalidInput(f"{action} does not take purchases; use resupply")

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
        hooks=hooks,
        day=day,
        buy=buy,
        purchases=tuple(purchases or ()),
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
    """Roll the rule set's lost die to see whether the party gets lost on a path. The party is lost
    when the roll is at or under the path's lost value. A lost value of 0 never loses it."""
    if path not in wilderness.lost_on:
        raise InvalidInput(f"there is no path named {path!r}")
    value = wilderness.lost_on[path]
    if value == 0:
        roll: int | None = None
        lost = False
    else:
        roll = roll_total(wilderness.lost_die, rng, plus=plus)
        lost = roll <= value
    record(db, campaign_id, LostRolled(path=path, roll=roll, lost=lost, reason=reason))
    db.commit()
    return {"path": path, "roll": roll, "lost": lost}


@dataclass(frozen=True)
class TravelPlan:
    """The watches an overland move takes, and what the journey costs the party."""

    watches: int
    terrain: str
    # Fatigue each traveller takes for the journey.
    fatigue: int


ROUTE_KEYS = ("path", "distance", "terrain", "vast", "weather_choice")
# The two effects a WeatherPenalty with `choice` can pick between: its fatigue or its watches.
WEATHER_CHOICES = ("fatigue", "watch")


def _route_name(route: Mapping[str, Any], key: str, table: Mapping[str, int]) -> str:
    value = route.get(key)
    if value is None:
        raise InvalidInput(f"an overland route needs a {key}; use one of {', '.join(table)}")
    if not isinstance(value, str) or value not in table:
        raise InvalidInput(f"there is no {key} named {value!r}; use one of {', '.join(table)}")
    return str(value)


def plan_overland(
    wilderness: Wilderness, route: Mapping[str, Any], weather: str, held_back: bool
) -> TravelPlan:
    """Work out the watches of one overland move. The weather of the day it starts sets the
    penalty for the whole journey. A traveller held back makes the ground one step harder."""
    travel = wilderness.travel
    if travel is None:
        raise InvalidInput("this rule set has no overland travel; use the in_settlement mode")
    unknown = sorted(set(route) - set(ROUTE_KEYS))
    if unknown:
        raise InvalidInput(f"an overland route has no {', '.join(unknown)}")
    path = _route_name(route, "path", travel.path)
    distance = _route_name(route, "distance", travel.distance)
    terrain = _route_name(route, "terrain", travel.terrain)
    vast = route.get("vast", 0)
    if isinstance(vast, bool) or not isinstance(vast, int) or not 0 <= vast <= travel.vast_max:
        raise InvalidInput(f"vast must be a whole number from 0 to {travel.vast_max}")

    penalty = travel.weather.get(weather, WeatherPenalty())
    if not penalty.travel:
        raise InvalidInput(f"no overland travel is possible in {weather} weather")
    choice = route.get("weather_choice")
    if penalty.choice and choice not in WEATHER_CHOICES:
        raise InvalidInput(
            f"the weather is {weather}: choose weather_choice, one of {', '.join(WEATHER_CHOICES)}"
        )
    fatigue, watches = penalty.fatigue, penalty.watches
    if penalty.choice:
        fatigue, watches = (penalty.fatigue, 0) if choice == "fatigue" else (0, penalty.watches)

    levels = tuple(travel.terrain)
    steps = penalty.terrain_steps + (travel.held_back_terrain_steps if held_back else 0)
    level = min(levels.index(terrain) + steps, len(levels) - 1)
    hardest = levels[level]
    watches += travel.path[path] + travel.distance[distance] + vast + travel.terrain[hardest]
    return TravelPlan(watches=watches, terrain=hardest, fatigue=fatigue)
