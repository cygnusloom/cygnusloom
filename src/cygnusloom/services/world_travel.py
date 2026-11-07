"""Travel: moving actors and the party overland between places."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cygnusloom.services import clock as clock_service
from cygnusloom.services import inventory as inventory_service
from cygnusloom.services import wilderness as wilderness_service
from cygnusloom.services.common import (
    InvalidInput,
    session_scope,
)
from cygnusloom.services.events import (
    PC_KIND,
    load_state,
    name_key,
)
from cygnusloom.services.hooks import HookContext, call_hook
from cygnusloom.services.views import (
    _character_named,
    _place_named,
)
from cygnusloom.services.world_support import *
from cygnusloom.services.world_support import _authored_route, _required, _travelling_party


class TravelMixin:
    def move_actor(
        self,
        name: str,
        location: str | None = None,
        travel: str | None = None,
        mode: str = IN_SETTLEMENT,
        route: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if mode not in MOVE_MODES:
            names = ", ".join(MOVE_MODES)
            raise InvalidInput(f"there is no move mode named {mode!r}; use one of {names}")
        if mode == OVERLAND and location is not None:
            with session_scope(self.engine) as db:
                actor = _character_named(load_state(db, self.campaign_id), name)
            if actor.kind == PC_KIND:
                if self.wilderness is None:
                    # A rule set with no wilderness subsystem still needs to move the party.
                    # Put them at the destination and let the GM narrate the journey, using
                    # the rule set's own travel guidance or a dedicated tool.
                    with session_scope(self.engine) as db:
                        result = move_actor(
                            db,
                            self.campaign_id,
                            name,
                            location,
                            mode=IN_SETTLEMENT,
                            clock_rules=self.clock_rules,
                            hooks=self.hooks,
                            inventory=self.inventory,
                        )
                    result["note"] = (
                        "This rule set does not track wilderness; the GM narrates the journey."
                    )
                    return result
                return self._overland_move(actor.name, location, route)
        with session_scope(self.engine) as db:
            return move_actor(
                db,
                self.campaign_id,
                name,
                location,
                travel=self.travel,
                speed=travel,
                mode=mode,
                clock_rules=self.clock_rules,
                hooks=self.hooks,
                inventory=self.inventory,
            )

    def _overland_move(
        self, name: str, location: str, route: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """An overland move takes watches. Each watch rolls for the party getting lost, takes the
        Travel Wilderness Action (weather, events, night hooks), and advances the clock one part.
        A lost watch makes the next one a recovery that makes no progress. The move places the
        character once the last progress watch is spent."""
        wilderness = _required(self.wilderness, "wilderness")
        clock_rules = _required(self.clock_rules, "time")
        inventory = _required(self.inventory, "inventory")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            actor = _character_named(state, name)
            where = _place_named(state, location)
            origin = state.places.get(name_key(actor.location)) if actor.location else None
            authored = _authored_route(state, origin, where)
            if authored is not None:
                # The world's route supplies the path, distance, terrain, and vast; only the
                # weather choice comes from the GM.
                effective_route: dict[str, Any] = {}
                if route is not None and "weather_choice" in route:
                    effective_route["weather_choice"] = route["weather_choice"]
                effective_route.update(authored)
                route = effective_route
            if route is None:
                raise InvalidInput(
                    "an overland move needs a route with path, distance, and terrain"
                )
            party = _travelling_party(state, actor)
            start = state.clock
            season = wilderness_service.season_for_day(
                wilderness, state.start_season or wilderness.start_season, start.day
            )
            hc = HookContext(db, self.campaign_id, self.hooks, self.inventory)
            held = any(call_hook(self.hooks, "held_back", hc, c.name, default="") for c in party)
            weather = wilderness_service.roll_weather(
                db,
                self.campaign_id,
                wilderness,
                self.dice_tables,
                self.rng,
                plus=self.plus,
                day=start.day,
                season=season,
            )
            plan = wilderness_service.plan_overland(wilderness, route, weather["weather"], held)
            if plan.fatigue:
                for member in party:
                    inventory_service.add_penalty(
                        db, self.campaign_id, inventory, member.name, cause="travel"
                    )
        journey: list[dict[str, Any]] = []
        progress = 0
        recovering = False
        while progress < plan.watches:
            with session_scope(self.engine) as db:
                state = load_state(db, self.campaign_id)
                clock = state.clock
                season = wilderness_service.season_for_day(
                    wilderness, state.start_season or wilderness.start_season, clock.day
                )
                names = [m.name for m in _travelling_party(state, _character_named(state, name))]
                lost = wilderness_service.roll_lost(
                    db,
                    self.campaign_id,
                    route["path"],
                    wilderness,
                    self.rng,
                    reason=f"overland travel to {where.name}",
                )
                watch = wilderness_service.wilderness_action(
                    db,
                    self.campaign_id,
                    wilderness_service.TRAVEL_ACTION,
                    f"overland travel to {where.name}",
                    wilderness,
                    self.dice_tables,
                    self.rng,
                    plus=self.plus,
                    day=clock.day,
                    season=season,
                    at=clock_service.absolute(
                        clock, len(clock_rules.parts), clock_rules.hours_per_part
                    ),
                    party=names,
                    night=clock.part == len(clock_rules.parts) - 1,
                    give=self._give_catalogue_item,
                    travel_watch=True,
                )
            with session_scope(self.engine) as db:
                clock_now = clock_service.advance_time(
                    db, self.campaign_id, clock_rules, self.hooks, self.inventory, days=0, parts=1
                )
            made_progress = not recovering
            if made_progress:
                progress += 1
            journey.append(
                {
                    "lost": lost["lost"],
                    "roll": lost["roll"],
                    "progress": made_progress,
                    "weather": watch["weather"],
                    "events": watch["events"],
                    "effects": watch["effects"],
                    "clock": clock_now,
                }
            )
            recovering = lost["lost"]
        with session_scope(self.engine) as db:
            result = move_actor(
                db,
                self.campaign_id,
                name,
                location,
                travel=self.travel,
                mode=OVERLAND,
                clock_rules=self.clock_rules,
                hooks=self.hooks,
                inventory=self.inventory,
            )
        result.update(
            {
                "watches": plan.watches,
                "terrain": plan.terrain,
                "fatigue": plan.fatigue,
                "lost_watches": sum(1 for w in journey if w["lost"]),
                "journey": journey,
            }
        )
        return result
