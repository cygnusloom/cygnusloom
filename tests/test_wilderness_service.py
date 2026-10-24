from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from cygnusforge.content.library import DiceRow, DiceTable, Wilderness, WildernessAction
from cygnusforge.db.models import Event
from cygnusforge.services import wilderness
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CampMade,
    LostRolled,
    SupplyGathered,
    WeatherRolled,
    WildernessEventRolled,
    load_state,
)
from cygnusforge.services.wilderness import (
    roll_lost,
    roll_weather,
    season_for_day,
    start_season_for,
    wilderness_action,
)
from tests.test_campaigns import _campaign_id
from tests.test_character_creation import _cairn_campaign
from tests.test_combat import CAIRN, _tools
from tests.test_world import _db

SEASONS = ("spring", "summer", "fall", "winter")


def _wilderness(**overrides) -> Wilderness:
    values = {
        "seasons": SEASONS,
        "start_season": "spring",
        "season_days": 90,
        "night_event_rolls": 2,
        "catastrophe_streak": 2,
        "catastrophe_weather": "Extreme",
        "catastrophe_result": "Catastrophic",
        "lost_on": {"roads": 0, "trails": 2, "wilderness": 3},
        "actions": {
            "travel": WildernessAction(id="travel", name="Travel", text="Go on.", effect="travel"),
            "explore": WildernessAction(id="explore", name="Explore", text="Look around."),
            "supply": WildernessAction(
                id="supply", name="Supply", text="Hunt and forage.", effect="supply"
            ),
            "make_camp": WildernessAction(
                id="make_camp", name="Make Camp", text="Rest.", effect="make_camp"
            ),
        },
        "events_table": "wilderness_events",
        "weather_tables": {season: "weather" for season in SEASONS},
    }
    values.update(overrides)
    return Wilderness(**values)


def _rows(*results: str) -> tuple[DiceRow, ...]:
    return tuple(
        DiceRow(low=i + 1, high=i + 1, result=r, text=r.lower()) for i, r in enumerate(results)
    )


TABLES = {
    "weather": DiceTable(
        id="weather",
        name="Weather",
        dice="1d6",
        rows=_rows("Nice", "Fair", "Unpleasant", "Inclement", "Extreme", "Extreme"),
    ),
    "wilderness_events": DiceTable(
        id="wilderness_events",
        name="Wilderness Events",
        dice="1d6",
        rows=_rows("Sign", "Sign", "Sign", "Sign", "Sign", "Quiet"),
    ),
}


class StubRng:
    """Returns a fixed value from randint and records the die sides it was asked for."""

    def __init__(self, value: int):
        self.value = value
        self.sides: list[int] = []

    def randint(self, low: int, high: int) -> int:
        self.sides.append(high)
        return self.value


def _kinds(app: FastAPI, campaign_id: int) -> list[str]:
    with _db(app) as db:
        rows = db.exec(select(Event).where(Event.campaign_id == campaign_id)).all()
        return [row.kind for row in rows]


def _event_payloads(app: FastAPI, campaign_id: int, kind: str) -> list[dict]:
    with _db(app) as db:
        rows = db.exec(select(Event).where(Event.campaign_id == campaign_id)).all()
        return [json.loads(row.payload) for row in rows if row.kind == kind]


def _fix_roll(monkeypatch: pytest.MonkeyPatch, *rolls: int) -> None:
    """Make roll_total return the given rolls in order, the last one repeating."""
    queue = list(rolls)

    def fake(formula, rng=None, *, plus=None):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(wilderness, "roll_total", fake)


def test_day_one_is_the_start_season():
    assert season_for_day(_wilderness(), "spring", 1) == "spring"


def test_day_ninety_is_still_the_first_season():
    assert season_for_day(_wilderness(), "spring", 90) == "spring"


def test_day_ninety_one_starts_the_next_season():
    assert season_for_day(_wilderness(), "spring", 91) == "summer"


def test_the_seasons_wrap_after_four_seasons():
    assert season_for_day(_wilderness(), "spring", 4 * 90) == "winter"
    assert season_for_day(_wilderness(), "spring", 4 * 90 + 1) == "spring"


def test_the_calendar_starts_from_any_season():
    assert season_for_day(_wilderness(), "fall", 1) == "fall"
    assert season_for_day(_wilderness(), "fall", 91) == "winter"


def test_an_unknown_start_season_is_rejected():
    with pytest.raises(InvalidInput):
        season_for_day(_wilderness(), "monsoon", 1)


def test_rolling_the_same_day_twice_logs_one_weather_event(
    client_factory: Callable[[], TestClient], app: FastAPI, monkeypatch: pytest.MonkeyPatch
):
    _, campaign_id = _campaign_id(client_factory)
    _fix_roll(monkeypatch, 3)
    with _db(app) as db:
        first = roll_weather(
            db, campaign_id, _wilderness(), TABLES, StubRng(1), plus=None, day=1, season="spring"
        )
        second = roll_weather(
            db, campaign_id, _wilderness(), TABLES, StubRng(1), plus=None, day=1, season="spring"
        )
    assert (
        first
        == second
        == {
            "day": 1,
            "season": "spring",
            "roll": 3,
            "weather": "Unpleasant",
            "catastrophe": False,
        }
    )
    assert _kinds(app, campaign_id).count(WeatherRolled.kind) == 1


def test_two_extremes_in_a_row_make_a_catastrophe(
    client_factory: Callable[[], TestClient], app: FastAPI, monkeypatch: pytest.MonkeyPatch
):
    _, campaign_id = _campaign_id(client_factory)
    _fix_roll(monkeypatch, 5)
    with _db(app) as db:
        one = roll_weather(
            db, campaign_id, _wilderness(), TABLES, StubRng(1), plus=None, day=1, season="spring"
        )
        two = roll_weather(
            db, campaign_id, _wilderness(), TABLES, StubRng(1), plus=None, day=2, season="spring"
        )
    assert one["weather"] == "Extreme" and one["catastrophe"] is False
    assert two["weather"] == "Catastrophic" and two["catastrophe"] is True
    with _db(app) as db:
        assert load_state(db, campaign_id).weather_streak == 2


def test_a_rolled_extreme_after_a_catastrophe_is_catastrophic_again(
    client_factory: Callable[[], TestClient], app: FastAPI, monkeypatch: pytest.MonkeyPatch
):
    _, campaign_id = _campaign_id(client_factory)
    results = []
    for day, roll in enumerate([5, 5, 5, 1, 5], start=1):
        _fix_roll(monkeypatch, roll)
        with _db(app) as db:
            results.append(
                roll_weather(
                    db,
                    campaign_id,
                    _wilderness(),
                    TABLES,
                    StubRng(1),
                    plus=None,
                    day=day,
                    season="spring",
                )["weather"]
            )
    assert results == ["Extreme", "Catastrophic", "Catastrophic", "Nice", "Extreme"]


def test_an_unknown_wilderness_action_is_rejected(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db, pytest.raises(InvalidInput, match="use one of explore, make_camp"):
        wilderness_action(
            db,
            campaign_id,
            "dance",
            "",
            _wilderness(),
            TABLES,
            StubRng(1),
            plus=None,
            day=1,
            season="spring",
            party_size=1,
            night=False,
        )


def test_night_travel_rolls_the_events_twice(
    client_factory: Callable[[], TestClient], app: FastAPI, monkeypatch: pytest.MonkeyPatch
):
    _, campaign_id = _campaign_id(client_factory)
    _fix_roll(monkeypatch, 2)
    with _db(app) as db:
        result = wilderness_action(
            db,
            campaign_id,
            "travel",
            "Pushing on to the ford.",
            _wilderness(),
            TABLES,
            StubRng(1),
            plus=None,
            day=1,
            season="spring",
            party_size=1,
            night=True,
        )
    assert len(result["events"]) == 2
    assert result["events"][0] == {"roll": 2, "event": "Sign", "text": "sign"}
    assert result["rations_found"] is None
    assert result["weather"]["weather"] == "Fair"
    assert _kinds(app, campaign_id).count("wilderness.event_rolled") == 2


@pytest.mark.parametrize(("party_size", "sides"), [(1, 4), (2, 6), (5, 12)])
def test_the_bounty_die_grows_with_the_party(
    party_size: int,
    sides: int,
    client_factory: Callable[[], TestClient],
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
):
    _, campaign_id = _campaign_id(client_factory)
    _fix_roll(monkeypatch, 1)
    rng = StubRng(3)
    with _db(app) as db:
        result = wilderness_action(
            db,
            campaign_id,
            "supply",
            "Hunting.",
            _wilderness(),
            TABLES,
            rng,
            plus=None,
            day=1,
            season="spring",
            party_size=party_size,
            night=False,
        )
    assert rng.sides == [sides]
    assert result["rations_found"] == 3
    payloads = _event_payloads(app, campaign_id, "wilderness.supply_gathered")
    assert payloads[-1]["sides"] == sides


def test_a_lost_value_of_zero_never_loses_the_party(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        result = roll_lost(db, campaign_id, "roads", _wilderness(), StubRng(1), plus=None)
    assert result == {"path": "roads", "roll": None, "lost": False}


def test_a_trails_lost_value_of_two_loses_on_two_and_not_three(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        lost = roll_lost(db, campaign_id, "trails", _wilderness(), StubRng(2), plus=None)
        safe = roll_lost(db, campaign_id, "trails", _wilderness(), StubRng(3), plus=None)
    assert lost == {"path": "trails", "roll": 2, "lost": True}
    assert safe == {"path": "trails", "roll": 3, "lost": False}
    assert _kinds(app, campaign_id).count(LostRolled.kind) == 2


def test_an_unknown_lost_path_is_rejected(client_factory: Callable[[], TestClient], app: FastAPI):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db, pytest.raises(InvalidInput, match="there is no path named 'sea'"):
        roll_lost(db, campaign_id, "sea", _wilderness(), StubRng(1), plus=None)


def test_a_requested_start_season_overrides_the_rule_sets():
    assert start_season_for(_wilderness(), None) == "spring"
    assert start_season_for(_wilderness(), "winter") == "winter"


def test_an_unknown_requested_start_season_is_rejected():
    with pytest.raises(InvalidInput):
        start_season_for(_wilderness(), "monsoon")


def test_a_new_campaign_starts_in_the_rule_sets_season(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _cairn_campaign(client_factory)
    with _db(app) as db:
        assert load_state(db, campaign_id).start_season == CAIRN.wilderness.start_season


def _live_wilderness_tools(app: FastAPI, campaign_id: int, seed: int = 0):
    return replace(
        _tools(app, campaign_id, random.Random(seed)),
        wilderness=CAIRN.wilderness,
        dice_tables=CAIRN.dice_tables,
    )


def test_a_wilderness_action_spends_one_watch(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _cairn_campaign(client_factory)
    tools = _live_wilderness_tools(app, campaign_id)
    with _db(app) as db:
        before = load_state(db, campaign_id).clock.part
    result = tools.wilderness_action("explore", "scouting the road")
    assert result["night"] is False
    with _db(app) as db:
        assert load_state(db, campaign_id).clock.part == before + 1
    kinds = _kinds(app, campaign_id)
    assert kinds.count(WeatherRolled.kind) == 1
    assert kinds.count(WildernessEventRolled.kind) == 1


def test_the_last_part_of_the_day_is_night_and_rolls_events_twice(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _cairn_campaign(client_factory)
    tools = _live_wilderness_tools(app, campaign_id)
    last = len(CAIRN.clock.parts) - 1
    with _db(app) as db:
        state = load_state(db, campaign_id)
    tools.advance_time(parts=last - state.clock.part)
    result = tools.wilderness_action("travel", "a night march")
    assert result["night"] is True
    assert len(result["events"]) == 2
    assert _kinds(app, campaign_id).count(WildernessEventRolled.kind) == 2


def test_a_night_watch_that_is_not_travel_rolls_the_events_once(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _cairn_campaign(client_factory)
    tools = _live_wilderness_tools(app, campaign_id)
    last = len(CAIRN.clock.parts) - 1
    with _db(app) as db:
        state = load_state(db, campaign_id)
    tools.advance_time(parts=last - state.clock.part)
    result = tools.wilderness_action("explore", "scouting by lantern")
    assert result["night"] is True
    assert len(result["events"]) == 1
    assert _kinds(app, campaign_id).count(WildernessEventRolled.kind) == 1


def test_supply_logs_the_rations_found_and_make_camp_logs_the_camp(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    tools = _live_wilderness_tools(app, campaign_id)
    supplied = tools.wilderness_action("supply", "foraging")
    camped = tools.wilderness_action("make_camp", "a night's rest")
    kinds = _kinds(app, campaign_id)
    assert SupplyGathered.kind in kinds
    assert CampMade.kind in kinds
    assert 1 <= supplied["rations_found"] <= 12
    assert camped["rations_found"] is None
