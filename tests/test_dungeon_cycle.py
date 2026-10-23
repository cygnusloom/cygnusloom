from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.library import CONTEXT_ARGUMENT
from cygnusforge.rules import cairn2e
from cygnusforge.rules.context import ToolContext
from cygnusforge.services import dungeon
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import events_since
from cygnusforge.services.tools import tool_specs
from cygnusforge.services.world import WorldTools
from tests.test_combat import CAIRN, _fight_client
from tests.test_world import _db, _state

EVENT_FOR_FACE = {
    1: ("Encounter", "Roll on an encounter table. Possibly hostile. (See Reactions.)"),
    2: (
        "Sign",
        "A clue, spoor, track, abandoned lair, scent, victim, etc is discovered.",
    ),
    3: (
        "Environment",
        (
            "Surroundings shift or escalate. Water rises, ceilings collapse, a ritual nears "
            "completion, etc."
        ),
    ),
    4: (
        "Loss",
        (
            "Torches are blown out, an ongoing spell fizzles, etc. The party must resolve the "
            "effect before moving on."
        ),
    ),
    5: (
        "Exhaustion",
        (
            "The party must rest (triggering another roll on this table), add a Fatigue, or "
            "consume a ration."
        ),
    ),
    6: ("Quiet", "The party is left alone (and safe) for the time being."),
}
REACTION_BANDS = {
    2: "Hostile",
    3: "Wary",
    4: "Wary",
    5: "Wary",
    6: "Curious",
    7: "Curious",
    8: "Curious",
    9: "Kind",
    10: "Kind",
    11: "Kind",
    12: "Helpful",
}


def _live_tools(client_factory: Callable[[], TestClient], seed: int = 0):
    client, campaign_id, tools = _fight_client(client_factory)
    tools = replace(tools, dice_tables=CAIRN.dice_tables, rng=random.Random(seed))
    return client.app, campaign_id, tools


def _logged_kinds(app: FastAPI, campaign_id: int) -> list[str]:
    with _db(app) as db:
        return [row.kind for row in events_since(db, campaign_id)]


@pytest.mark.parametrize("face", range(1, 7))
def test_each_d6_face_maps_to_its_dungeon_event(
    face: int, client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _, _, tools = _live_tools(client_factory)
    monkeypatch.setattr(dungeon, "roll_total", lambda formula, rng=None, plus=None: face)
    out = tools.dungeon_cycle(["loud"], "Shouting in the hall.")
    event, text = EVENT_FOR_FACE[face]
    assert out["roll"] == face
    assert out["event"] == event
    assert out["text"] == text
    assert out["triggers"] == ["loud"]
    assert out["reason"] == "Shouting in the hall."


def test_the_dungeon_events_table_has_the_six_spec_entries():
    table = CAIRN.dice_tables["dungeon_events"]
    assert table.dice == "1d6"
    assert {
        face: (table.row_for(face).result, table.row_for(face).text) for face in range(1, 7)
    } == (EVENT_FOR_FACE)


def test_encounter_note_points_to_the_right_next_step(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _, _, tools = _live_tools(client_factory)
    monkeypatch.setattr(dungeon, "roll_total", lambda formula, rng=None, plus=None: 1)
    assert "roll_tables" in tools.dungeon_cycle(["new_area"], "Door.")["note"]
    monkeypatch.setattr(dungeon, "roll_total", lambda formula, rng=None, plus=None: 5)
    result = tools.dungeon_cycle(["lingering"], "Camping.")
    assert result["note"] == ""
    assert "rest" in result["text"] and "Fatigue" in result["text"] and "ration" in result["text"]


def test_the_dungeon_cycle_does_not_move_the_clock(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    app, campaign_id, tools = _live_tools(client_factory)
    clock_before = _state(app, campaign_id).clock
    monkeypatch.setattr(dungeon, "roll_total", lambda formula, rng=None, plus=None: 3)
    tools.dungeon_cycle(["hasty", "lingering"], "Running through the ruin.")
    assert _state(app, campaign_id).clock == clock_before


def test_the_dungeon_cycle_logs_a_dungeon_event(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _live_tools(client_factory)
    before = _logged_kinds(app, campaign_id)
    tools.dungeon_cycle(["new_area"], "Entering the crypt.")
    after = _logged_kinds(app, campaign_id)
    assert after[len(before) :] == ["dungeon.event"]


def test_the_dungeon_cycle_rejects_an_unknown_trigger(client_factory: Callable[[], TestClient]):
    _, _, tools = _live_tools(client_factory)
    with pytest.raises(InvalidInput, match="unknown trigger 'bored'"):
        tools.dungeon_cycle(["bored"], "Waiting.")


def test_reaction_bands_cover_two_through_twelve_with_no_gaps():
    table = CAIRN.dice_tables["dungeon_reaction"]
    assert table.dice == "2d6"
    assert {roll: table.row_for(roll).result for roll in range(2, 13)} == REACTION_BANDS


def test_roll_reaction_returns_and_logs_the_band(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _live_tools(client_factory, seed=3)
    before = _logged_kinds(app, campaign_id)
    out = tools.roll_reaction("The guard eyes the party.")
    assert set(out) == {"roll", "band", "reason"}
    assert out["band"] == REACTION_BANDS[out["roll"]]
    assert out["reason"] == "The guard eyes the party."
    assert _logged_kinds(app, campaign_id)[len(before) :] == ["reaction.rolled"]


def test_the_tools_are_in_the_tool_list_and_wired_to_python():
    names = {"dungeon_cycle", "roll_reaction"}
    assert names <= set(CAIRN.tool_calls)
    specs = {spec["function"]["name"]: spec for spec in tool_specs(CAIRN.tool_calls) or []}
    assert names <= set(specs)
    cycle = specs["dungeon_cycle"]["function"]["parameters"]
    assert set(cycle["properties"]) == {"triggers", "reason"}
    assert cycle["properties"]["triggers"]["items"]["enum"] == [
        "new_area",
        "loud",
        "hasty",
        "lingering",
    ]
    assert set(specs["roll_reaction"]["function"]["parameters"]["properties"]) == {"reason"}
    assert CAIRN.tool_calls["dungeon_cycle"].function is cairn2e.dungeon_cycle
    assert CAIRN.tool_calls["roll_reaction"].function is cairn2e.roll_reaction


def _context(tools: WorldTools) -> ToolContext:
    async def quiet_speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        return None

    return ToolContext(
        world=tools,
        speak=quiet_speak,
        ooc=lambda narrate: None,
        rules=CAIRN.rules,
        lookup_tools={},
    )


def test_the_tool_functions_reach_the_world_through_the_context(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    app, campaign_id, tools = _live_tools(client_factory)
    monkeypatch.setattr(dungeon, "roll_total", lambda formula, rng=None, plus=None: 6)
    out = CAIRN.tool_calls["dungeon_cycle"].function(
        **{"triggers": ["loud"], "reason": "Hammering.", CONTEXT_ARGUMENT: _context(tools)}
    )
    assert out["event"] == "Quiet"
    reaction = CAIRN.tool_calls["roll_reaction"].function(
        **{"reason": "Who goes there?", CONTEXT_ARGUMENT: _context(tools)}
    )
    assert reaction["band"] == REACTION_BANDS[reaction["roll"]]
    assert _logged_kinds(app, campaign_id)[-2:] == ["dungeon.event", "reaction.rolled"]
