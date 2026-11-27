"""Background music: the licensed pack, mood derivation, the GM cue, and the player page."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.library import ClockRules
from cygnusloom.services.events import (
    PC_KIND,
    CampaignState,
    Character,
    Clock,
    Encounter,
    MusicCue,
    MusicCued,
    Place,
    decode,
    events_since,
    is_hidden,
    name_key,
)
from cygnusloom.services.music import (
    MusicView,
    derive_scene_mood,
    music_view,
    scene_key,
)
from tests.factories import campaign_ready_fast, send_message


def _character(**overrides) -> Character:
    character = Character(
        kind=PC_KIND,
        name="Ada",
        location="Mossbell Hollow",
        description="A traveller.",
        personality="",
        accent="",
        quirks=[],
    )
    for name, value in overrides.items():
        setattr(character, name, value)
    return character


def _state(**overrides) -> CampaignState:
    place = Place(name="Mossbell Hollow", description="A small town.", icon="town")
    pc = _character()
    state = CampaignState(
        owner_id=1,
        places={name_key(place.name): place},
        characters={name_key(pc.name): pc},
    )
    for name, value in overrides.items():
        setattr(state, name, value)
    return state


def _tool_script(name: str, arguments: dict[str, object]) -> str:
    return "__FAKE__:" + json.dumps(
        {
            "turns": [
                {
                    "rounds": [[{"name": name, "arguments": arguments}]],
                    "text": "The world answers.",
                }
            ]
        }
    )


def _tool_results(app: FastAPI, campaign_id: int) -> list[str]:
    with Session(app.state.engine) as db:
        rows = [decode(row) for row in events_since(db, campaign_id)]
    return [
        message["content"]
        for event in rows
        if hasattr(event, "messages")
        for message in event.messages
        if message.get("role") == "tool"
    ]


def _music_events(app: FastAPI, campaign_id: int) -> list[MusicCued]:
    with Session(app.state.engine) as db:
        return [
            event
            for event in (decode(row) for row in events_since(db, campaign_id))
            if isinstance(event, MusicCued)
        ]


def test_combat_is_derived_during_an_encounter():
    state = _state(encounter=Encounter(map="Camp", location="Mossbell Hollow"))

    assert derive_scene_mood(state) == MusicView("combat", "auto")
    state.characters[name_key("Ada")].defeated = True
    assert derive_scene_mood(state) == MusicView("combat", "auto")


def test_places_and_night_suggest_a_mood():
    state = _state()
    assert derive_scene_mood(state) == MusicView("social", "auto")

    state.places[name_key("Mossbell Hollow")] = Place(
        name="Mossbell Hollow", description="", icon="forest"
    )
    clock = ClockRules(parts=("morning", "night"), stale_days=1)
    state.clock = Clock(day=1, part=1)
    assert derive_scene_mood(state, clock) == MusicView("tension", "auto")


def test_a_gm_cue_lasts_for_its_scene_and_expires_when_the_scene_moves():
    state = _state()
    state.music = MusicCue("social", "gm", scene=scene_key(state))
    assert music_view(state) == MusicView("social", "gm")

    state.clock = Clock(day=2, part=0)
    assert music_view(state) != MusicView("social", "gm")


def test_combat_overrides_a_stale_cue_and_silence_is_kept():
    state = _state()
    state.music = MusicCue("social", "gm", scene=scene_key(state))
    state.encounter = Encounter(map="Camp", location="Mossbell Hollow")
    assert music_view(state) == MusicView("combat", "auto")

    state.encounter = None
    state.music = MusicCue("silence", "gm", scene=scene_key(state))
    assert music_view(state) == MusicView("silence", "gm")


def test_a_combat_cue_does_not_linger_after_the_encounter():
    state = _state(encounter=Encounter(map="Camp", location="Mossbell Hollow"))
    state.music = MusicCue("combat", "gm", scene=scene_key(state))
    assert music_view(state) == MusicView("combat", "gm")

    state.encounter = None
    assert music_view(state).mood != "combat"


def test_sorrow_is_for_a_party_with_no_character_left_in_play():
    state = _state()
    state.characters[name_key("Ada")].dead = True
    assert music_view(state) == MusicView("sorrow", "auto")

    replacement = _character(name="Bram")
    state.characters[name_key("Bram")] = replacement
    assert music_view(state).mood != "sorrow"


def test_music_cues_are_hidden_from_the_transcript():
    assert is_hidden("music.cued")


def test_the_gm_can_cue_music_and_the_page_renders_it(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)

    response = send_message(
        client,
        campaign_id,
        _tool_script("set_music", {"mood": "tension"}),
    )

    assert response.status_code == 303
    cues = _music_events(app, campaign_id)
    assert cues and cues[-1].mood == "tension"
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-music-mood="tension"' in page
    assert "data-music-mood-label>Tension<" in page


def test_an_unknown_mood_is_refused_without_recording_a_cue(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)

    response = send_message(
        client,
        campaign_id,
        _tool_script("set_music", {"mood": "dance"}),
    )

    assert response.status_code == 303
    assert not _music_events(app, campaign_id)
    assert any("mood" in result for result in _tool_results(app, campaign_id))


def test_repeating_the_same_cue_in_the_same_scene_does_not_fill_the_log(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    script = _tool_script("set_music", {"mood": "tension"})

    assert send_message(client, campaign_id, script).status_code == 303
    assert send_message(client, campaign_id, script).status_code == 303

    assert len(_music_events(app, campaign_id)) == 1


def test_a_test_server_can_mute_music_by_default(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app.state.settings = replace(app.state.settings, disable_music=True)

    page = client.get(f"/campaigns/{campaign_id}").text

    assert 'data-music-default-enabled="0"' in page
