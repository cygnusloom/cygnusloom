from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusforge.content.library import (
    DiceRow,
    DiceTable,
    DieGrowth,
    Wilderness,
    WildernessAction,
    WildernessEffect,
    WildernessHooks,
)
from cygnusforge.db.models import Event
from cygnusforge.services import wilderness
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CampFedSet,
    ItemRemoved,
    LostRolled,
    WeatherRolled,
    WildernessEffectApplied,
    WildernessEventRolled,
    load_state,
    record,
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
from tests.test_combat import CAIRN, PC, _tools
from tests.test_travel import _pc_campaign
from tests.test_world import _db

SEASONS = ("spring", "summer", "fall", "winter")


def _wilderness(**overrides) -> Wilderness:
    values = {
        "seasons": SEASONS,
        "start_season": "spring",
        "season_days": 90,
        "catastrophe_streak": 2,
        "catastrophe_weather": "Extreme",
        "catastrophe_result": "Catastrophic",
        "lost_on": {"roads": 0, "trails": 2, "wilderness": 3},
        "actions": {
            "travel": WildernessAction(
                id="travel",
                name="Travel",
                text="Go on.",
                hooks=WildernessHooks(night=(WildernessEffect(do="roll_events", times=1),)),
            ),
            "explore": WildernessAction(id="explore", name="Explore", text="Look around."),
            "supply": WildernessAction(
                id="supply",
                name="Supply",
                text="Hunt and forage.",
                hooks=WildernessHooks(
                    resolve=(
                        WildernessEffect(
                            do="grant",
                            item="rations",
                            count=DieGrowth(base=2, per_member=2, max=12),
                        ),
                    )
                ),
            ),
            "make_camp": WildernessAction(
                id="make_camp",
                name="Make Camp",
                text="Rest.",
                hooks=WildernessHooks(
                    resolve=(
                        WildernessEffect(do="consume", item="rations", per="member", meal=True),
                        WildernessEffect(do="clear", item="fatigue"),
                    )
                ),
            ),
        },
        "events_table": "wilderness_events",
        "weather_tables": {season: "weather" for season in SEASONS},
        "hooks": WildernessHooks(),
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


def _no_give(db: Session, name: str, item: str) -> None:
    raise AssertionError(f"nothing should be given, but {item!r} was given to {name}")


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
            at=0,
            party=[],
            night=False,
            give=_no_give,
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
            at=0,
            party=[],
            night=True,
            give=_no_give,
        )
    assert len(result["events"]) == 2
    assert result["events"][0] == {"roll": 2, "event": "Sign", "text": "sign"}
    assert result["effects"] == [
        {"do": "roll_events", "text": "Extra Wilderness Events roll: Sign."}
    ]
    assert result["weather"]["weather"] == "Fair"
    kinds = _kinds(app, campaign_id)
    assert kinds.count(WildernessEventRolled.kind) == 2
    assert kinds.count(WildernessEffectApplied.kind) == 1


def test_a_day_watch_travel_rolls_the_events_once(
    client_factory: Callable[[], TestClient], app: FastAPI, monkeypatch: pytest.MonkeyPatch
):
    _, campaign_id = _campaign_id(client_factory)
    _fix_roll(monkeypatch, 2)
    with _db(app) as db:
        result = wilderness_action(
            db,
            campaign_id,
            "travel",
            "Pushing on.",
            _wilderness(),
            TABLES,
            StubRng(1),
            plus=None,
            day=1,
            season="spring",
            at=0,
            party=[],
            night=False,
            give=_no_give,
        )
    assert len(result["events"]) == 1
    assert result["effects"] == []
    assert _kinds(app, campaign_id).count(WildernessEventRolled.kind) == 1


@pytest.mark.parametrize(
    ("party", "sides"),
    [(["Ada"], 4), (["Ada", "Bren"], 6), (["Ada", "Bren", "Cato", "Dara", "Eve"], 12)],
)
def test_the_bounty_die_grows_with_the_party(
    party: list[str],
    sides: int,
    client_factory: Callable[[], TestClient],
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
):
    _, campaign_id = _campaign_id(client_factory)
    _fix_roll(monkeypatch, 1)
    rng = StubRng(3)
    given: list[tuple[str, str]] = []

    def give(db: Session, name: str, item: str) -> None:
        given.append((name, item))

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
            at=0,
            party=party,
            night=False,
            give=give,
        )
    assert rng.sides == [sides]
    assert given == [("Ada", "rations")] * 3
    assert result["effects"] == [{"do": "grant", "text": f"Ada finds 3 rations (d{sides})."}]
    payloads = _event_payloads(app, campaign_id, WildernessEffectApplied.kind)
    assert payloads[-1]["do"] == "grant"


def test_a_grant_with_no_one_in_the_party_gives_nothing(
    client_factory: Callable[[], TestClient], app: FastAPI, monkeypatch: pytest.MonkeyPatch
):
    _, campaign_id = _campaign_id(client_factory)
    _fix_roll(monkeypatch, 1)
    with _db(app) as db:
        result = wilderness_action(
            db,
            campaign_id,
            "supply",
            "Hunting.",
            _wilderness(),
            TABLES,
            StubRng(3),
            plus=None,
            day=1,
            season="spring",
            at=0,
            party=[],
            night=False,
            give=_no_give,
        )
    assert result["effects"] == [{"do": "grant", "text": "No one in the party to receive rations."}]


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


def _pc_tools(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _pc_campaign(client_factory)
    return (
        app,
        campaign_id,
        replace(tools, wilderness=CAIRN.wilderness, dice_tables=CAIRN.dice_tables),
    )


def _pc_rations(app: FastAPI, campaign_id: int) -> list:
    with _db(app) as db:
        character = load_state(db, campaign_id).characters[PC.casefold()]
    return [item for item in character.items if item.name == "Rations"]


def _pc_items(app: FastAPI, campaign_id: int) -> list:
    with _db(app) as db:
        return list(load_state(db, campaign_id).characters[PC.casefold()].items)


def test_supply_adds_rations_to_the_party(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _pc_tools(client_factory)
    before = len(_pc_rations(app, campaign_id))
    result = tools.wilderness_action("supply", "foraging")
    gained = len(_pc_rations(app, campaign_id)) - before
    assert 1 <= gained <= 4
    assert result["effects"][0]["do"] == "grant"
    assert f"finds {gained} rations" in result["effects"][0]["text"]


def test_make_camp_eats_a_ration_and_clears_fatigue(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _pc_tools(client_factory)
    tools.add_penalty(PC, "a long march")
    uses_before = _pc_rations(app, campaign_id)[0].uses
    assert any(item.kind == "fatigue" for item in _pc_items(app, campaign_id))
    result = tools.wilderness_action("make_camp", "a night's rest")
    assert _pc_rations(app, campaign_id)[0].uses == uses_before - 1
    assert not any(item.kind == "fatigue" for item in _pc_items(app, campaign_id))
    assert [e["do"] for e in result["effects"]] == ["consume", "clear"]
    assert result["effects"][1]["text"].startswith("Fatigue cleared from")


def test_make_camp_with_no_rations_reports_the_shortage(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _pc_tools(client_factory)
    with _db(app) as db:
        record(db, campaign_id, ItemRemoved(name=PC, item="Rations"))
        db.commit()
    result = tools.wilderness_action("make_camp", "a night's rest")
    assert result["effects"][0]["text"] == f"No rations used. No rations for {PC}."
    assert _kinds(app, campaign_id).count(WildernessEffectApplied.kind) >= 2


def _pc_camp_fed_at(app: FastAPI, campaign_id: int) -> int | None:
    with _db(app) as db:
        return load_state(db, campaign_id).characters[PC.casefold()].camp_fed_at


def test_make_camp_marks_the_camp_meal_for_the_member(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _pc_tools(client_factory)
    assert _pc_camp_fed_at(app, campaign_id) is None
    tools.wilderness_action("make_camp", "a night's rest")
    assert _pc_camp_fed_at(app, campaign_id) is not None
    assert CampFedSet.kind in _kinds(app, campaign_id)


def test_a_rest_right_after_make_camp_eats_no_second_ration(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    uses_before = _pc_rations(app, campaign_id)[0].uses
    tools.wilderness_action("make_camp", "a night's rest")
    assert _pc_rations(app, campaign_id)[0].uses == uses_before - 1
    tools.rest("night")
    assert _pc_rations(app, campaign_id)[0].uses == uses_before - 1
    assert _pc_camp_fed_at(app, campaign_id) is None


def test_a_stale_camp_meal_does_not_skip_a_later_rest_meal(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    uses_before = _pc_rations(app, campaign_id)[0].uses
    tools.wilderness_action("make_camp", "a night's rest")
    tools.wilderness_action("explore", "a morning look around")
    assert _pc_camp_fed_at(app, campaign_id) is not None
    tools.rest("night")
    assert _pc_rations(app, campaign_id)[0].uses == uses_before - 2
    assert _pc_camp_fed_at(app, campaign_id) is None
