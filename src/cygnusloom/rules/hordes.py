"""Hordes rule hooks. A safe night clears one harm; a full day clears two.

The city-navigation tool also lives here: it is zombie-survival policy rather than a generic
engine service. It moves the party, raises the City Alert clock by the route's cost, and rolls
the street-event table so no crossing is a blank gap between scenes.
"""

from __future__ import annotations

from typing import Any

from cygnusloom.rules import core
from cygnusloom.rules.context import ToolContext
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import CounterChanged, record
from cygnusloom.services.hooks import HookContext
from cygnusloom.services.state import PC_KIND

HARM_RECOVERY = {"night": 1, "day": 2}

# What each way across a city costs. The fast roads are seen; the quiet ways take their price in
# time, dark, and whatever is already in them.
CITY_ROUTES = {
    "main-street": (2, 0, "fast, open, and watched"),
    "side-street": (1, 0, "ordinary streets with ordinary cover"),
    "alley": (0, 0, "slow, cramped, and full of things left behind"),
    "rooftops": (0, 0, "a high way over the street, with falls and gaps"),
    "through-buildings": (0, 1, "slow and loud, but out of sight between walls"),
    "storm-drain": (0, 0, "dark, wet, and narrow, below the street"),
}


def navigate_city(
    character: str,
    destination: str,
    route: str,
    ctx: ToolContext,
    reason: str = "",
) -> dict[str, Any]:
    """Move a party across the city, raise City Alert by the route, and roll a street event."""
    chosen = route.strip().lower().replace("_", "-").replace(" ", "-")
    if chosen not in CITY_ROUTES:
        raise InvalidInput(
            f"there is no city route named {route!r}; choose one of {', '.join(CITY_ROUTES)}"
        )
    alert, noise, note = CITY_ROUTES[chosen]
    moved = ctx.world.move_actor(character, location=destination)
    if str(moved.get("note", "")).startswith(f"{character}"):
        return {"moved": moved, "route": chosen, "city_alert": None, "street": None}
    when = reason.strip() or f"crossing the city by the {chosen.replace('-', ' ')}"
    result: dict[str, Any] = {
        "moved": moved,
        "route": chosen,
        "route_note": note,
    }
    if alert:
        result["city_alert"] = ctx.world.advance_clock("city_alert", alert, when)
    if noise:
        result["noise"] = ctx.world.advance_noise(noise, when)
    try:
        rolled = ctx.world.roll_tables(["street-event"])
    except InvalidInput:
        rolled = None
    if rolled is not None:
        result["street"] = rolled["rolls"][0]
    return result


async def create_site(
    name: str,
    description: str,
    site_type: str,
    ctx: ToolContext,
    parent: str | None = None,
    discovered: bool = False,
    summary: str | None = None,
    icon: str | None = None,
) -> dict[str, Any]:
    """Add a seeded, searchable place from a site type."""
    return await core.create_location(
        name,
        description,
        ctx,
        discovered=discovered,
        parent=parent,
        icon=icon,
        summary=summary,
        site_type=site_type,
    )


def after_rest(
    hc: HookContext,
    name: str,
    length: str,
    start: int,
    end: int,
    interrupted: bool,
) -> None:
    """Clear harm after a full rest. An interrupted rest clears none."""
    if interrupted:
        return
    amount = HARM_RECOVERY.get(length, 0)
    if not amount:
        return
    character = hc.character(name)
    if character.kind != PC_KIND:
        return
    before = character.counters.get("harm", 0)
    after = max(0, before - amount)
    if after == before:
        return
    db, campaign_id = hc.session()
    record(
        db,
        campaign_id,
        CounterChanged(
            name=character.name,
            counter="harm",
            label="Harm",
            before=before,
            after=after,
            cause=f"a {length} rest",
        ),
    )
