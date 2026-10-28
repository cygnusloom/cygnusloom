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
from cygnusforge.services import clock as clock_service
from cygnusforge.services import wilderness
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    ItemAdded,
    ItemRemoved,
    LostRolled,
    MealSet,
    WeatherRolled,
    WildernessEffectApplied,
    WildernessEventRolled,
    load_state,
    record,
)
from cygnusforge.services.market import listed_item
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
from tests.test_marketplace import _gold, _grant_gold
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
        assert load_state(db, campaign_id).weather_streak == 0


def test_a_catastrophe_ends_the_run_so_the_next_extreme_starts_a_new_one(
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
    assert results == ["Extreme", "Catastrophic", "Extreme", "Nice", "Extreme"]


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
            travel_watch=True,
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
            travel_watch=True,
        )
    assert len(result["events"]) == 1
    assert result["effects"] == []
    assert _kinds(app, campaign_id).count(WildernessEventRolled.kind) == 1


def test_a_supply_stops_when_the_pack_is_full(
    client_factory: Callable[[], TestClient],
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
):
    _, campaign_id = _campaign_id(client_factory)
    _fix_roll(monkeypatch, 1)
    rng = StubRng(3)
    given: list[tuple[str, str]] = []

    def give(db: Session, name: str, item: str) -> bool:
        if len(given) == 2:
            return False
        given.append((name, item))
        return True

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
            party=["Ada"],
            night=False,
            give=give,
        )
    assert given == [("Ada", "rations")] * 2
    assert result["effects"] == [
        {
            "do": "grant",
            "text": f"Ada finds 3 rations (d{rng.sides[0]}). "
            "Only 2 fit in the pack; the rest are left behind.",
        }
    ]


def test_a_supply_with_no_room_keeps_nothing(
    client_factory: Callable[[], TestClient],
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
):
    _, campaign_id = _campaign_id(client_factory)
    _fix_roll(monkeypatch, 1)
    rng = StubRng(3)

    def give(db: Session, name: str, item: str) -> bool:
        return False

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
            party=["Ada"],
            night=False,
            give=give,
        )
    assert result["effects"] == [
        {
            "do": "grant",
            "text": f"Ada finds 3 rations (d{rng.sides[0]}). "
            "None fit in the pack, so none are kept.",
        }
    ]


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

    def give(db: Session, name: str, item: str) -> bool:
        given.append((name, item))
        return True

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


def test_the_travel_action_is_refused_outside_overland_travel(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _cairn_campaign(client_factory)
    tools = _live_wilderness_tools(app, campaign_id)
    with pytest.raises(InvalidInput, match="move_actor in overland mode"):
        tools.wilderness_action("travel", "a night march")


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


def _pc_meal_at(app: FastAPI, campaign_id: int) -> int | None:
    with _db(app) as db:
        return load_state(db, campaign_id).characters[PC.casefold()].meal_at


def test_make_camp_marks_the_camp_meal_for_the_member(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _pc_tools(client_factory)
    assert _pc_meal_at(app, campaign_id) is None
    tools.wilderness_action("make_camp", "a night's rest")
    assert _pc_meal_at(app, campaign_id) is not None
    assert MealSet.kind in _kinds(app, campaign_id)


def test_a_rest_right_after_make_camp_eats_no_second_ration(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    uses_before = _pc_rations(app, campaign_id)[0].uses
    tools.wilderness_action("make_camp", "a night's rest")
    assert _pc_rations(app, campaign_id)[0].uses == uses_before - 1
    tools.rest("night")
    assert _pc_rations(app, campaign_id)[0].uses == uses_before - 1
    assert _pc_meal_at(app, campaign_id) is None


def test_a_stale_camp_meal_does_not_skip_a_later_rest_meal(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    uses_before = _pc_rations(app, campaign_id)[0].uses
    tools.wilderness_action("make_camp", "a night's rest")
    tools.wilderness_action("explore", "a morning look around")
    assert _pc_meal_at(app, campaign_id) is not None
    tools.rest("night")
    assert _pc_rations(app, campaign_id)[0].uses == uses_before - 2
    assert _pc_meal_at(app, campaign_id) is None


def _add_item(app: FastAPI, campaign_id: int, item: str, uses: int, max_uses: int) -> None:
    with Session(app.state.engine) as db:
        record(
            db,
            campaign_id,
            ItemAdded(
                name=PC,
                item=item,
                size="normal",
                slots=1,
                cause="a test",
                uses=uses,
                max_uses=max_uses,
            ),
        )
        db.commit()


def _remove_all(app: FastAPI, campaign_id: int, item: str) -> None:
    for _ in range(20):
        if not any(i.name == item for i in _pc_items(app, campaign_id)):
            return
        with Session(app.state.engine) as db:
            record(db, campaign_id, ItemRemoved(name=PC, item=item))
            db.commit()
    raise AssertionError(f"could not remove every {item}")


def _held_uses(app: FastAPI, campaign_id: int, item: str) -> list[int]:
    return [i.uses for i in _pc_items(app, campaign_id) if i.name == item]


def _clock_hours(app: FastAPI, campaign_id: int) -> int:
    with _db(app) as db:
        clock = load_state(db, campaign_id).clock
    return clock_service.absolute(clock, len(CAIRN.clock.parts), CAIRN.clock.hours_per_part)


def _to_the_night_watch(app: FastAPI, campaign_id: int, tools) -> None:
    with _db(app) as db:
        part = load_state(db, campaign_id).clock.part
    tools.advance_time(parts=len(CAIRN.clock.parts) - 1 - part)


def _consume_entry(result: dict) -> dict:
    return next(e for e in result["effects"] if e["do"] == "consume")


def test_a_night_watch_spends_one_use_of_a_carried_torch(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    _remove_all(app, campaign_id, "Torch")
    _remove_all(app, campaign_id, "Lantern")
    _add_item(app, campaign_id, "Torch", uses=3, max_uses=3)
    _to_the_night_watch(app, campaign_id, tools)

    result = tools.wilderness_action("explore", "scouting by torchlight")

    assert result["night"] is True
    assert _held_uses(app, campaign_id, "Torch") == [2]
    entry = _consume_entry(result)
    assert entry["text"] == "One torch used by Mara."
    assert "status" not in entry and "panicking" not in entry


def test_a_night_watch_burns_oil_when_a_party_member_has_a_lantern(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    _remove_all(app, campaign_id, "Torch")
    _remove_all(app, campaign_id, "Lantern")
    _remove_all(app, campaign_id, "Oil Can")
    _add_item(app, campaign_id, "Lantern", uses=0, max_uses=0)
    _add_item(app, campaign_id, "Oil Can", uses=6, max_uses=6)
    _to_the_night_watch(app, campaign_id, tools)

    result = tools.wilderness_action("explore", "scouting by lantern")

    assert _held_uses(app, campaign_id, "Oil Can") == [5]
    assert _held_uses(app, campaign_id, "Torch") == []
    entry = _consume_entry(result)
    assert entry["text"] == "One Oil Can used by Mara."
    assert "panicking" not in entry


def test_oil_without_a_lantern_does_not_light_the_night_watch(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    _remove_all(app, campaign_id, "Torch")
    _remove_all(app, campaign_id, "Lantern")
    _remove_all(app, campaign_id, "Oil Can")
    _add_item(app, campaign_id, "Oil Can", uses=6, max_uses=6)
    _to_the_night_watch(app, campaign_id, tools)

    result = tools.wilderness_action("explore", "scouting in the dark")

    assert _held_uses(app, campaign_id, "Oil Can") == [6]
    entry = _consume_entry(result)
    assert entry["status"] == "panicking"
    assert entry["panicking"] is True
    assert entry["text"] == "No torch in the party (panicking)."


def test_a_day_watch_does_not_consume_light(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _pc_tools(client_factory)
    _remove_all(app, campaign_id, "Torch")
    _add_item(app, campaign_id, "Torch", uses=3, max_uses=3)

    result = tools.wilderness_action("explore", "a morning look around")

    assert result["night"] is False
    assert not any(e["do"] == "consume" for e in result["effects"])
    assert _held_uses(app, campaign_id, "Torch") == [3]


def test_resupply_buys_the_purchases_and_spends_a_watch(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    _grant_gold(app, campaign_id, 100)
    gold_before = _gold(app, campaign_id)
    rations_before = len(_pc_rations(app, campaign_id))
    hours_before = _clock_hours(app, campaign_id)
    rations_price = listed_item(CAIRN.marketplace, "Rations").price
    torch = listed_item(CAIRN.marketplace, "Torch")
    oil = listed_item(CAIRN.marketplace, "Oil Can")
    assert torch is not None and oil is not None
    _remove_all(app, campaign_id, "Oil Can")

    result = tools.wilderness_action(
        "resupply",
        "a village watch",
        purchases=[{"item": "Rations", "quantity": 2}, {"item": "Torch"}, {"item": "Oil Can"}],
    )

    assert _gold(app, campaign_id) == gold_before - 2 * rations_price - torch.price - oil.price
    rations = _pc_rations(app, campaign_id)
    assert len(rations) == rations_before + 2
    assert [r.uses for r in rations[-2:]] == [listed_item(CAIRN.marketplace, "Rations").uses] * 2
    assert _held_uses(app, campaign_id, "Torch") == [torch.uses]
    assert _held_uses(app, campaign_id, "Oil Can") == [oil.uses]
    assert result["effects"][0]["do"] == "buy"
    assert result["effects"][0]["text"] == f"{PC} buys 2 Rations, 1 Torch, 1 Oil Can."
    assert _clock_hours(app, campaign_id) - hours_before == CAIRN.clock.hours_per_part


def test_resupply_without_purchases_is_refused(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _pc_tools(client_factory)
    hours_before = _clock_hours(app, campaign_id)
    with pytest.raises(InvalidInput, match="resupply needs purchases"):
        tools.wilderness_action("resupply", "a village watch")
    assert _clock_hours(app, campaign_id) == hours_before


def test_resupply_refuses_an_item_that_is_not_a_basic_supply(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    _grant_gold(app, campaign_id, 100)
    gold_before = _gold(app, campaign_id)
    hours_before = _clock_hours(app, campaign_id)
    with pytest.raises(InvalidInput, match="resupply can only buy Rations, Torch"):
        tools.wilderness_action("resupply", "a village watch", purchases=[{"item": "Chainmail"}])
    assert _gold(app, campaign_id) == gold_before
    assert _clock_hours(app, campaign_id) == hours_before


def test_purchases_on_a_non_resupply_action_are_refused(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    _grant_gold(app, campaign_id, 100)
    gold_before = _gold(app, campaign_id)
    with pytest.raises(InvalidInput, match="explore does not take purchases; use resupply"):
        tools.wilderness_action("explore", "a look around", purchases=[{"item": "Rations"}])
    assert _gold(app, campaign_id) == gold_before


def test_make_camp_with_no_rations_leaves_the_member_deprived(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    _remove_all(app, campaign_id, "Rations")
    tools.wilderness_action("make_camp", "a night's rest")
    with _db(app) as db:
        character = load_state(db, campaign_id).characters[PC.casefold()]
    assert character.unmet_since is not None


def test_make_camp_does_not_clear_fatigue_from_a_deprived_member(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _pc_tools(client_factory)
    tools.add_penalty(PC, "a long march")
    _remove_all(app, campaign_id, "Rations")
    result = tools.wilderness_action("make_camp", "a night's rest")
    assert any(item.kind == "fatigue" for item in _pc_items(app, campaign_id))
    assert result["effects"][1]["do"] == "clear"
    assert result["effects"][1]["text"] == "No fatigue to clear."
