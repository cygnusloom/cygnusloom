"""A GM-played hireling's rolls never become a prompt for the player."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.config import Settings
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services import turns as turns_service
from cygnusloom.services.events import (
    CharacterSaved,
    CheckMade,
    DebugInjectionRan,
    PromptShown,
    decode,
    events_since,
)
from tests.factories import send_message
from tests.test_campaigns import _debug_client_factory
from tests.test_debug_injection import _inject
from tests.test_encounters import _npc_turn_stream
from tests.test_hireling_turns import HIRELING, _begin, _end, _set_flag, _state
from tests.test_wwn_combat import MAP, PC, YARD, _foe, _wwn_campaign

WWN_CHOICES = {
    "templates": {"class": "warrior", "tradition": "no_tradition"},
    "stats": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10},
}


def _setup_fight(
    client_factory: Callable[[], TestClient],
    *,
    gm_plays: bool,
    choices: dict | None = None,
) -> tuple[TestClient, int]:
    """A WWN fight with Wen hired and joined to the party side before initiative is rolled."""
    client, campaign_id, tools = _wwn_campaign(client_factory)
    tools.create_location(YARD, "Cobbles around a dry well.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(MAP, YARD, 10, 10, "Open cobbles.")
    tools.start_encounter(MAP)
    tools.add_participant(PC)
    tools.hire_hireling(
        HIRELING,
        PC,
        0,
        "A hired hand, good with a rope.",
        character=choices or WWN_CHOICES,
    )
    tools.add_participant(HIRELING)
    _foe(tools)
    if gm_plays:
        _set_flag(client.app, campaign_id, True)
    return client, campaign_id


def _play_until_the_players_turn(client: TestClient, campaign_id: int) -> None:
    """Play the foe's turn when its side won initiative, so the player character's turn runs."""
    app = client.app
    state = _state(app, campaign_id)
    assert state.encounter is not None
    if state.encounter.acting is None and state.encounter.order[state.encounter.up] != PC:
        foe = state.encounter.order[state.encounter.up]
        assert _npc_turn_stream(client, campaign_id, foe)[-1]["type"] == "saved"
        state = _state(app, campaign_id)
    if state.encounter.acting != PC:
        assert state.encounter.acting is None
        assert state.encounter.order[state.encounter.up] == PC
        _begin(app, campaign_id, PC)


def _play_until_the_hirelings_turn(client: TestClient, campaign_id: int) -> None:
    """Reach the hireling's turn, which a GM-played campaign leaves waiting for Start."""
    _play_until_the_players_turn(client, campaign_id)
    _end(client.app, campaign_id, PC, by_player=True)
    state = _state(client.app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.order[state.encounter.up] == HIRELING


def _script(*turns: dict) -> str:
    return "__FAKE__:" + json.dumps({"turns": list(turns)})


def _check_and_save_round() -> list[dict]:
    return [
        {
            "name": "roll_check",
            "arguments": {
                "prompt": "Wen, read the ground.",
                "character": HIRELING,
                "skill": "notice",
                "stat": "wis",
                "difficulty": 8,
                "cause": "reading the ground",
            },
        },
        {
            "name": "player_roll_save",
            "arguments": {
                "prompt": "Wen, keep your footing.",
                "character": HIRELING,
                "save": "physical",
                "cause": "loose stones",
            },
        },
    ]


def _events(app: FastAPI, campaign_id: int) -> list:
    with Session(app.state.engine) as db:
        return [decode(row) for row in events_since(db, campaign_id)]


def _owner(app: FastAPI, campaign_id: int) -> int:
    state = _state(app, campaign_id)
    owner = state.owner_of(state.pc())
    assert owner is not None
    return owner


def test_a_gm_played_hirelings_checks_and_saves_roll_without_prompting_the_player(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _setup_fight(client_factory, gm_plays=True)
    app = client.app
    _play_until_the_hirelings_turn(client, campaign_id)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert f"Start {HIRELING}'s turn" in page

    script = _script(
        {"text": "Wen waits for the word."},
        {"rounds": [_check_and_save_round()], "text": "Wen reads the cobbles and holds her feet."},
    )
    assert send_message(client, campaign_id, script).status_code == 303

    lines = _npc_turn_stream(client, campaign_id, HIRELING)

    assert [line for line in lines if line["type"] == "prompt"] == []
    assert lines[-1]["type"] == "saved"
    state = _state(app, campaign_id)
    assert state.turn is None
    assert state.encounter is not None and state.encounter.acting is None
    events = _events(app, campaign_id)
    assert [event.name for event in events if isinstance(event, CheckMade)] == [HIRELING]
    assert [event.name for event in events if isinstance(event, CharacterSaved)] == [HIRELING]
    assert not any(isinstance(event, PromptShown) for event in events)


def test_a_player_played_hirelings_check_still_waits_for_the_player(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _setup_fight(client_factory, gm_plays=False)
    app = client.app
    _play_until_the_hirelings_turn(client, campaign_id)
    state = _state(app, campaign_id)
    # The hireling's turn belongs to the player, so it started by itself.
    assert state.encounter is not None
    assert state.encounter.acting == HIRELING
    assert state.encounter.gm_played is False

    script = _script(
        {
            "rounds": [[_check_and_save_round()[0]]],
            "text": "Wen reads the ground.",
        }
    )
    assert send_message(client, campaign_id, script).status_code == 303

    pending = turns_service.pending_turn(app.state.engine, _owner(app, campaign_id), campaign_id)
    assert pending is not None and pending.prompt is not None
    assert pending.prompt.text == "Wen, read the ground."
    assert pending.prompt.character == HIRELING


def test_a_hireling_the_gm_plays_rolls_its_check_outside_its_turn_too(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _setup_fight(client_factory, gm_plays=True)
    app = client.app
    _play_until_the_players_turn(client, campaign_id)

    script = _script(
        {
            "rounds": [[_check_and_save_round()[0]]],
            "text": "Wen reads the ground while the others move.",
        }
    )
    assert send_message(client, campaign_id, script).status_code == 303

    pending = turns_service.pending_turn(app.state.engine, _owner(app, campaign_id), campaign_id)
    assert pending is None
    events = _events(app, campaign_id)
    assert [event.name for event in events if isinstance(event, CheckMade)] == [HIRELING]


def test_a_gm_played_hirelings_reroll_reaction_is_rolled_by_the_engine(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _setup_fight(
        client_factory, gm_plays=True, choices={**WWN_CHOICES, "features": ["focus_lucky"]}
    )
    app = client.app
    _play_until_the_players_turn(client, campaign_id)

    script = _script(
        {
            "rounds": [[_check_and_save_round()[0]]],
            "text": "Wen reads the ground while the others move.",
        }
    )
    assert send_message(client, campaign_id, script).status_code == 303

    pending = turns_service.pending_turn(app.state.engine, _owner(app, campaign_id), campaign_id)
    assert pending is None
    events = _events(app, campaign_id)
    checks = [event for event in events if isinstance(event, CheckMade)]
    # The first check is the roll that triggered Lucky; the second records its reroll.
    assert [event.name for event in checks] == [HIRELING, HIRELING]
    assert checks[-1].reaction == "Lucky already rolled the check twice and kept the better result."


def test_the_debug_panel_can_inject_a_roll_for_a_gm_played_hireling(
    settings: Settings, fake: FakeProvider
):
    client, campaign_id = _setup_fight(_debug_client_factory(settings, fake), gm_plays=True)
    app = client.app
    _play_until_the_hirelings_turn(client, campaign_id)

    response = _inject(
        client,
        campaign_id,
        "tool",
        tool="player_roll_save",
        arguments=json.dumps(
            {
                "prompt": "Wen, keep your footing.",
                "character": HIRELING,
                "save": "evasion",
                "cause": "loose stones",
            }
        ),
    )

    assert response.status_code == 303
    lines = _npc_turn_stream(client, campaign_id, HIRELING)
    assert [line for line in lines if line["type"] == "prompt"] == []
    assert lines[-1]["type"] == "saved"
    events = _events(app, campaign_id)
    assert any(isinstance(event, DebugInjectionRan) for event in events)
    assert [event.name for event in events if isinstance(event, CharacterSaved)] == [HIRELING]


def test_a_prompt_named_for_an_npc_rolls_in_the_engine_instead(
    client_factory: Callable[[], TestClient],
):
    """The GM asks the player to roll only for a player's character. A prompt tool that names
    an NPC records the engine's roll instead."""
    client, campaign_id = _setup_fight(client_factory, gm_plays=False)
    app = client.app
    state = _state(app, campaign_id)
    assert state.encounter is not None
    if state.encounter.acting is None and state.encounter.order[state.encounter.up] != PC:
        foe = state.encounter.order[state.encounter.up]
        assert _npc_turn_stream(client, campaign_id, foe)[-1]["type"] == "saved"
        state = _state(app, campaign_id)
    if state.encounter.acting != PC:
        _begin(app, campaign_id, PC)
    foe = next(name for name in state.encounter.order if name != PC and name != HIRELING)

    script = _script(
        {
            "rounds": [
                [
                    {
                        "name": "player_roll_save",
                        "arguments": {
                            "prompt": "The hound, keep your footing.",
                            "character": foe,
                            "save": "evasion",
                            "cause": "loose stones",
                        },
                    }
                ]
            ],
            "text": "Loose stones rattle underfoot.",
        }
    )
    assert send_message(client, campaign_id, script).status_code == 303

    pending = turns_service.pending_turn(app.state.engine, _owner(app, campaign_id), campaign_id)
    assert pending is None
    events = _events(app, campaign_id)
    assert [event.name for event in events if isinstance(event, CharacterSaved)] == [foe]
