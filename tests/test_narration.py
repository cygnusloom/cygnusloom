"""Tests for what the GM narrator is given to narrate from. Each test is a fact the player must hear,
or a piece of text the narrator must not treat as settled."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.services.campaigns import _without_draft
from cygnusloom.services.events import Channel, decode, events_since, is_hidden, last_event_id
from cygnusloom.services.prompts import system_parts
from tests.conftest import content_library
from tests.test_campaigns import _answer, _campaign_id, _place_pc, _send
from tests.test_tools import ROLL, _recording_provider, _script


def _narrator(recorder: Any) -> list[dict[str, Any]]:
    """The messages of the narration, which is the turn's last provider call."""
    return recorder.calls[-1]["messages"]


def _visible_log(app: FastAPI, campaign_id: int, after: int) -> list[str]:
    """The Log lines the player is shown for events after `after`."""
    with Session(app.state.engine) as db:
        rows = events_since(db, campaign_id, after, Channel.LOG)
    lines = []
    for row in rows:
        if is_hidden(row.kind):
            continue
        text = decode(row).render()
        if text is not None:
            lines.append(text)
    return lines


def _system_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


def test_every_log_line_the_player_sees_is_in_the_narration(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    with Session(app.state.engine) as db:
        before = last_event_id(db, campaign_id)
    _send(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Well rolled."}))

    _answer(client, campaign_id, "roll")

    shown = _visible_log(app, campaign_id, before)
    assert shown, "the roll should put at least one line in the Log"
    narrator = json.dumps(_narrator(recorder), ensure_ascii=False)
    missing = [line for line in shown if line not in narrator]
    assert not missing, f"the narrator was not given these Log lines: {missing}"
    assert "Turn log, in order:\n1. " in _system_text(_narrator(recorder))


def test_tool_call_messages_lose_their_draft_prose_for_the_narrator():
    call = {"role": "assistant", "content": "The wolf lies dead at your feet.", "tool_calls": [{}]}

    blanked = _without_draft(call)

    assert blanked["content"] is None
    assert blanked["tool_calls"] == call["tool_calls"]
    assert call["content"] == "The wolf lies dead at your feet."
    plain = {"role": "assistant", "content": "Settled narration."}
    assert _without_draft(plain) is plain


def test_world_changes_made_this_turn_are_in_the_narrators_system_context(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    _place_pc(client, campaign_id)
    npc = {
        "name": "create_npc",
        "arguments": {
            "name": "Brother Anselm",
            "location": "Mossbell Hollow",
            "description": "A thin monk with ink on his fingers.",
        },
    }
    _send(client, campaign_id, _script({"rounds": [[npc]], "text": "Anselm watches you."}))

    assert "Brother Anselm" in _system_text(_narrator(recorder))


def test_queued_dialogue_reaches_the_narration_verbatim(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    _place_pc(client, campaign_id)
    npc = {
        "name": "create_npc",
        "arguments": {
            "name": "Brother Anselm",
            "location": "Mossbell Hollow",
            "description": "A thin monk with ink on his fingers.",
        },
    }
    speak = {"name": "speak", "arguments": {"npc": "Brother Anselm", "intent": "Warn them."}}
    script = _script(
        {
            "rounds": [[npc], [speak]],
            "speech": {"Brother Anselm": "Beware the road past the mill."},
            "text": "Anselm lowers his voice.",
        }
    )
    _send(client, campaign_id, script)

    assert "Beware the road past the mill." in _system_text(_narrator(recorder))


def test_failed_tool_calls_reach_the_narration(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    bad = {
        "name": "social_roll",
        "arguments": {"prompt": "Roll.", "relationship": "high", "circumstance": 0},
    }
    _send(client, campaign_id, _script({"rounds": [[bad]], "text": "The attempt fails."}))

    tool_messages = [m for m in _narrator(recorder) if m["role"] == "tool"]
    assert tool_messages and "must be integer" in tool_messages[0]["content"]


def test_narrator_is_told_the_player_character_moves_only_when_a_move_is_recorded(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    _place_pc(client, campaign_id)
    _send(client, campaign_id, _script({"rounds": [[]], "text": "The lane is quiet."}))

    system = _system_text(_narrator(recorder))
    assert "only when a move_actor result records it" in system


def test_narrator_is_told_not_to_invent_times_distances_or_street_names(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    _place_pc(client, campaign_id)
    _send(client, campaign_id, _script({"rounds": [[]], "text": "The lane is quiet."}))

    system = _system_text(_narrator(recorder))
    assert "Never write a number of minutes, hours, days, or distance" in system
    assert "Name no street, square, alley, or route" in system


def test_narrator_keeps_routine_beats_under_sixty_words(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    _place_pc(client, campaign_id)
    _send(client, campaign_id, _script({"rounds": [[]], "text": "The lane is quiet."}))

    system = _system_text(_narrator(recorder))
    assert "takes two or three sentences (under 60 words)" in system
    assert "including arriving at or discovering a place" in system
    assert "or a brief exchange of dialogue" in system


def test_present_tense_narration_is_an_important_core_rule() -> None:
    """The present-tense rule comes from the core rule set and is given in full to a ruleset
    that does not replace it."""
    rule_set = content_library().rule_sets["cairn2e"]
    assert "narration" in rule_set.important
    parts = system_parts(rule_set.id, rule_set.rules, rule_set.important, "", 0)
    system = "\n".join(part.content for part in parts)
    assert "- narration: Narrate in the present tense" in system
