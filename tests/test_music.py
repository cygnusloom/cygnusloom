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
    MOODS,
    MUSIC_DIR,
    MusicView,
    derive_scene_mood,
    load_manifest,
    music_view,
    scene_key,
    validate_manifest,
)
from cygnusloom.services.prompts import state_instruction
from tests.conftest import bundled_library
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


def test_the_bundled_pack_is_valid_and_covers_every_mood_but_silence():
    manifest = load_manifest()

    assert validate_manifest(manifest) == []
    covered = {mood for track in manifest["tracks"] for mood in track["moods"]}
    assert set(MOODS) - covered == {"silence"}
    assert len(manifest["tracks"]) >= 40
    assert all("intensity" not in track for track in manifest["tracks"])
    for mood in MOODS:
        if mood == "silence":
            continue
        count = sum(1 for track in manifest["tracks"] if mood in track["moods"])
        assert count >= 4, f"only {count} tracks for the {mood} mood"
    total = sum((MUSIC_DIR / track["file"]).stat().st_size for track in manifest["tracks"])
    assert total < 100 * 1024 * 1024


def test_combat_is_derived_during_an_encounter():
    state = _state(encounter=Encounter(map="Camp", location="Mossbell Hollow"))

    assert derive_scene_mood(state) == MusicView("combat", "auto")
    state.characters[name_key("Ada")].defeated = True
    assert derive_scene_mood(state) == MusicView("combat", "auto")


def test_a_music_view_has_a_player_facing_label():
    assert MusicView("exploration", "auto").label == "Exploration"
    assert MusicView("silence", "gm").label == "Silence"


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


def test_every_shipped_rule_set_tells_the_gm_to_cue_music():
    """Music is handled by its own core prompt, not the turn checklist. The GM cues a mood
    only when the scene changes, and the engine derives combat, social, and night moods."""
    for rule_set in bundled_library().rule_sets.values():
        text = state_instruction(rule_set)
        assert "Background music follows the mood of the scene" in text
        assert "set_music" in text


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
    assert "data-music-intensity" not in page
    assert 'data-music-manifest="/static/music/manifest.json"' in page
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


def test_the_page_has_player_controls_and_the_license_page_has_music_credits(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert "data-music-toggle" in page
    assert "data-music-volume" in page
    assert "data-music-ticker" in page
    assert "data-music-menu-toggle" in page
    assert "data-music-mood-label" in page
    assert "data-music-skip" in page
    assert 'data-music-default-enabled="1"' in page
    assert 'id="settings-music-title"' in page
    assert "Play background music" in page

    license_page = client.get("/license").text
    assert "Music" in license_page
    assert "Scott Buckley" in license_page


def test_a_test_server_can_mute_music_by_default(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    app.state.settings = replace(app.state.settings, disable_music=True)

    page = client.get(f"/campaigns/{campaign_id}").text

    assert 'data-music-default-enabled="0"' in page
