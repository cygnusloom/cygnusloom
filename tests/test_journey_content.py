"""Loading a journey definition: models, bundled data, and journey-event validation."""

from __future__ import annotations

import pytest

from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.content.library import (
    JourneyEvent,
    _load_dice_table,
    _load_journey,
)
from tests.conftest import bundled_library


def _tables() -> dict:
    raw = Definition(
        type="dice_table",
        id="events",
        data={
            "type": "dice_table",
            "id": "events",
            "name": "Events",
            "dice": "1d6",
            "rows": [{"roll": str(n), "result": f"E{n}"} for n in range(1, 7)],
        },
    )
    return {"events": _load_dice_table(raw, "t")}


def _definition(**overrides) -> Definition:
    data = {
        "type": "journey",
        "id": "test",
        "model": "terrain_days",
        "terrain_speeds": {"plains": 3.0},
        "weather_factors": {"clear": 1.0},
        "forage_difficulty": {"plains": 9},
    }
    data.update(overrides)
    return Definition(type="journey", id="test", data=data)


def _event(**overrides) -> dict:
    event = {"on": "day", "table": "events"}
    event.update(overrides)
    return event


def test_the_bundled_wwn_journey_has_no_events() -> None:
    journey = bundled_library().rule_sets["worldswithoutnumber"].journey
    assert journey is not None
    assert journey.model == "terrain_days"
    assert journey.events == ()


def test_the_bundled_cairn_journey_is_watch_based_and_has_no_events() -> None:
    journey = bundled_library().rule_sets["cairn2e"].journey
    assert journey is not None
    assert journey.model == "watches"
    assert journey.events == ()


def test_a_day_event_loads_its_filters() -> None:
    event = _event(
        when={
            "terrain": ["plains"],
            "weather": ["clear"],
            "road": [True],
            "path": ["roads"],
            "chance": 2,
        },
        reason="an ambush",
    )

    journey = _load_journey([_definition(events=[event])], _tables(), "t")

    assert journey is not None
    assert journey.events == (
        JourneyEvent(
            on="day",
            table="events",
            reason="an ambush",
            terrain=("plains",),
            weather=("clear",),
            road=(True,),
            path=("roads",),
            chance=2,
        ),
    )


def test_a_watch_journey_loads_without_terrain_speeds() -> None:
    journey = _load_journey(
        [
            _definition(
                model="watches",
                terrain_speeds={},
                weather_factors={},
                forage_difficulty={},
                events=[_event(on="watch")],
            )
        ],
        _tables(),
        "t",
    )

    assert journey is not None
    assert journey.model == "watches"
    assert journey.events[0].on == "watch"


@pytest.mark.parametrize("on", ["bogus", "dawn"])
def test_an_unknown_event_lifecycle_point_is_rejected(on: str) -> None:
    with pytest.raises(ContentError, match="'on' must be one of day, watch"):
        _load_journey([_definition(events=[_event(on=on)])], _tables(), "t")


def test_a_day_event_on_a_watch_journey_is_rejected() -> None:
    with pytest.raises(ContentError, match="a watches journey takes 'watch' events"):
        _load_journey([_definition(model="watches", events=[_event(on="day")])], _tables(), "t")


def test_an_unknown_event_table_is_rejected() -> None:
    with pytest.raises(ContentError, match="names unknown dice table 'missing'"):
        _load_journey([_definition(events=[_event(table="missing")])], _tables(), "t")


def test_an_unknown_event_key_is_rejected() -> None:
    with pytest.raises(ContentError, match="unknown key\\(s\\) 'effect'"):
        _load_journey([_definition(events=[_event(effect="supply")])], _tables(), "t")


def test_an_unknown_when_key_is_rejected() -> None:
    with pytest.raises(ContentError, match="when: unknown key\\(s\\) 'phase'"):
        _load_journey([_definition(events=[_event(when={"phase": "night"})])], _tables(), "t")


@pytest.mark.parametrize("chance", [0, 7, True, "3"])
def test_an_out_of_range_chance_is_rejected(chance: object) -> None:
    with pytest.raises(ContentError, match="'chance' must be a whole number from 1 to 6"):
        _load_journey([_definition(events=[_event(when={"chance": chance})])], _tables(), "t")


def test_a_non_boolean_road_filter_is_rejected() -> None:
    with pytest.raises(ContentError, match="'road' must be a list of true or false"):
        _load_journey([_definition(events=[_event(when={"road": ["roads"]})])], _tables(), "t")


def test_a_non_list_events_value_is_rejected() -> None:
    with pytest.raises(ContentError, match="'events' must be a list of objects"):
        _load_journey([_definition(events={"on": "day"})], _tables(), "t")
