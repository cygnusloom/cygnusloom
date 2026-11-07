from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.character import NO_CAMPAIGN
from cygnusloom.llm.base import ToolRequest
from cygnusloom.rules.context import ToolContext, brief
from cygnusloom.services import world
from cygnusloom.services.events import CampaignState, load_state
from cygnusloom.services.tools import ToolRun, run_tool
from cygnusloom.services.views import story_view, world_state_text
from tests.conftest import content_library
from tests.factories import campaign_ready_fast

LIBRARY = content_library()
CONTRACT = LIBRARY.rule_sets["rolltest"]
TOOLS = CONTRACT.tool_calls
TABLES = LIBRARY.core.tables


def _state(app: FastAPI, campaign_id: int) -> CampaignState:
    with Session(app.state.engine, expire_on_commit=False) as session:
        return load_state(session, campaign_id)


def _ctx(app: FastAPI, campaign_id: int) -> ToolContext:
    async def quiet_speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
        return None

    return ToolContext(
        world=world.WorldTools(
            engine=app.state.engine,
            campaign_id=campaign_id,
            tables=TABLES,
            rng=random.Random(0),
            character=CONTRACT.character,
            plus="sum",
        ),
        speak=quiet_speak,
        ooc=lambda narrate: None,
        rules=CONTRACT.rules,
        lookup_tools={
            tool.name: brief(tool.description)
            for tool in CONTRACT.tool_calls.values()
            if tool.lookup
        },
    )


def _tool(app: FastAPI, campaign_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    request = ToolRequest(id="call_1", name=name, arguments=json.dumps(arguments))
    run = asyncio.run(run_tool(TOOLS, request, None, _ctx(app, campaign_id)))
    assert isinstance(run, ToolRun)
    result = json.loads(run.output)
    assert isinstance(result, dict)
    return result


def _create(app: FastAPI, campaign_id: int, **fields: Any) -> dict[str, Any]:
    payload = {"name": "The Missing Heir", "summary": "A miller's son has vanished."}
    payload.update(fields)
    return _tool(app, campaign_id, "create_storyline", payload)


def test_creating_a_storyline_records_it_focuses_it_and_shows_it_to_the_player(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    result = _create(
        app,
        campaign_id,
        summary="The miller's son is missing, and the village blames the fog.",
        plot="The bog witch took the heir; the village hid the bargain.",
        characters=["Mara Thistledown", "Brin Gallowcap"],
        locations=["Mossbell Hollow"],
        objects=["A brass key"],
        notes="The key opens the mill cellar.",
    )
    assert result["created"] == "storyline"
    assert result["focused"] is True
    state = _state(app, campaign_id)
    storyline = state.storylines[result["id"]]
    assert storyline.name == "The Missing Heir"
    assert storyline.summary == "The miller's son is missing, and the village blames the fog."
    assert storyline.plot == "The bog witch took the heir; the village hid the bargain."
    assert storyline.characters == ("Mara Thistledown", "Brin Gallowcap")
    assert storyline.locations == ("Mossbell Hollow",)
    assert storyline.objects == ("A brass key",)
    assert storyline.notes == "The key opens the mill cellar."
    assert storyline.active is True
    assert state.focused_storyline() is storyline

    context = world_state_text(state, NO_CAMPAIGN)
    assert "Storylines (the focused one is the main plot; the others are secondary):" in context
    assert "- [" in context
    assert "The Missing Heir (focused): " in context
    assert "Plot: The bog witch took the heir; the village hid the bargain." in context
    assert "Characters: Mara Thistledown, Brin Gallowcap" in context
    assert "Locations: Mossbell Hollow" in context
    assert "Objects: A brass key" in context
    assert "Notes: The key opens the mill cellar." in context

    view = story_view(state)
    assert [(item.name, item.active, item.focused) for item in view.storylines] == [
        ("The Missing Heir", True, True)
    ]
    assert view.storylines[0].summary == storyline.summary


def test_up_to_three_storylines_can_be_active_and_a_fourth_is_refused(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    first = _create(app, campaign_id, name="The Missing Heir")
    _create(app, campaign_id, name="The Salt Road", summary="A trade route closes.")
    _create(app, campaign_id, name="The Drowned Bell", summary="A bell rings underwater.")

    state = _state(app, campaign_id)
    assert [story.name for story in state.active_storylines()] == [
        "The Missing Heir",
        "The Salt Road",
        "The Drowned Bell",
    ]
    # The first storyline keeps the focus; the other two are active but secondary.
    assert state.focused_storyline() is not None
    assert state.focused_storyline().id == first["id"]

    view = story_view(state)
    assert [(item.name, item.active, item.focused) for item in view.storylines] == [
        ("The Missing Heir", True, True),
        ("The Salt Road", True, False),
        ("The Drowned Bell", True, False),
    ]

    refused = _create(app, campaign_id, name="The Fourth Thread")
    assert "3 storylines are already active" in refused["error"]
    assert "deactivate one" in refused["error"]
    assert len(_state(app, campaign_id).active_storylines()) == 3


def test_focusing_a_storyline_makes_it_the_main_plot(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    _create(app, campaign_id, name="The Missing Heir", summary="The miller's son is lost.")
    second = _create(app, campaign_id, name="The Salt Road", summary="The road is closed.")

    focused = _tool(app, campaign_id, "focus_storyline", {"storyline": "The Salt Road"})
    assert focused == {"focused": "The Salt Road", "id": second["id"]}
    state = _state(app, campaign_id)
    assert state.focused_storyline() is not None
    assert state.focused_storyline().id == second["id"]

    view = story_view(state)
    assert [(item.name, item.focused) for item in view.storylines] == [
        ("The Salt Road", True),
        ("The Missing Heir", False),
    ]
    assert view.storylines[0].summary == "The road is closed."

    context = world_state_text(state, NO_CAMPAIGN)
    assert context.index("The Salt Road (focused)") < context.index("The Missing Heir (secondary)")


def test_deactivating_the_focused_storyline_hides_it_from_context_and_promotes_the_oldest_active(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    first = _create(app, campaign_id, name="The Missing Heir", summary="The heir is missing.")
    second = _create(app, campaign_id, name="The Salt Road", summary="The road is closed.")
    third = _create(app, campaign_id, name="The Drowned Bell", summary="The bell rings below.")
    _tool(app, campaign_id, "focus_storyline", {"storyline": "The Salt Road"})

    result = _tool(app, campaign_id, "deactivate_storyline", {"storyline": "The Salt Road"})
    assert result["deactivated"] == "The Salt Road"
    assert result["focused"] == "The Missing Heir"

    state = _state(app, campaign_id)
    assert state.storylines[second["id"]].active is False
    assert state.focused_storyline() is not None
    assert state.focused_storyline().id == first["id"]
    assert [story.id for story in state.active_storylines()] == [first["id"], third["id"]]

    view = story_view(state)
    assert [(item.name, item.active, item.focused) for item in view.storylines] == [
        ("The Missing Heir", True, True),
        ("The Drowned Bell", True, False),
        ("The Salt Road", False, False),
    ]
    context = world_state_text(state, NO_CAMPAIGN)
    assert "The Salt Road" not in context

    found = _tool(app, campaign_id, "search_storylines", {"query": "salt"})
    assert found["storylines"][0]["id"] == second["id"]
    assert found["storylines"][0]["active"] is False
    assert found["storylines"][0]["focused"] is False

    refused = _tool(app, campaign_id, "focus_storyline", {"storyline": "The Salt Road"})
    assert "deactivated" in refused["error"]

    reactivated = _tool(
        app,
        campaign_id,
        "update_storyline",
        {"storyline": "The Salt Road", "active": True},
    )
    assert reactivated["active"] is True
    assert _state(app, campaign_id).storylines[second["id"]].active is True


def test_splitting_a_storyline_is_an_update_and_a_create(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    created = _create(
        app,
        campaign_id,
        name="Two Missing",
        summary="Aldra and Jandy are both missing from Isthmus Town.",
        plot="Both searches begin in town, but they lead to different places.",
    )
    _tool(
        app,
        campaign_id,
        "update_storyline",
        {
            "storyline": "Two Missing",
            "name": "The Missing Heir",
            "summary": "Aldra disappeared after leaving the Butcher after dark.",
            "plot": "Aldra's ledger points to the smuggling at the inn.",
        },
    )
    split = _create(
        app,
        campaign_id,
        name="The Missing Tithe",
        summary="Jandy and the tithe never reached the Cities.",
        plot="The tithe trail leads into the Wood.",
    )

    state = _state(app, campaign_id)
    assert [story.name for story in state.active_storylines()] == [
        "The Missing Heir",
        "The Missing Tithe",
    ]
    # Splitting keeps the original thread focused and the new thread secondary.
    assert state.focused_storyline() is not None
    assert state.focused_storyline().id == created["id"]
    assert split["focused"] is False
    view = story_view(state)
    assert [(item.name, item.focused) for item in view.storylines] == [
        ("The Missing Heir", True),
        ("The Missing Tithe", False),
    ]


def test_updating_a_storyline_keeps_fields_left_out_and_can_clear_them(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    created = _create(
        app,
        campaign_id,
        plot="The witch holds the heir.",
        characters=["Mara"],
        objects=["A brass key"],
        notes="The cellar holds the answer.",
    )
    result = _tool(
        app,
        campaign_id,
        "update_storyline",
        {
            "storyline": "The Missing Heir",
            "summary": "The heir is held in the bog.",
            "characters": ["Mara", "Brin"],
            "notes": "",
        },
    )
    assert result["updated"] == "storyline"
    assert result["summary"] == "The heir is held in the bog."
    state = _state(app, campaign_id)
    storyline = state.storylines[created["id"]]
    assert storyline.name == "The Missing Heir"
    assert storyline.plot == "The witch holds the heir."
    assert storyline.characters == ("Mara", "Brin")
    assert storyline.objects == ("A brass key",)
    assert storyline.notes == ""

    # A name shared with another storyline is refused rather than making references ambiguous.
    other = _create(app, campaign_id, name="The Salt Road", summary="The road is closed.")
    refused = _tool(
        app,
        campaign_id,
        "update_storyline",
        {"storyline": "The Salt Road", "name": "The Missing Heir"},
    )
    assert "already named" in refused["error"]
    assert _state(app, campaign_id).storylines[other["id"]].name == "The Salt Road"


def test_searching_storylines_finds_deactivated_threads_by_any_field(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    created = _create(
        app,
        campaign_id,
        characters=["Mara Thistledown"],
        locations=["The Old Mill"],
        objects=["A brass key"],
        notes="The key is under the third stone.",
    )
    _tool(app, campaign_id, "deactivate_storyline", {"storyline": "The Missing Heir"})

    for query in ("missing heir", "Mara", "Old Mill", "brass key", "third stone"):
        found = _tool(app, campaign_id, "search_storylines", {"query": query})
        assert found["storylines"][0]["id"] == created["id"], query
        assert found["storylines"][0]["active"] is False, query

    found = _tool(app, campaign_id, "search_storylines", {"query": "no such thread"})
    assert found["storylines"] == []


def test_the_journal_story_tab_shows_every_player_facing_storyline(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app = client.app
    _create(
        app,
        campaign_id,
        name="The Missing Heir",
        summary="The heir is missing.",
        plot="The bog witch took him, and the village hid the bargain.",
    )
    _create(app, campaign_id, name="The Salt Road", summary="The road is closed.")
    _tool(app, campaign_id, "deactivate_storyline", {"storyline": "The Salt Road"})

    page = client.get(f"/campaigns/{campaign_id}")
    assert page.status_code == 200
    # Storylines live in the Journal view now, not the small side panel.
    assert 'id="journal-tab-story"' in page.text
    assert 'id="journal-panel-story"' in page.text
    assert 'id="tab-story"' not in page.text
    assert 'id="panel-story"' not in page.text
    # The focused thread, the deactivated thread, and their summaries are all shown.
    assert "The Missing Heir" in page.text
    assert "The heir is missing." in page.text
    assert "The Salt Road" in page.text
    assert "The road is closed." in page.text
    assert "Deactivated" in page.text
    # The private plot and notes never reach the player-facing page.
    assert "bog witch" not in page.text
