from __future__ import annotations

import pytest

from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.content.library import (
    DieGrowth,
    WeatherPenalty,
    WildernessAlternative,
    WildernessEffect,
    WildernessHooks,
    _load_dice_table,
    _load_wilderness,
)
from tests.conftest import bundled_library

WEATHER = {
    "spring": ["Nice", "Fair", "Fair", "Unpleasant", "Inclement", "Extreme"],
    "summer": ["Nice", "Nice", "Fair", "Unpleasant", "Inclement", "Extreme"],
    "fall": ["Fair", "Fair", "Unpleasant", "Inclement", "Inclement", "Extreme"],
    "winter": ["Fair", "Unpleasant", "Inclement", "Inclement", "Extreme", "Extreme"],
}
EVENTS = ["Encounter", "Sign", "Environment", "Loss", "Exhaustion", "Discovery"]


@pytest.fixture(scope="module")
def cairn():
    return bundled_library().rule_sets["cairn2e"]


@pytest.fixture(scope="module")
def wilderness(cairn):
    assert cairn.wilderness is not None
    return cairn.wilderness


@pytest.fixture(scope="module")
def dice_tables(cairn):
    return cairn.dice_tables


def test_the_real_cairn_wilderness_has_five_actions(wilderness):
    assert set(wilderness.actions) == {"travel", "explore", "supply", "make_camp", "resupply"}
    assert wilderness.actions["travel"].name == "Travel"
    assert wilderness.actions["explore"].name == "Explore"
    assert wilderness.actions["supply"].name == "Supply"
    assert wilderness.actions["make_camp"].name == "Make Camp"
    assert wilderness.actions["resupply"].name == "Resupply"
    assert all(action.text for action in wilderness.actions.values())


def test_the_real_cairn_wilderness_seasons_and_timing(wilderness):
    assert wilderness.seasons == ("spring", "summer", "fall", "winter")
    assert wilderness.start_season == "spring"
    assert wilderness.season_days == 90
    assert wilderness.catastrophe_streak == 2
    assert wilderness.catastrophe_weather == "Extreme"
    assert wilderness.catastrophe_result == "Catastrophic"


def test_the_real_cairn_actions_declare_their_hooks(wilderness):
    hooks = {action.id: action.hooks for action in wilderness.actions.values()}
    assert hooks == {
        "travel": WildernessHooks(night=(WildernessEffect(do="roll_events", times=1),)),
        "explore": WildernessHooks(),
        "supply": WildernessHooks(
            resolve=(
                WildernessEffect(
                    do="grant",
                    item="rations",
                    count=DieGrowth(base=2, per_member=2, max=12),
                ),
            )
        ),
        "make_camp": WildernessHooks(
            resolve=(
                WildernessEffect(do="consume", item="rations", per="member", track=True),
                WildernessEffect(do="clear", item="fatigue"),
            )
        ),
        "resupply": WildernessHooks(
            resolve=(WildernessEffect(do="buy", items=("Rations", "Torch", "Oil Can")),)
        ),
    }
    assert wilderness.hooks == WildernessHooks(
        night=(
            WildernessEffect(
                do="consume",
                item="torch",
                per="party",
                alternatives=(WildernessAlternative(item="Oil Can", requires="Lantern"),),
                on_shortage="panicking",
            ),
        )
    )


def test_the_real_cairn_wilderness_lost_on_values(wilderness):
    assert dict(wilderness.lost_on) == {"roads": 0, "trails": 2, "wilderness": 3}


def test_the_real_cairn_wilderness_travel_values(wilderness):
    travel = wilderness.travel
    assert travel is not None
    assert dict(travel.path) == {"roads": 0, "trails": 1, "wilderness": 2}
    assert dict(travel.distance) == {"short": 1, "medium": 2, "long": 3}
    assert list(travel.terrain.keys()) == ["easy", "tough", "perilous"]
    assert travel.vast_max == 2
    assert travel.held_back_terrain_steps == 1
    assert travel.weather["Catastrophic"].travel is False
    assert travel.weather["Inclement"].terrain_steps == 1
    assert travel.weather["Extreme"].fatigue == 1
    assert travel.weather["Extreme"].watches == 1
    assert travel.weather["Unpleasant"] == WeatherPenalty(
        choice=True, fatigue=1, watches=1, terrain_steps=0
    )
    assert travel.weather["Unpleasant"].choice is True
    assert travel.weather["Unpleasant"].watches == 1
    assert travel.weather["Inclement"] == WeatherPenalty(
        choice=True, fatigue=1, watches=1, terrain_steps=1
    )
    assert travel.weather["Extreme"] == WeatherPenalty(
        choice=False, fatigue=1, watches=1, terrain_steps=1
    )
    assert travel.weather["Catastrophic"] == WeatherPenalty(choice=False, travel=False)


def test_a_wilderness_without_travel_loads_with_travel_none():
    loaded = _load_wilderness([_definition()], _tables(), "t")
    assert loaded is not None
    assert loaded.travel is None


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


def _travel_with_resolve(*effects) -> dict:
    return {"travel": {"name": "Travel", "text": "Travel.", "hooks": {"resolve": list(effects)}}}


def test_unknown_effect_kind_raises_content_error():
    actions = _travel_with_resolve({"do": "teleport"})
    with pytest.raises(ContentError, match="'do' 'teleport' is not one of consume, grant"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_consume_without_per_raises_content_error():
    actions = _travel_with_resolve({"do": "consume", "item": "rations"})
    with pytest.raises(ContentError, match="'per' must be a non-empty string"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_a_tracked_consume_must_be_per_member_raises_content_error():
    actions = _travel_with_resolve(
        {"do": "consume", "item": "rations", "per": "party", "track": True}
    )
    with pytest.raises(ContentError, match="must be per member"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_a_track_flag_must_be_true_or_false_raises_content_error():
    actions = _travel_with_resolve(
        {"do": "consume", "item": "rations", "per": "member", "track": "yes"}
    )
    with pytest.raises(ContentError, match="'track' must be true or false"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_grant_with_max_above_twelve_raises_content_error():
    count = {"base": 2, "per_member": 2, "max": 13}
    actions = _travel_with_resolve({"do": "grant", "item": "rations", "count": count})
    with pytest.raises(ContentError, match="grant 'count' max must be at most 12"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_leftover_action_effect_key_raises_content_error():
    actions = {"supply": {"name": "Supply", "text": "Supply.", "effect": "supply"}}
    with pytest.raises(ContentError, match="has unknown keys \\['effect'\\]"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_leftover_top_level_key_raises_content_error():
    definition = _definition(night_event_rolls=2)
    with pytest.raises(ContentError, match="has unknown keys \\['night_event_rolls'\\]"):
        _load_wilderness([definition], _tables(), "t")


def test_unknown_hooks_key_raises_content_error():
    definition = _definition(hooks={"later": []})
    with pytest.raises(ContentError, match="'hooks' must be an object with only"):
        _load_wilderness([definition], _tables(), "t")


def _travel(**overrides) -> dict:
    travel = {
        "path": {"roads": 0},
        "distance": {"short": 1},
        "terrain": {"easy": 0},
        "vast_max": 2,
        "held_back_terrain_steps": 1,
        "weather": {"Inclement": {"choice": True, "terrain_steps": 1}},
    }
    travel.update(overrides)
    return travel


def test_a_valid_travel_block_loads():
    loaded = _load_wilderness([_definition(travel=_travel())], _tables(), "t")
    assert loaded is not None
    assert loaded.travel is not None
    assert loaded.travel.weather["Inclement"] == WeatherPenalty(choice=True, terrain_steps=1)


def test_unknown_top_level_travel_key_raises_content_error():
    definition = _definition(travel=_travel(speed=2))
    with pytest.raises(ContentError, match="travel has unknown keys \\['speed'\\]"):
        _load_wilderness([definition], _tables(), "t")


def test_unknown_weather_penalty_key_raises_content_error():
    weather = {"Inclement": {"choice": True, "rain": 1}}
    definition = _definition(travel=_travel(weather=weather))
    with pytest.raises(ContentError, match="has unknown keys \\['rain'\\]"):
        _load_wilderness([definition], _tables(), "t")


def test_travel_path_keys_must_match_lost_on_keys_raises_content_error():
    definition = _definition(travel=_travel(path={"roads": 0, "trails": 1}))
    with pytest.raises(ContentError, match="'path' keys must match the keys of 'lost_on'"):
        _load_wilderness([definition], _tables(), "t")


def test_negative_travel_distance_raises_content_error():
    definition = _definition(travel=_travel(distance={"short": -1}))
    with pytest.raises(ContentError, match="'distance' 'short' must be a whole number"):
        _load_wilderness([definition], _tables(), "t")


def test_boolean_vast_max_raises_content_error():
    definition = _definition(travel=_travel(vast_max=True))
    with pytest.raises(ContentError, match="'vast_max' must be a whole number"):
        _load_wilderness([definition], _tables(), "t")


def test_a_consume_may_name_alternatives_that_need_another_item(wilderness):
    night = wilderness.hooks.night[0]
    assert night.alternatives == (WildernessAlternative(item="Oil Can", requires="Lantern"),)
    assert night.on_shortage == "panicking"


def test_an_alternative_without_a_requirement_loads():
    actions = _travel_with_resolve(
        {
            "do": "consume",
            "item": "torch",
            "per": "party",
            "alternatives": [{"item": "Oil, Can"}],
        }
    )
    loaded = _load_wilderness([_definition(actions=actions)], _tables(), "t")
    assert loaded is not None
    effect = loaded.actions["travel"].hooks.resolve[0]
    assert effect.alternatives == (WildernessAlternative(item="Oil, Can", requires=None),)
    assert effect.on_shortage is None


def test_alternatives_must_be_a_list_raises_content_error():
    actions = _travel_with_resolve(
        {"do": "consume", "item": "torch", "per": "party", "alternatives": "Oil, Can"}
    )
    with pytest.raises(ContentError, match="consume 'alternatives' must be a list of objects"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_an_alternative_that_is_not_an_object_raises_content_error():
    actions = _travel_with_resolve(
        {"do": "consume", "item": "torch", "per": "party", "alternatives": ["Oil, Can"]}
    )
    with pytest.raises(ContentError, match="consume 'alternatives' 0 must be an object"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_an_alternative_with_an_unknown_key_raises_content_error():
    actions = _travel_with_resolve(
        {
            "do": "consume",
            "item": "torch",
            "per": "party",
            "alternatives": [{"item": "Oil, Can", "fuel": 1}],
        }
    )
    with pytest.raises(ContentError, match="does not take fuel"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_an_alternative_without_an_item_raises_content_error():
    actions = _travel_with_resolve(
        {
            "do": "consume",
            "item": "torch",
            "per": "party",
            "alternatives": [{"requires": "Lantern"}],
        }
    )
    with pytest.raises(ContentError, match="'item'"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_an_alternative_with_a_blank_requirement_raises_content_error():
    actions = _travel_with_resolve(
        {
            "do": "consume",
            "item": "torch",
            "per": "party",
            "alternatives": [{"item": "Oil, Can", "requires": "  "}],
        }
    )
    with pytest.raises(ContentError, match="'requires' must be non-empty text"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_a_blank_on_shortage_raises_content_error():
    actions = _travel_with_resolve(
        {"do": "consume", "item": "torch", "per": "party", "on_shortage": ""}
    )
    with pytest.raises(ContentError, match="'on_shortage' must be non-empty text"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_on_shortage_is_only_for_a_consume_raises_content_error():
    count = {"base": 2, "per_member": 2, "max": 12}
    actions = _travel_with_resolve(
        {"do": "grant", "item": "rations", "count": count, "on_shortage": "panicking"}
    )
    with pytest.raises(ContentError, match="grant does not take on_shortage"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_a_buy_effect_loads_its_items():
    actions = {
        "resupply": {
            "name": "Resupply",
            "text": "Buy.",
            "hooks": {"resolve": [{"do": "buy", "items": ["Rations", "Torch"]}]},
        }
    }
    loaded = _load_wilderness([_definition(actions=actions)], _tables(), "t")
    assert loaded is not None
    assert loaded.actions["resupply"].hooks.resolve == (
        WildernessEffect(do="buy", items=("Rations", "Torch")),
    )


@pytest.mark.parametrize("items", [None, "Rations", [], [""], ["Rations", 3]])
def test_a_buy_needs_a_non_empty_list_of_item_names(items):
    effect = {"do": "buy"} if items is None else {"do": "buy", "items": items}
    actions = {"resupply": {"name": "Resupply", "text": "Buy.", "hooks": {"resolve": [effect]}}}
    with pytest.raises(ContentError, match="buy 'items' must be"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")


def test_a_buy_takes_only_items():
    actions = {
        "resupply": {
            "name": "Resupply",
            "text": "Buy.",
            "hooks": {"resolve": [{"do": "buy", "items": ["Rations"], "count": 2}]},
        }
    }
    with pytest.raises(ContentError, match="buy does not take count"):
        _load_wilderness([_definition(actions=actions)], _tables(), "t")
