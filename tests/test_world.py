from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.content.library import ContentLibrary
from cygnusforge.llm.base import ToolRequest
from cygnusforge.rules.context import ToolContext
from cygnusforge.services import world
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import NPC_KIND, CampaignState, Character, load_state
from cygnusforge.services.tools import ToolRun, run_tool
from tests.conftest import csrf_token
from tests.test_campaigns import _campaign_id, _new_campaign, _send, _signed_in
from tests.test_tools import _recording_provider, _saved, _script

LIBRARY = ContentLibrary.load()
TOOLS = LIBRARY.core.tool_calls
TABLES = LIBRARY.core.tables
BUNDLED_NPCS = {
    "Mara Thistledown": "Mossbell Hollow",
    "Brin Gallowcap": "Glowcap Ring",
    "Old Hob": "Spore Bridge",
}


def _ctx(
    app: FastAPI,
    campaign_id: int,
    speak: Callable[[str, str, str], Any] | None = None,
) -> ToolContext:
    async def quiet_speak(speaker: str, profile: str, intent: str) -> None:
        return None

    return ToolContext(
        world=world.WorldTools(
            engine=app.state.engine,
            campaign_id=campaign_id,
            tables=TABLES,
            rng=random.Random(0),
        ),
        speak=speak or quiet_speak,
    )


def _tool(app: FastAPI, campaign_id: int, name: str, arguments: dict[str, Any], speak=None) -> dict:
    request = ToolRequest(id="call_1", name=name, arguments=json.dumps(arguments))
    run = asyncio.run(run_tool(TOOLS, request, None, _ctx(app, campaign_id, speak)))
    assert isinstance(run, ToolRun)
    return json.loads(run.output)


def _db(app: FastAPI) -> Session:
    return Session(app.state.engine, expire_on_commit=False)


def _state(app: FastAPI, campaign_id: int) -> CampaignState:
    with _db(app) as db:
        return load_state(db, campaign_id)


def _actors(app: FastAPI, campaign_id: int) -> dict[str, Character]:
    return {c.name: c for c in _state(app, campaign_id).characters.values()}


def test_player_character_starts_at_the_world_start_location(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    pc = _state(app, campaign_id).pc()
    assert pc is not None
    assert pc.name == "Ada"
    assert pc.location == "Mossbell Hollow"


def test_bundled_npcs_are_placed_at_their_locations(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    npcs = {
        c.name: c.location
        for c in _state(app, campaign_id).characters.values()
        if c.kind == NPC_KIND
    }
    assert npcs == BUNDLED_NPCS


def test_npcs_without_listed_traits_get_random_ones(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    actors = _actors(app, campaign_id)
    for name in ("Brin Gallowcap", "Old Hob"):
        npc = actors[name]
        assert npc.personality in TABLES["personality"]
        assert npc.accent in TABLES["accent"]
        assert len(npc.quirks) == world.QUIRKS_PER_NPC
        assert set(npc.quirks) <= set(TABLES["quirk"])
        assert len(set(npc.quirks)) == world.QUIRKS_PER_NPC


def test_listed_npc_traits_are_kept(client_factory: Callable[[], TestClient], app: FastAPI):
    _, campaign_id = _campaign_id(client_factory)
    mara = _actors(app, campaign_id)["Mara Thistledown"]
    assert mara.personality == "warm and protective"
    assert mara.accent == "soft and lilting"
    assert mara.quirks == ["refers to everyone as 'friend'", "speaks in proverbs"]


def test_character_name_cannot_match_an_npc(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, character_name="old hob")
    assert response.status_code == 400
    assert "already used" in response.text


def test_blank_character_name_is_rejected(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    assert _new_campaign(client, character_name="   ").status_code == 400


def test_random_traits_come_from_the_given_tables(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    tables = {
        "personality": ("gruff",),
        "accent": ("burr",),
        "quirk": ("hums", "sniffs", "winks"),
    }
    with _db(app) as db:
        result = world.create_npc(
            db,
            campaign_id,
            tables,
            random.Random(3),
            name="Pip",
            location="Mossbell Hollow",
            description="A tinker.",
        )
        db.commit()
    assert result["personality"] == "gruff"
    assert result["accent"] == "burr"
    assert len(result["quirks"]) == 2
    assert set(result["quirks"]) <= {"hums", "sniffs", "winks"}


def test_too_many_quirks_are_rejected(client_factory: Callable[[], TestClient], app: FastAPI):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db, pytest.raises(InvalidInput):
        world.create_npc(
            db,
            campaign_id,
            TABLES,
            random.Random(0),
            name="Pip",
            location="Mossbell Hollow",
            description="",
            quirks=["x"] * (world.MAX_QUIRKS + 1),
        )


def test_gm_creates_a_location_and_an_npc_there(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    place = _tool(
        app,
        campaign_id,
        "create_location",
        {"name": "Hollow Pond", "description": "Still, cold water."},
    )
    assert place == {"created": "location", "name": "Hollow Pond"}
    npc = _tool(
        app,
        campaign_id,
        "create_npc",
        {
            "name": "Reed",
            "location": "hollow pond",
            "description": "A fisher.",
            "personality": "Patient",
        },
    )
    assert npc["location"] == "Hollow Pond"
    assert npc["personality"] == "Patient"
    assert len(npc["quirks"]) == world.QUIRKS_PER_NPC


def test_location_and_character_names_are_unique(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    error = _tool(
        app, campaign_id, "create_location", {"name": "mossbell hollow", "description": "Again."}
    )
    assert "error" in error
    error = _tool(
        app,
        campaign_id,
        "create_npc",
        {"name": "Ada", "location": "Mossbell Hollow", "description": ""},
    )
    assert "error" in error


def test_npc_needs_a_known_location(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    error = _tool(
        app, campaign_id, "create_npc", {"name": "Reed", "location": "Nowhere", "description": ""}
    )
    assert "no location named 'Nowhere'" in error["error"]


def test_moving_the_player_changes_the_world_state(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    moved = _tool(app, campaign_id, "move_actor", {"name": "Ada", "location": "Glowcap Ring"})
    assert moved == {"moved": "Ada", "location": "Glowcap Ring"}
    text = world.world_state_text(_state(app, campaign_id))
    assert "Player character: Ada, at Glowcap Ring." in text
    assert "- Brin Gallowcap, at Glowcap Ring:" in text


def test_moving_needs_a_known_character_and_location(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    assert (
        "no character named 'Nobody'"
        in _tool(app, campaign_id, "move_actor", {"name": "Nobody", "location": "Glowcap Ring"})[
            "error"
        ]
    )
    assert (
        "no location named 'Nowhere'"
        in _tool(app, campaign_id, "move_actor", {"name": "Ada", "location": "Nowhere"})["error"]
    )


def test_notes_attach_to_the_campaign_a_location_or_a_character(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        world.add_note(db, campaign_id, "Find the lost key.")
        world.add_note(db, campaign_id, "The bridge sways.", subject="Spore Bridge")
        world.add_note(db, campaign_id, "Hob owes Mara a favour.", subject="Old Hob")
        db.commit()
    view = world.snapshot(_state(app, campaign_id))
    assert [(note.subject, note.text) for note in view.notes] == [
        ("Old Hob", "Hob owes Mara a favour."),
        ("Spore Bridge", "The bridge sways."),
        (world.CAMPAIGN_WIDE, "Find the lost key."),
    ]


def test_note_subject_must_exist_and_text_must_not_be_blank(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        with pytest.raises(InvalidInput):
            world.add_note(db, campaign_id, "Hello.", subject="Nobody")
        with pytest.raises(InvalidInput):
            world.add_note(db, campaign_id, "   ")


def test_gm_adds_and_searches_notes_through_tools(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    added = _tool(
        app,
        campaign_id,
        "add_note",
        {"text": "The bridge is unsafe.", "subject": "Spore Bridge"},
    )
    assert added == {"noted": True, "subject": "Spore Bridge"}
    _tool(app, campaign_id, "add_note", {"text": "Hob hides the key."})
    found = _tool(app, campaign_id, "search_notes", {"query": "bridge"})
    assert found == {"notes": [{"subject": "Spore Bridge", "text": "The bridge is unsafe."}]}


def test_search_needs_every_word_and_returns_newest_first(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        world.add_note(db, campaign_id, "The bridge is unsafe.", subject="Spore Bridge")
        world.add_note(db, campaign_id, "Hob hides the key.", subject="Old Hob")
        world.add_note(db, campaign_id, "Find the key.")
        db.commit()
    state = _state(app, campaign_id)
    keys = world.search_notes(state, "key")["notes"]
    both = world.search_notes(state, "hob key")["notes"]
    none = world.search_notes(state, "dragon")["notes"]
    assert [note["text"] for note in keys] == ["Find the key.", "Hob hides the key."]
    assert [note["subject"] for note in both] == ["Old Hob"]
    assert none == []


def test_search_results_are_capped(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        for index in range(world.NOTE_RESULTS + 3):
            world.add_note(db, campaign_id, f"Clue {index}.")
        db.commit()
        found = world.search_notes(load_state(db, campaign_id), "clue")["notes"]
    assert len(found) == world.NOTE_RESULTS


def test_speaker_profile_describes_an_npc_at_the_players_location(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    profile = world.speaker_profile(_state(app, campaign_id), "Mara Thistledown")
    assert profile.splitlines()[0] == "Speaker: Mara Thistledown"
    assert "Personality: warm and protective" in profile
    assert "Location: Mossbell Hollow" in profile


def test_npcs_away_from_the_player_cannot_speak(client_factory: Callable[[], TestClient], app):
    _, campaign_id = _campaign_id(client_factory)
    state = _state(app, campaign_id)
    with pytest.raises(InvalidInput, match="not at the player character's location"):
        world.speaker_profile(state, "Brin Gallowcap")
    with pytest.raises(InvalidInput, match="does not need speaking for"):
        world.speaker_profile(state, "Ada")


def test_speak_tool_queues_dialogue_and_returns_only_an_acknowledgment(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    asked: list[tuple[str, str, str]] = []

    async def speak(speaker: str, profile: str, intent: str) -> None:
        asked.append((speaker, profile, intent))

    result = _tool(
        app,
        campaign_id,
        "speak",
        {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
        speak=speak,
    )
    assert result == {"speaker": "Mara Thistledown", "status": "dialogue queued for the narration"}
    assert len(asked) == 1
    speaker, profile, intent = asked[0]
    assert speaker == "Mara Thistledown"
    assert profile.startswith("Speaker: Mara Thistledown")
    assert intent == "Warn Ada about the bog."


def test_speak_tool_refuses_an_npc_away_from_the_player(
    client_factory: Callable[[], TestClient], app
):
    _, campaign_id = _campaign_id(client_factory)
    asked: list[str] = []

    async def speak(speaker: str, profile: str, intent: str) -> None:
        asked.append(profile)

    result = _tool(
        app, campaign_id, "speak", {"npc": "Old Hob", "intent": "Greet Ada."}, speak=speak
    )
    assert "error" in result
    assert asked == []


def test_campaign_page_lists_places_characters_and_notes(
    client_factory: Callable[[], TestClient], app
):
    client, campaign_id = _campaign_id(client_factory)
    with _db(app) as db:
        world.add_note(db, campaign_id, "The bridge is unsafe.", subject="Spore Bridge")
        db.commit()
    page = client.get(f"/campaigns/{campaign_id}").text
    for text in (
        "Ada",
        "Mara Thistledown",
        "Brin Gallowcap",
        "Old Hob",
        "Mossbell Hollow",
        "Glowcap Ring",
        "Spore Bridge",
        "The bridge is unsafe.",
    ):
        assert text in page


def test_campaign_page_escapes_character_names(client_factory: Callable[[], TestClient]):
    client = _signed_in(client_factory)
    response = _new_campaign(client, character_name="<b>Zed</b>")
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "&lt;b&gt;Zed&lt;/b&gt;" in page
    assert "<b>Zed</b>" not in page


def test_speech_is_a_separate_call_and_only_the_narration_is_saved(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    speak_call = {
        "name": "speak",
        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada about the bog."},
    }
    script = _script(
        {
            "rounds": [[speak_call]],
            "text": "Mara looks up from her kettle.",
            "speech": {"Mara Thistledown": "Mind the bog, dearie."},
        }
    )

    _send(client, campaign_id, script)

    speech_calls = [
        call
        for call in recorder.calls
        if call["messages"] and "Speech step." in str(call["messages"][0]["content"])
    ]
    assert len(speech_calls) == 1
    assert "Speaker: Mara Thistledown" in json.dumps(speech_calls[0]["messages"])
    assert "Intent: Warn Ada about the bog." in json.dumps(speech_calls[0]["messages"])
    assert _saved(app, campaign_id) == [
        ("user", script),
        ("assistant", "Mara looks up from her kettle."),
    ]
    narration = recorder.calls[-1]["messages"]
    assert "Mind the bog, dearie." in json.dumps(narration)


def test_speech_shows_a_writing_dialogue_status(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    script = _script(
        {
            "rounds": [
                [
                    {
                        "name": "speak",
                        "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada."},
                    }
                ]
            ],
            "text": "Mara looks up.",
            "speech": {"Mara Thistledown": "Mind the bog."},
        }
    )
    token = csrf_token(client, "/campaigns")

    response = client.post(
        f"/campaigns/{campaign_id}/messages/stream",
        data={"csrf_token": token, "content": script},
    )

    statuses = [
        json.loads(line)["text"]
        for line in response.text.splitlines()
        if line.strip() and json.loads(line)["type"] == "status"
    ]
    assert "Writing dialogue for Mara Thistledown..." in statuses
    assert "Running speak..." not in statuses
