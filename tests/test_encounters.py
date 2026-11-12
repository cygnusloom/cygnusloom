from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.content.library import EncounterGrid
from cygnusloom.llm.base import ToolRequest
from cygnusloom.rules.context import ToolContext, brief
from cygnusloom.services import encounters
from cygnusloom.services import turns as turns_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    NPC_KIND,
    PC_KIND,
    CampaignState,
    Character,
    ChatGM,
    Encounter,
    group_by_side,
    record,
    speaker_name,
    token_label,
    token_labels,
)
from cygnusloom.services.tools import ToolRun, run_tool
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library, csrf_token
from tests.factories import (
    answer,
    campaign_ready,
    campaign_ready_fast,
    create_character,
    place_pc,
    send_message,
)
from tests.factories import (
    db as _db,
)
from tests.factories import (
    open_campaign as _state,
)
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_combat import _tools
from tests.test_tools import _script

LIBRARY = content_library()
CAIRN_GRID = LIBRARY.rule_sets["cairn2e"].grid
CAIRN_TOOLS = LIBRARY.rule_sets["cairn2e"].tool_calls
MAP = "Mossbell Green"
TOWN = "Mossbell Hollow"


def _ctx(
    app: FastAPI,
    campaign_id: int,
    speak: Callable[[str, str, str, bool], Any] | None = None,
) -> ToolContext:
    """The tool context the app builds, from the fixture rule set's own tools."""
    rule_set = LIBRARY.rule_sets["rolltest"]

    async def quiet_speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        return None

    return ToolContext(
        world=WorldTools(
            engine=app.state.engine,
            campaign_id=campaign_id,
            tables=dict(LIBRARY.core.tables),
            rng=random.Random(0),
            character=rule_set.character,
            plus="sum",
        ),
        speak=speak or quiet_speak,
        ooc=lambda narrate: None,
        rules=rule_set.rules,
        lookup_tools={
            tool.name: brief(tool.description)
            for tool in rule_set.tool_calls.values()
            if tool.lookup
        },
    )


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
    run = asyncio.run(run_tool(CAIRN_TOOLS, request, None, ctx))
    assert isinstance(run, ToolRun)
    return json.loads(run.output)


def _encounter_with(
    client_factory: Callable[[], TestClient], columns: int = 20, rows: int = 20, cairn: bool = False
) -> tuple[TestClient, int]:
    """A campaign with Ada placed in town, a map there, an encounter started, and Pip (an NPC)
    and Ada joined in that order. The Cairn option gives a Cairn campaign, whose GM has the
    encounter tools in its own table; the test campaign has none of them."""
    if cairn:
        client, campaign_id = _cairn_campaign(client_factory)
        created = create_character(
            client, campaign_id, name="Ada", fields=_complete_form(client, campaign_id)
        )
        assert created.status_code == 303
        answer(client, campaign_id, "continue")
        _call(
            client.app, campaign_id, "create_location", {"name": TOWN, "description": "A square."}
        )
    else:
        client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    place_pc(client, campaign_id, TOWN)
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
    client, campaign_id = campaign_ready(client_factory)
    app = client.app
    place_pc(client, campaign_id, TOWN)
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


def test_participant_with_no_attacks_gets_a_warning(
    client_factory: Callable[[], TestClient],
):
    """An NPC with a sheet but no attacks can take a turn in the order, but the GM should hear
    before the fight that they cannot strike. The result warns without refusing the join."""
    client, campaign_id = _encounter_with(client_factory)
    app = client.app
    _call(
        app,
        campaign_id,
        "create_npc",
        {"name": "Wren", "location": TOWN, "description": "A ferry hand."},
    )

    added = _call(app, campaign_id, "add_participant", {"name": "Wren"})

    assert added["added"] == "Wren"
    assert "has no attacks" in added["warning"]
    assert "update_npc" in added["warning"]


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
    forced = _call(
        app,
        campaign_id,
        "place_participant",
        {"name": "Pip", "column": 9, "row": 0, "force": True},
        CAIRN_GRID,
    )
    assert forced["placed"] == "Pip"
    assert forced["moves_left"] == 0


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
    assert _end(app, campaign_id, "Pip", by_player=False) == {"ended_turn": "Pip"}
    _finish_gm_reply(app, campaign_id)

    assert _encounter(app, campaign_id).encounter.acting is None
    _begin(app, campaign_id, "Ada")
    with pytest.raises(InvalidInput, match="the player ends Ada's turn"):
        _end(app, campaign_id, "Ada", by_player=False)
    assert _end(app, campaign_id, "Ada", by_player=True) == {"ended_turn": "Ada"}
    assert _encounter(app, campaign_id).encounter.round == first_round + 1


def test_encounter_ends_mid_turn(client_factory: Callable[[], TestClient]):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _begin(app, campaign_id, "Pip")
    assert _call(app, campaign_id, "end_encounter", {})["ended"] == "encounter"
    assert _encounter(app, campaign_id).encounter is None
    _finish_gm_reply(app, campaign_id)
    assert _encounter(app, campaign_id).turn is None


def test_removing_the_acting_participant_passes_the_turn_on(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _begin(app, campaign_id, "Pip")
    assert _call(app, campaign_id, "remove_participant", {"name": "Pip"}) == {"removed": "Pip"}
    state = _encounter(app, campaign_id).encounter
    assert state.acting is None
    assert state.order == ["Ada"]
    _finish_gm_reply(app, campaign_id)
    _begin(app, campaign_id, "Ada")
    _end(app, campaign_id, "Ada", by_player=True)
    assert _call(app, campaign_id, "end_encounter", {})["ended"] == "encounter"


def test_a_next_up_player_character_may_attack_while_none_is_acting(
    client_factory: Callable[[], TestClient],
):
    """A settled order leaves nobody acting between turns. The player character whose turn is
    next may strike; an NPC, or a player character who is not next, still needs their turn."""
    client, campaign_id = _encounter_with(client_factory, cairn=True)
    app = client.app
    tools = _tools(app, campaign_id)
    tools.create_monster(
        "Bo",
        TOWN,
        description="A test foe.",
        stats={"hp": 6, "str": 10, "dex": 10, "wil": 10},
        attacks=[{"name": "Claw", "dice": "1d6"}],
    )
    assert tools.add_participant("Bo")["added"] == "Bo"

    # Pip joined first, so Ada is a player character who is not next, and Pip is next but an NPC.
    with pytest.raises(InvalidInput, match="no one is taking a turn"):
        tools.player_attack("Bo", "unarmed", damage_roll=1)
    with pytest.raises(InvalidInput, match="no one is taking a turn"):
        tools.attack("Pip", "Claw", "Ada")

    # Pip's turn passes; Ada is next with nobody acting.
    _begin(app, campaign_id, "Pip")
    _end(app, campaign_id, "Pip", by_player=False)
    _finish_gm_reply(app, campaign_id)
    state = _encounter(app, campaign_id).encounter
    assert state is not None and state.acting is None
    assert state.order[state.up] == "Ada"

    result = tools.player_attack("Bo", "unarmed", damage_roll=1)
    assert result["target"] == "Bo"


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


def test_npc_turn_is_played_out_and_ends_automatically(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10, cairn=True)
    app = client.app
    # The directive's turn is the GM's reply to the player's message; the NPC's turn is the next.
    script = _script(
        {"text": "Pip waits by the gate."},
        {"text": "Pip ducks behind the crate."},
    )
    assert send_message(client, campaign_id, script).status_code == 303

    lines = _npc_turn_stream(client, campaign_id, "Pip")

    assert lines[-1]["type"] == "saved"
    state = _state(app, campaign_id)
    # The engine ends the NPC's turn when the reply finishes, and Ada's turn starts on its own.
    assert state.turn is None
    assert state.encounter.acting == "Ada"
    assert state.encounter.up == 1
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "End Pip&#39;s turn" not in page
    assert "Start Ada's turn" not in page
    assert "End your turn" in page
    assert "Attacks left: 1 of 1." in page
    assert "Movement left:" in page


def test_the_encounter_view_reports_the_turns_remaining_resources(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory)
    app = client.app
    _begin(app, campaign_id, "Pip")
    state = _encounter(app, campaign_id)
    combat = LIBRARY.rule_sets["cairn2e"].combat
    view = encounters.encounter_view(state, CAIRN_GRID, combat)
    assert view is not None
    assert view["acting"] == "Pip"
    assert (view["attacks_left"], view["attacks_per_turn"]) == (1, 1)
    assert view["moves_left"] == CAIRN_GRID.move_cells


def test_an_out_of_character_npc_turn_still_ends_when_the_reply_finishes(
    client_factory: Callable[[], TestClient],
):
    """The GM can answer an NPC's turn out of character, such as when the statblock has nothing
    to roll. That reply is still the end of the turn, so the table is not left waiting on it."""
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10, cairn=True)
    app = client.app
    script = _script(
        {"text": "Pip waits by the gate."},
        {
            "rounds": [[{"name": "ooc", "arguments": {"no_narration": True}}]],
            "ooc": "Pip has no attacks to roll.",
        },
    )
    assert send_message(client, campaign_id, script).status_code == 303

    lines = _npc_turn_stream(client, campaign_id, "Pip")

    assert lines[-1]["type"] == "saved"
    state = _state(app, campaign_id)
    assert state.turn is None
    assert state.encounter.acting == "Ada"
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "End Pip&#39;s turn" not in page
    assert "End your turn" in page


def test_the_owner_can_end_an_npc_turn_the_gm_left_open(
    client_factory: Callable[[], TestClient],
):
    """A reply that finished without closing its NPC turn used to leave the table with no input.
    The owner now gets the End control for that turn so play can continue."""
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10, cairn=True)
    app = client.app
    with _db(app) as db:
        encounters.begin_turn(db, campaign_id, "Pip")
    # The old failed path: the reply is saved, the turn is closed, but the encounter still says
    # it is Pip's turn.
    _finish_gm_reply(app, campaign_id)
    state = _state(app, campaign_id)
    assert state.turn is None
    assert state.encounter is not None and state.encounter.acting == "Pip"

    page = client.get(f"/campaigns/{campaign_id}").text
    assert "End Pip&#39;s turn" in page
    assert "Pip&#39;s turn is still open." in page

    ended = _encounter_post(client, campaign_id, "end", "Pip")

    assert ended.status_code == 303
    state = _state(app, campaign_id)
    assert state.encounter is not None and state.encounter.acting == "Ada"
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "End Pip&#39;s turn" not in page
    assert "End your turn" in page


def test_a_player_turn_starts_after_the_initiative_prompt_finishes(
    client_factory: Callable[[], TestClient], monkeypatch
):
    """The player rolls initiative inside the GM's reply; when the reply ends, their turn begins."""
    client, campaign_id = _cairn_campaign(client_factory)
    created = create_character(
        client, campaign_id, name="Ada", fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    answer(client, campaign_id, "continue")
    app = client.app
    rounds = [
        [
            {
                "name": "create_location",
                "arguments": {
                    "name": "Test Yard",
                    "description": "A dusty yard.",
                    "discovered": True,
                    "summary": "A dusty yard.",
                },
            }
        ],
        [
            {
                "name": "move_actor",
                "arguments": {"name": "Ada", "location": "Test Yard", "mode": "in_settlement"},
            }
        ],
        [
            {
                "name": "create_encounter_map",
                "arguments": {
                    "name": "Test Yard Map",
                    "location": "Test Yard",
                    "description": "A dusty yard.",
                    "columns": 6,
                    "rows": 6,
                },
            }
        ],
        [{"name": "start_encounter", "arguments": {"map": "Test Yard Map"}}],
        [{"name": "add_participant", "arguments": {"name": "Ada"}}],
        [{"name": "player_initiative", "arguments": {"prompt": "Roll initiative."}}],
    ]
    script = _script({"rounds": rounds, "text": "The fight begins."})
    assert send_message(client, campaign_id, script).status_code == 303
    assert _state(app, campaign_id).encounter.acting is None
    pending = _state(app, campaign_id).turn
    assert pending is not None and pending.prompt is not None, pending

    # The reply ends after the roll, so Ada's turn should start without another button.
    monkeypatch.setattr(turns_service, "roll_values", lambda *args, **kwargs: ([1], 1))
    events = answer(client, campaign_id, "roll")

    assert events[-1]["type"] == "saved"
    state = _state(app, campaign_id)
    assert state.turn is None
    assert state.encounter.order == ["Ada"]
    assert state.encounter.acting == "Ada"
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "End your turn" in page
    assert "Start Ada's turn" not in page


def test_npc_turn_ends_only_when_the_gm_reply_finishes(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10, cairn=True)
    app = client.app
    script = _script({"text": "Pip waits."}, {"text": "Pip shrugs and does nothing."})
    assert send_message(client, campaign_id, script).status_code == 303

    lines = _npc_turn_stream(client, campaign_id, "Pip")

    assert lines[-1]["type"] == "saved"
    state = _state(app, campaign_id)
    assert state.turn is None
    assert state.encounter.acting == "Ada"
    assert _encounter_post(client, campaign_id, "end", "Pip").status_code == 400


def test_player_turn_starts_automatically_and_ends_by_button(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _begin(app, campaign_id, "Pip")
    _finish_gm_reply(app, campaign_id)
    _end(app, campaign_id, "Pip", by_player=False)
    _finish_gm_reply(app, campaign_id)
    assert _state(app, campaign_id).encounter.acting == "Ada"
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Start Ada's turn" not in page
    assert "End your turn" in page

    assert _encounter_post(client, campaign_id, "end", "Ada").status_code == 303
    state = _state(app, campaign_id)
    # The new round groups the party first, so Ada's next turn starts at once.
    assert state.encounter.acting == "Ada"
    assert state.encounter.round == 2

    # With only the party left, ending a player turn still starts the next round's turn.
    _call(app, campaign_id, "remove_participant", {"name": "Pip"})
    assert _end(app, campaign_id, "Ada", by_player=True) == {"ended_turn": "Ada"}
    assert _encounter(app, campaign_id).encounter.acting == "Ada"


def test_turn_buttons_refuse_the_wrong_participant(client_factory: Callable[[], TestClient]):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app

    early = _encounter_post(client, campaign_id, "stream", "Ada")
    assert "it is Pip" in early.text

    _begin(app, campaign_id, "Pip")
    _finish_gm_reply(app, campaign_id)
    wrong = _encounter_post(client, campaign_id, "end", "Ada")
    assert wrong.status_code == 400
    assert "Ada is not taking a turn" in wrong.text
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
    assert 'id="tab-location" aria-controls="panel-location" aria-selected="true"' in page
    assert "world-map-details" not in page

    _begin(app, campaign_id, "Pip")
    _finish_gm_reply(app, campaign_id)
    _end(app, campaign_id, "Pip", by_player=False)
    _finish_gm_reply(app, campaign_id)
    acting = client.get(f"/campaigns/{campaign_id}").text
    assert "Start Ada's turn" not in acting
    assert "End your turn" in acting
    assert "Start your turn" not in acting
    assert acting.index("End your turn") < acting.index('id="composer"')


def test_an_idle_player_turn_gets_an_automatic_begin_form(
    client_factory: Callable[[], TestClient],
):
    """BACKWARDS-COMPATIBILITY: 3 - a saved campaign may still wait on the old button."""
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _call(app, campaign_id, "remove_participant", {"name": "Pip"})

    assert _encounter(app, campaign_id).encounter.acting is None
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Starting your turn..." in page
    assert "data-auto-start-turn" in page
    assert "Start Ada's turn" not in page


def test_campaign_page_has_no_board_or_roster_without_an_encounter(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "encounter-board" not in page
    assert 'id="tab-world"' not in page
    assert "encounter-roster" not in page
    assert 'class="side"' in page


def test_token_labels_are_short_initials_or_a_prefix():
    assert token_label("Mossbell Hollow") == "MH"
    assert token_label("Ashen Ford Inn") == "AFI"
    assert token_label("Ada Lovelace the Third") == "ALT"
    assert token_label("Pip") == "Pip"
    assert token_label("  Bartholomew ") == "Bar"


def test_token_labels_never_share_a_label_within_a_set():
    labels = token_labels(["Mara Thistledown", "Mossbell Terrace", "Mossbell Hollow"])
    assert labels["Mossbell Hollow"] == "MH"
    assert len(set(labels.values())) == 3

    labels = token_labels(["Mara Tower", "Mara Thistledown"])
    assert len(set(labels.values())) == 2


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
    assert 'class="map-token"' in page
    assert 'data-side="party"' in page
    assert ">Ada</span>" in page
    assert 'data-tip="Ada"' in page
    assert "Not on the map" not in page
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
    assert 'data-side="foes"' in page
    assert ">Pip</span>" in page


def test_campaign_page_lists_the_encounter_turn_order_in_the_location_panel(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _place(app, campaign_id, "Ada", 2, 3, grid=None)
    _place(app, campaign_id, "Pip", 4, 4, grid=None)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="side in-encounter"' in page
    assert 'class="tab-panel location-panel with-roster"' in page
    roster = page[page.index('class="encounter-roster"') :]
    assert roster.index("Pip") < roster.index("Ada")
    assert page.index('aria-label="Panels"') < page.index('class="encounter-roster"')

    _begin(app, campaign_id, "Pip")
    _finish_gm_reply(app, campaign_id)
    acting = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="acting"' in acting
    assert '<span class="roster-state">acting</span>' in acting


def test_the_roster_and_its_tokens_colour_each_side(client_factory: Callable[[], TestClient]):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10)
    app = client.app
    _call(
        app,
        campaign_id,
        "create_npc",
        {"name": "Moss", "location": TOWN, "description": "A bramble."},
    )
    added = _call(app, campaign_id, "add_participant", {"name": "Moss", "side": "wild things"})
    assert added["side"] == "wild things"
    _place(app, campaign_id, "Ada", 2, 3, grid=None)
    _place(app, campaign_id, "Pip", 4, 4, grid=None)
    _place(app, campaign_id, "Moss", 6, 6, grid=None)

    page = client.get(f"/campaigns/{campaign_id}").text

    assert f'style="--token-side: {encounters.PARTY_SIDE_COLOR}" data-side="party"' in page
    assert f'style="--token-side: {encounters.FOES_SIDE_COLOR}" data-side="foes"' in page
    assert (
        f'style="--token-side: {encounters.OTHER_SIDE_COLORS[0]}" data-side="wild things"'
        in page
    )
    assert '<span class="roster-side">party</span>' in page
    assert '<span class="roster-side">foes</span>' in page
    assert '<span class="roster-side">wild things</span>' in page
    assert f'style="--side-color: {encounters.PARTY_SIDE_COLOR}"' in page
    assert f'style="--side-color: {encounters.FOES_SIDE_COLOR}"' in page


def _npc(name: str) -> Character:
    return Character(
        kind=NPC_KIND,
        name=name,
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
    )


def test_composer_speaks_for_the_acting_pc_or_hireling_and_otherwise_the_pc():
    pc = Character(
        kind=PC_KIND,
        name="Ada",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
    )
    hireling = replace(pc, name="Bram", hireling=True)
    pip = _npc("Pip")
    characters = {c.name.casefold(): c for c in (pc, hireling, pip)}

    def acting(name: str | None) -> str | None:
        encounter = Encounter(map=MAP, location=TOWN, order=["Ada", "Bram", "Pip"], acting=name)
        return speaker_name(CampaignState(characters=characters, encounter=encounter))

    assert acting(None) == "Ada"
    assert acting("Bram") == "Bram"
    assert acting("Ada") == "Ada"
    assert acting("Pip") == "Ada"
    assert speaker_name(CampaignState(characters=characters)) == "Ada"
    assert speaker_name(CampaignState()) is None


def test_group_by_side_keeps_the_party_first_and_the_next_turn():
    state = CampaignState()
    for name in ("Pip", "Ada", "Bo", "Cy"):
        state.characters[name.casefold()] = _npc(name)
    encounter = Encounter(
        map="Green",
        location="Hollow",
        order=["Pip", "Ada", "Bo", "Cy"],
        sides={"pip": "foes", "ada": "party", "bo": "foes", "cy": "party"},
        up=2,
        round=2,
    )

    group_by_side(encounter)

    assert encounter.order == ["Ada", "Cy", "Pip", "Bo"]
    assert encounter.order[encounter.up] == "Bo"


def test_group_by_side_puts_a_new_side_after_the_ones_already_there():
    encounter = Encounter(
        map="Green",
        location="Hollow",
        order=["Wolf", "Ada", "Hound"],
        sides={"wolf": "beasts", "ada": "party", "hound": "wardens"},
    )

    group_by_side(encounter, keep_turn=False)

    assert encounter.order == ["Ada", "Wolf", "Hound"]
