from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.library import EncounterGrid
from cygnusforge.llm.base import ToolRequest
from cygnusforge.services import campaigns, encounters
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import CampaignState, ChatGM, record, token_label
from cygnusforge.services.tools import ToolRun, run_tool
from tests.conftest import content_library, csrf_token
from tests.test_campaigns import _campaign_id, _place_pc, _send
from tests.test_tools import _script
from tests.test_world import TOOLS, _ctx, _db, _state

LIBRARY = content_library()
CAIRN_GRID = LIBRARY.rule_sets["cairn2e"].grid
MAP = "Mossbell Green"
TOWN = "Mossbell Hollow"


def _call(
    app: FastAPI,
    campaign_id: int,
    name: str,
    arguments: dict[str, Any],
    grid: EncounterGrid | None = None,
) -> dict[str, Any]:
    ctx = _ctx(app, campaign_id)
    if grid is not None:
        ctx = replace(ctx, world=replace(ctx.world, grid=grid))
    request = ToolRequest(id="call_1", name=name, arguments=json.dumps(arguments))
    run = asyncio.run(run_tool(TOOLS, request, None, ctx))
    assert isinstance(run, ToolRun)
    return json.loads(run.output)


def _encounter_with(
    client_factory: Callable[[], TestClient], columns: int = 20, rows: int = 20
) -> tuple[TestClient, int]:
    """A campaign with Ada placed in town, a map there, an encounter started, and Pip (an NPC)
    and Ada joined in that order."""
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    created = _call(
        app,
        campaign_id,
        "create_encounter_map",
        {
            "name": MAP,
            "location": TOWN,
            "columns": columns,
            "rows": rows,
            "description": "Open ground.",
        },
    )
    assert created["created"] == "encounter map"
    assert _call(app, campaign_id, "start_encounter", {"map": MAP})["started"] == "encounter"
    _call(
        app,
        campaign_id,
        "create_npc",
        {"name": "Pip", "location": TOWN, "description": "A scruffy courier."},
    )
    assert _call(app, campaign_id, "add_participant", {"name": "Pip"})["added"] == "Pip"
    assert _call(app, campaign_id, "add_participant", {"name": "Ada"})["added"] == "Ada"
    return client, campaign_id


def _begin(app: FastAPI, campaign_id: int, name: str) -> None:
    with _db(app) as db:
        encounters.begin_turn(db, campaign_id, name)


def _end(app: FastAPI, campaign_id: int, name: str, by_player: bool) -> dict[str, Any]:
    with _db(app) as db:
        return encounters.end_turn(db, campaign_id, name, by_player)


def _finish_gm_reply(app: FastAPI, campaign_id: int) -> None:
    """The GM's final reply, which closes the NPC turn it was playing out."""
    with _db(app) as db:
        record(db, campaign_id, ChatGM(content="The turn plays out.", model="test"))
        db.commit()


def _place(
    app: FastAPI,
    campaign_id: int,
    name: str,
    column: int,
    row: int,
    grid: EncounterGrid | None = CAIRN_GRID,
) -> dict[str, Any]:
    return _call(
        app,
        campaign_id,
        "place_participant",
        {"name": name, "column": column, "row": row},
        grid,
    )


def _encounter(app: FastAPI, campaign_id: int) -> CampaignState:
    return _state(app, campaign_id)


def test_cairn2e_has_a_grid_and_rolltest_does_not():
    assert CAIRN_GRID == EncounterGrid(cell_feet=5, move_feet=40)
    assert CAIRN_GRID.move_cells == 8
    assert LIBRARY.rule_sets["rolltest"].grid is None


def test_map_creation_is_validated(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    app = client.app
    _place_pc(client, campaign_id, TOWN)
    args = {"name": MAP, "location": TOWN, "columns": 20, "rows": 20, "description": "Open."}
    assert _call(app, campaign_id, "create_encounter_map", args)["columns"] == 20
    assert "already" in _call(app, campaign_id, "create_encounter_map", args)["error"]
    for columns, rows in ((0, 20), (61, 20), (20, 61)):
        bad = {**args, "name": f"Map {columns} {rows}", "columns": columns, "rows": rows}
        assert "error" in _call(app, campaign_id, "create_encounter_map", bad)
    nowhere = {**args, "name": "Lost Green", "location": "Nowhere"}
    assert "no location named" in _call(app, campaign_id, "create_encounter_map", nowhere)["error"]
    assert _encounter(app, campaign_id).maps.keys() == {MAP.casefold()}


def test_only_one_encounter_runs_and_participants_join_in_order(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory)
    app = client.app
    state = _encounter(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.order == ["Pip", "Ada"]
    assert (
        "already in the encounter"
        in _call(app, campaign_id, "add_participant", {"name": "Pip"})["error"]
    )
    _call(
        app,
        campaign_id,
        "create_encounter_map",
        {
            "name": "Other Green",
            "location": TOWN,
            "columns": 5,
            "rows": 5,
            "description": "Elsewhere.",
        },
    )
    refused = _call(app, campaign_id, "start_encounter", {"map": "Other Green"})
    assert "already in progress" in refused["error"]


def test_participant_off_the_encounter_location_gets_a_warning(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory)
    app = client.app
    _call(app, campaign_id, "create_location", {"name": "Far Shore", "description": "Across."})
    _call(
        app,
        campaign_id,
        "create_npc",
        {"name": "Wren", "location": "Far Shore", "description": "A ferry hand."},
    )
    added = _call(app, campaign_id, "add_participant", {"name": "Wren"})
    assert added["added"] == "Wren"
    assert "not at Mossbell Hollow" in added["warning"]
    assert _call(app, campaign_id, "remove_participant", {"name": "Wren"}) == {"removed": "Wren"}


def test_placement_is_bounded_occupied_and_movement_limited(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory)
    app = client.app
    assert "error" not in _place(app, campaign_id, "Ada", 19, 19)
    assert "can only go to columns 0 to 19" in _place(app, campaign_id, "Pip", 20, 0)["error"]
    assert "taken by Ada" in _place(app, campaign_id, "Pip", 19, 19)["error"]
    # Off-turn moves and the first placement cost nothing.
    assert _place(app, campaign_id, "Pip", 0, 0)["placed"] == "Pip"

    _begin(app, campaign_id, "Pip")
    refused = _place(app, campaign_id, "Pip", 9, 0)
    assert "can move 8 more cells this turn, and that is 9" in refused["error"]
    moved = _place(app, campaign_id, "Pip", 8, 0)
    assert moved["moves_left"] == 0
    assert "can move 0 more cells" in _place(app, campaign_id, "Pip", 9, 0)["error"]


def test_turns_follow_join_order_and_wrap_into_a_new_round(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    first_round = _encounter(app, campaign_id).encounter.round

    with pytest.raises(InvalidInput, match="it is Pip's turn next"):
        _begin(app, campaign_id, "Ada")
    _begin(app, campaign_id, "Pip")
    with pytest.raises(InvalidInput, match="the GM ends Pip's turn"):
        _end(app, campaign_id, "Pip", by_player=True)
    assert _call(app, campaign_id, "end_turn", {"name": "Pip"}) == {"ended_turn": "Pip"}
    _finish_gm_reply(app, campaign_id)

    assert _encounter(app, campaign_id).encounter.acting == "Ada"
    with pytest.raises(InvalidInput, match="the player ends Ada's turn"):
        _end(app, campaign_id, "Ada", by_player=False)
    assert _end(app, campaign_id, "Ada", by_player=True) == {"ended_turn": "Ada"}
    assert _encounter(app, campaign_id).encounter.round == first_round + 1


def test_encounter_cannot_end_mid_turn(client_factory: Callable[[], TestClient]):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _begin(app, campaign_id, "Pip")
    assert "still taking their turn" in _call(app, campaign_id, "end_encounter", {})["error"]
    _end(app, campaign_id, "Pip", by_player=False)
    _finish_gm_reply(app, campaign_id)
    assert "Ada is still taking their turn" in _call(app, campaign_id, "end_encounter", {})["error"]
    _end(app, campaign_id, "Ada", by_player=True)
    assert _call(app, campaign_id, "end_encounter", {})["ended"] == "encounter"
    assert _encounter(app, campaign_id).encounter is None


def test_guards_protect_participants_maps_and_locations(client_factory: Callable[[], TestClient]):
    client, campaign_id = _encounter_with(client_factory, columns=20, rows=20)
    app = client.app
    _place(app, campaign_id, "Ada", 19, 19, grid=None)

    assert "in the encounter" in _call(app, campaign_id, "delete_npc", {"name": "Pip"})["error"]
    assert (
        "in use by the encounter"
        in _call(app, campaign_id, "delete_encounter_map", {"name": MAP})["error"]
    )
    shrink = {"name": MAP, "columns": 10, "rows": 10, "description": "Smaller."}
    assert "error" in _call(app, campaign_id, "update_encounter_map", shrink)

    _call(app, campaign_id, "create_location", {"name": "Far Shore", "description": "Across."})
    _call(
        app,
        campaign_id,
        "create_encounter_map",
        {
            "name": "Shore Green",
            "location": "Far Shore",
            "columns": 5,
            "rows": 5,
            "description": "Pebbles.",
        },
    )
    assert (
        "encounter map"
        in _call(app, campaign_id, "delete_location", {"name": "Far Shore"})["error"]
    )


def _spy_on_fallback_close(monkeypatch: pytest.MonkeyPatch, app: FastAPI, campaign_id: int):
    """Record who was acting when the fallback that closes NPC turns ran. The name of the NPC means
    the fallback had to close it; the next participant means the GM had already ended it with
    end_turn, so the fallback did nothing."""
    seen: list[str | None] = []
    original = campaigns._close_npc_turn

    def spy(engine: Any, cid: int, name: str) -> None:
        seen.append(_state(app, cid).encounter.acting)
        original(engine, cid, name)

    monkeypatch.setattr(campaigns, "_close_npc_turn", spy)
    return seen


def _npc_turn_stream(client: TestClient, campaign_id: int, name: str) -> list[dict[str, Any]]:
    token = csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/encounter/turn/stream",
        data={"csrf_token": token, "name": name},
    )
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _encounter_post(client: TestClient, campaign_id: int, action: str, name: str):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/encounter/turn/{action}",
        data={"csrf_token": token, "name": name},
        follow_redirects=False,
    )


def test_npc_turn_is_played_out_and_the_gm_ends_it(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    fallback = _spy_on_fallback_close(monkeypatch, app, campaign_id)
    # The directive's turn is the GM's reply to the player's message; the NPC's turn is the next.
    script = _script(
        {"text": "Pip waits by the gate."},
        {
            "rounds": [[{"name": "end_turn", "arguments": {"name": "Pip"}}]],
            "text": "Pip ducks behind the crate.",
        },
    )
    assert _send(client, campaign_id, script).status_code == 303

    lines = _npc_turn_stream(client, campaign_id, "Pip")

    assert lines[-1]["type"] == "saved"
    state = _state(app, campaign_id)
    assert state.turn is None
    assert state.encounter.acting == "Ada"
    assert state.encounter.up == 1
    assert fallback == ["Ada"]


def test_npc_turn_closes_even_when_the_gm_forgets_end_turn(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    fallback = _spy_on_fallback_close(monkeypatch, app, campaign_id)
    script = _script({"text": "Pip waits."}, {"text": "Pip shrugs and does nothing."})
    assert _send(client, campaign_id, script).status_code == 303

    lines = _npc_turn_stream(client, campaign_id, "Pip")

    assert lines[-1]["type"] == "saved"
    state = _state(app, campaign_id)
    assert state.turn is None
    assert state.encounter.acting == "Ada"
    assert state.encounter.up == 1
    assert fallback == ["Pip"]


def test_player_turn_starts_on_its_own_and_ends_by_button(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _begin(app, campaign_id, "Pip")
    _finish_gm_reply(app, campaign_id)
    _end(app, campaign_id, "Pip", by_player=False)
    _finish_gm_reply(app, campaign_id)
    assert _state(app, campaign_id).encounter.acting == "Ada"

    assert _encounter_post(client, campaign_id, "end", "Ada").status_code == 303
    state = _state(app, campaign_id)
    assert state.encounter.acting is None
    assert state.encounter.round == 2


def test_player_turn_starts_as_soon_as_it_is_next(client_factory: Callable[[], TestClient]):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _call(app, campaign_id, "remove_participant", {"name": "Pip"})
    assert _encounter(app, campaign_id).encounter.acting == "Ada"
    assert _end(app, campaign_id, "Ada", by_player=True) == {"ended_turn": "Ada"}
    assert _encounter(app, campaign_id).encounter.acting == "Ada"


def test_turn_buttons_refuse_the_wrong_participant(client_factory: Callable[[], TestClient]):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app

    early = _encounter_post(client, campaign_id, "stream", "Ada")
    assert "it is Pip" in early.text

    _begin(app, campaign_id, "Pip")
    _finish_gm_reply(app, campaign_id)
    wrong = _encounter_post(client, campaign_id, "end", "Pip")
    assert wrong.status_code == 400
    assert "GM ends Pip" in wrong.text
    assert _state(app, campaign_id).encounter.acting == "Pip"


def test_campaign_page_shows_the_encounter_board_and_turn_controls(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _place(app, campaign_id, "Ada", 2, 3, grid=None)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert "encounter-board" in page
    assert MAP in page
    assert "Ada moves to column 3, row 4." in page
    assert "Start Pip" in page
    assert "column 3, row 4" in page
    assert "Start your turn" not in page
    assert 'id="tab-encounter" aria-controls="panel-encounter" aria-selected="true"' in page
    assert 'id="tab-world" aria-controls="panel-world" aria-selected="false"' in page
    assert 'aria-label="Map views"' in page
    assert page.index('aria-label="Map views"') < page.index('aria-label="Panels"')
    assert page.index('id="panel-encounter"') < page.index('aria-label="Panels"')
    assert page.index('id="panel-world"') < page.index('aria-label="Panels"')
    assert 'id="tab-characters" aria-controls="panel-characters" aria-selected="true"' in page
    assert "world-map-details" not in page

    _begin(app, campaign_id, "Pip")
    _finish_gm_reply(app, campaign_id)
    _end(app, campaign_id, "Pip", by_player=False)
    _finish_gm_reply(app, campaign_id)
    acting = client.get(f"/campaigns/{campaign_id}").text
    assert "End your turn" in acting
    assert "Start your turn" not in acting
    assert acting.index("End your turn") < acting.index('id="composer"')


def test_campaign_page_has_no_board_without_an_encounter(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)
    assert "encounter-board" not in client.get(f"/campaigns/{campaign_id}").text
    assert 'id="tab-world"' not in client.get(f"/campaigns/{campaign_id}").text


def test_token_labels_are_short_initials_or_a_prefix():
    assert token_label("Mossbell Hollow") == "MH"
    assert token_label("Ashen Ford Inn") == "AFI"
    assert token_label("Ada Lovelace the Third") == "ALT"
    assert token_label("Pip") == "Pip"
    assert token_label("  Bartholomew ") == "Bar"


def test_encounter_map_can_be_renamed_and_moved_while_in_use(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=20, rows=20)
    app = client.app
    _call(app, campaign_id, "create_location", {"name": "Far Shore", "description": "Across."})
    _place(app, campaign_id, "Ada", 2, 2, grid=None)

    renamed = _call(
        app,
        campaign_id,
        "update_encounter_map",
        {
            "name": MAP,
            "columns": 12,
            "rows": 12,
            "description": "Smaller now.",
            "new_name": "Green Two",
            "location": "Far Shore",
        },
    )
    assert renamed == {
        "updated": "encounter map",
        "name": "Green Two",
        "location": "Far Shore",
        "columns": 12,
        "rows": 12,
    }
    state = _encounter(app, campaign_id)
    assert state.maps.keys() == {"green two"}
    assert state.maps["green two"].location == "Far Shore"
    assert state.encounter.map == "Green Two"
    assert state.encounter.location == "Far Shore"
    assert state.encounter.cells["ada"] == (2, 2)

    assert _place(app, campaign_id, "Ada", 11, 11)["placed"] == "Ada"
    assert "error" in _place(app, campaign_id, "Ada", 12, 0)


def test_encounter_map_rename_keeps_its_own_name_and_refuses_clashes(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=20, rows=20)
    app = client.app
    _call(
        app,
        campaign_id,
        "create_encounter_map",
        {
            "name": "Shore Green",
            "location": TOWN,
            "columns": 5,
            "rows": 5,
            "description": "Pebbles.",
        },
    )
    base = {"name": MAP, "columns": 20, "rows": 20, "description": "Open ground."}

    clash = _call(app, campaign_id, "update_encounter_map", {**base, "new_name": "shore GREEN"})
    assert "already" in clash["error"]
    nowhere = _call(app, campaign_id, "update_encounter_map", {**base, "location": "Nowhere"})
    assert "no location named" in nowhere["error"]
    blank = _call(app, campaign_id, "update_encounter_map", {**base, "new_name": "   "})
    assert "error" in blank

    same = _call(app, campaign_id, "update_encounter_map", {**base, "new_name": MAP.upper()})
    assert same["name"] == MAP.upper()
    assert _encounter(app, campaign_id).maps.keys() == {MAP.casefold(), "shore green"}


def test_campaign_page_draws_tokens_with_full_name_tooltips_and_no_legend(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _place(app, campaign_id, "Ada", 2, 3, grid=None)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert '<span class="map-token">Ada</span>' in page
    assert 'title="Ada"' in page
    assert "Not on the map: Pip." in page
    assert "encounter-list" not in page
    assert "map-legend" not in page
    assert "map-number" not in page


def test_campaign_page_says_when_no_one_is_on_the_encounter_map(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _place(app, campaign_id, "Ada", 1, 1, grid=None)
    _place(app, campaign_id, "Pip", 2, 1, grid=None)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Not on the map" not in page
    assert '<span class="map-token">Pip</span>' in page
