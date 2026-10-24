from __future__ import annotations

import pytest

from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import (
    DATA_DIR,
    ContentLibrary,
    _load_dice_table,
    _load_wilderness,
)

WEATHER = {
    "spring": ["Nice", "Fair", "Fair", "Unpleasant", "Inclement", "Extreme"],
    "summer": ["Nice", "Nice", "Fair", "Unpleasant", "Inclement", "Extreme"],
    "fall": ["Fair", "Fair", "Unpleasant", "Inclement", "Inclement", "Extreme"],
    "winter": ["Fair", "Unpleasant", "Inclement", "Inclement", "Extreme", "Extreme"],
}
EVENTS = ["Encounter", "Sign", "Environment", "Loss", "Exhaustion", "Discovery"]


@pytest.fixture(scope="module")
def wilderness():
    rule_set = ContentLibrary.load(DATA_DIR).rule_sets["cairn2e"]
    assert rule_set.wilderness is not None
    return rule_set.wilderness


@pytest.fixture(scope="module")
def dice_tables():
    rule_set = ContentLibrary.load(DATA_DIR).rule_sets["cairn2e"]
    return rule_set.dice_tables


def test_the_real_cairn_wilderness_has_four_actions(wilderness):
    assert set(wilderness.actions) == {"travel", "explore", "supply", "make_camp"}
    assert wilderness.actions["travel"].name == "Travel"
    assert wilderness.actions["explore"].name == "Explore"
    assert wilderness.actions["supply"].name == "Supply"
    assert wilderness.actions["make_camp"].name == "Make Camp"
    assert all(action.text for action in wilderness.actions.values())


def test_the_real_cairn_wilderness_seasons_and_timing(wilderness):
    assert wilderness.seasons == ("spring", "summer", "fall", "winter")
    assert wilderness.start_season == "spring"
    assert wilderness.season_days == 90
    assert wilderness.night_event_rolls == 2
    assert wilderness.catastrophe_streak == 2
    assert wilderness.catastrophe_weather == "Extreme"
    assert wilderness.catastrophe_result == "Catastrophic"


def test_the_real_cairn_actions_declare_their_effects(wilderness):
    effects = {action.id: action.effect for action in wilderness.actions.values()}
    assert effects == {
        "travel": None,
        "explore": None,
        "supply": "supply",
        "make_camp": "make_camp",
    }


def test_the_real_cairn_wilderness_lost_on_values(wilderness):
    assert dict(wilderness.lost_on) == {"roads": 0, "trails": 2, "wilderness": 3}


def test_the_real_cairn_events_table_has_the_six_events(wilderness, dice_tables):
    assert wilderness.events_table == "wilderness_events"
    table = dice_tables["wilderness_events"]
    assert len(table.rows) == 6
    assert [row.result for row in table.rows] == EVENTS
    assert [(row.low, row.high) for row in table.rows] == [(n, n) for n in range(1, 7)]


@pytest.mark.parametrize("season", ["spring", "summer", "fall", "winter"])
def test_each_season_has_its_weather_table(wilderness, dice_tables, season):
    table_id = wilderness.weather_tables[season]
    assert table_id == f"weather_{season}"
    table = dice_tables[table_id]
    assert [(row.low, row.high) for row in table.rows] == [(n, n) for n in range(1, 7)]
    assert [row.result for row in table.rows] == WEATHER[season]


def test_wilderness_weather_tables_have_one_entry_per_season(wilderness):
    assert set(wilderness.weather_tables) == set(wilderness.seasons)


def _definition(**overrides) -> Definition:
    data = {
        "type": "wilderness",
        "id": "test",
        "seasons": ["spring", "summer"],
        "start_season": "spring",
        "season_days": 90,
        "night_event_rolls": 2,
        "catastrophe_streak": 2,
        "catastrophe_weather": "Extreme",
        "catastrophe_result": "Catastrophic",
        "lost_on": {"roads": 0},
        "actions": {"travel": {"name": "Travel", "text": "Travel."}},
        "events_table": "events",
        "weather_tables": {"spring": "weather", "summer": "weather"},
    }
    data.update(overrides)
    return Definition(type="wilderness", id="test", data=data)


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
    weather = Definition(
        type="dice_table",
        id="weather",
        data={
            "type": "dice_table",
            "id": "weather",
            "name": "Weather",
            "dice": "1d6",
            "rows": [{"roll": str(n), "result": f"W{n}"} for n in range(1, 7)],
        },
    )
    return {"events": _load_dice_table(raw, "t"), "weather": _load_dice_table(weather, "t")}


def test_a_valid_hand_built_wilderness_loads():
    loaded = _load_wilderness([_definition()], _tables(), "t")
    assert loaded is not None
    assert loaded.start_season == "spring"
    assert loaded.weather_tables == {"spring": "weather", "summer": "weather"}


def test_no_wilderness_definition_gives_none():
    assert _load_wilderness([], _tables(), "t") is None


def test_start_season_not_in_seasons_raises_content_error():
    definition = _definition(start_season="winter")
    with pytest.raises(ContentError, match="'start_season' 'winter' is not one of 'seasons'"):
        _load_wilderness([definition], _tables(), "t")


def test_unknown_events_table_raises_content_error():
    with pytest.raises(ContentError, match="unknown dice table 'missing'"):
        _load_wilderness([_definition(events_table="missing")], _tables(), "t")


def test_lost_on_out_of_range_raises_content_error():
    with pytest.raises(ContentError, match="'lost_on' 'roads' must be a whole number from 0 to 6"):
        _load_wilderness([_definition(lost_on={"roads": 7})], _tables(), "t")


def test_unknown_action_effect_raises_content_error():
    actions = {"travel": {"name": "Travel", "text": "Travel.", "effect": "teleport"}}
    with pytest.raises(ContentError, match="'effect' must be one of supply, make_camp"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_repeated_action_effect_raises_content_error():
    actions = {
        "one": {"name": "One", "text": "One.", "effect": "supply"},
        "two": {"name": "Two", "text": "Two.", "effect": "supply"},
    }
    with pytest.raises(ContentError, match="only one action"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")
