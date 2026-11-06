"""The turn review checks the actions and narration before the player keeps them.

A sound narration costs one model call and changes nothing. A review that finds a fault undoes
the narration and may fix the record; the narrator then writes the beat again from the corrected
state and the review's reason.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Campaign, Event
from cygnusloom.llm.base import ChatReply
from cygnusloom.services.campaigns import get_turn_context
from cygnusloom.services.events import CampaignState, load_state
from cygnusloom.services.tools import tool_specs
from tests.conftest import content_library, csrf_token, is_review_messages
from tests.test_campaigns import _campaign_id, _place_pc, _send
from tests.test_tools import _recording_provider, _saved, _saved_ids, _script

UNDO = {
    "name": "undo_narration",
    "arguments": {"reason": "Ada has no advancement point left; the tool spent it."},
}
CREATE_GATE = {
    "name": "create_location",
    "arguments": {"name": "Gate Room", "description": "A room behind the gate."},
}
LIST_LOOKUPS = {"name": "list_lookups", "arguments": {}}
LIST_DICE_TABLES = {"name": "list_dice_tables", "arguments": {}}
BAD_UNDO = {"name": "undo_narration", "arguments": {"reason": ""}}
SPEAK_MARA = {
    "name": "speak",
    "arguments": {"npc": "Mara Thistledown", "intent": "Warn them about the bridge."},
}


class _ReviewProvider:
    """The recording fake, with the text the fake leaves out: a review's own words, and the
    prose a state step writes alongside a tool call. Both are what the review and the rewrite
    must carry between them."""

    name = "fake"

    def __init__(
        self,
        inner: Any,
        *,
        fault: str = "",
        verdict: str = "",
        state_text: str = "",
    ) -> None:
        self.inner = inner
        self.fault = fault
        self.verdict = verdict
        self.state_text = state_text

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        reply = await self.inner.chat(model, messages, tools)
        if is_review_messages(messages):
            text = self.fault if reply.tool_requests else self.verdict
            return ChatReply(
                text=text, tool_requests=reply.tool_requests, reasoning=reply.reasoning
            )
        if self.state_text and tools is not None and reply.tool_requests:
            return ChatReply(
                text=self.state_text,
                tool_requests=reply.tool_requests,
                reasoning=reply.reasoning,
            )
        return reply

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        async for item in self.inner.stream(model, messages, tools):
            yield item


def _state(app: FastAPI, campaign_id: int) -> CampaignState:
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id)


def _narration_calls(recorder: Any, script: str) -> list[dict[str, Any]]:
    """The narration calls for the test's own message, not an opening the recorder also saw."""
    return [
        call
        for call in recorder.calls
        if call["tools"] is None and any(m.get("content") == script for m in call["messages"])
    ]


def _review_calls(recorder: Any) -> list[dict[str, Any]]:
    return [call for call in recorder.calls if is_review_messages(call["messages"])]


def _stream(client: TestClient, campaign_id: int, text: str) -> list[dict[str, Any]]:
    token = csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/messages/stream",
        data={"csrf_token": token, "content": text},
    )
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _deltas(events: list[dict[str, Any]]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


def _undone_events(app: FastAPI, campaign_id: int) -> list[Event]:
    with Session(app.state.engine) as db:
        return list(
            db.exec(
                select(Event).where(
                    Event.campaign_id == campaign_id, Event.kind == "narration.undone"
                )
            ).all()
        )


def test_a_sound_narration_passes_review_and_is_saved_once(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)

    script = _script({"rounds": [[]], "text": "Ada waits."})
    assert _send(client, campaign_id, script).status_code == 303

    narration_calls = _narration_calls(recorder, script)
    assert len(narration_calls) == 1
    reviews = _review_calls(recorder)
    assert len(reviews) == 1
    review = reviews[0]
    assert "Ada waits." in json.dumps(review["messages"])
    offered = {spec["function"]["name"] for spec in review["tools"]}
    assert "undo_narration" in offered
    assert {"append_journal", "replace_in_journal"} <= offered
    assert _saved(app, campaign_id)[-1] == ("assistant", "Ada waits.")
    assert _undone_events(app, campaign_id) == []


def test_an_undone_narration_is_retracted_and_written_again(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[]],
            "text": "Ada still has one advancement point to spend.",
            "rewrite": "Ada has no advancement point left.",
            "review": [[[UNDO]]],
        }
    )

    events = _stream(client, campaign_id, script)

    assert [e["type"] for e in events].count("retract") == 1
    retract_at = next(i for i, e in enumerate(events) if e["type"] == "retract")
    assert _deltas(events[:retract_at]) == "Ada still has one advancement point to spend."
    assert _deltas(events[retract_at:]) == "Ada has no advancement point left."
    assert events[-1]["type"] == "saved"
    assert _saved(app, campaign_id)[-1] == ("assistant", "Ada has no advancement point left.")

    narration_calls = _narration_calls(recorder, script)
    assert len(narration_calls) == 2
    assert "The last narration was set aside because:" in json.dumps(narration_calls[1])
    # The undo ends its review, so the only other review call is the rewrite's pass.
    assert len(_review_calls(recorder)) == 2
    undone = _undone_events(app, campaign_id)
    assert len(undone) == 1
    assert json.loads(undone[0].payload)["reason"] == UNDO["arguments"]["reason"]


def test_a_review_cannot_change_the_record_before_undoing(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[]],
            "text": "The gate is open.",
            "rewrite": "The gate opens onto a small room.",
            "review": [[[CREATE_GATE]]],
        }
    )

    assert _send(client, campaign_id, script).status_code == 303

    # A record change before undo_narration is refused, so a passing narration is not sent back.
    assert "gate room" not in _state(app, campaign_id).places
    assert len(_narration_calls(recorder, script)) == 1
    assert _saved(app, campaign_id)[-1] == ("assistant", "The gate is open.")
    assert _undone_events(app, campaign_id) == []


def test_a_review_that_moves_a_token_does_not_rewrite_or_move_it(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    # A review that fixes a cosmetic record detail used to send a passing narration back. A
    # record change without undo is now refused, so the narration and the token both stand.
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    move = {
        "name": "place_map_token",
        "arguments": {
            "map": "The Ninth Bell local map",
            "name": "Brenna Vosk",
            "column": 7,
            "row": 7,
        },
    }
    script = _script(
        {
            "rounds": [[]],
            "text": "Brenna steps up beside you.",
            "rewrite": "Brenna is beside you.",
            "review": [[[move]]],
        }
    )

    events = _stream(client, campaign_id, script)

    assert [e["type"] for e in events].count("retract") == 0
    assert len(_narration_calls(recorder, script)) == 1
    assert _saved(app, campaign_id)[-1] == ("assistant", "Brenna steps up beside you.")
    assert _undone_events(app, campaign_id) == []


def test_a_review_can_undo_and_fix_the_record_before_the_rewrite(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[]],
            "text": "Ada stands in an empty hall.",
            "rewrite": "Ada finds a gate room behind the hall.",
            "review": [[[UNDO, CREATE_GATE]]],
        }
    )

    assert _send(client, campaign_id, script).status_code == 303

    assert "gate room" in _state(app, campaign_id).places
    assert _saved(app, campaign_id)[-1] == (
        "assistant",
        "Ada finds a gate room behind the hall.",
    )
    assert len(_undone_events(app, campaign_id)) == 1


def test_review_offers_the_usual_tools_and_the_journal_tools(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)

    script = _script({"rounds": [[]], "text": "Ada waits."})
    assert _send(client, campaign_id, script).status_code == 303

    assert len(_narration_calls(recorder, script)) == 1
    review = _review_calls(recorder)[0]
    offered = {spec["function"]["name"] for spec in review["tools"]}
    assert {
        "create_location",
        "get_statblock",
        "add_note",
        "move_actor",
        "undo_narration",
        "append_journal",
        "replace_in_journal",
    } <= offered
    assert {"speak", "ooc"} & offered == set()


def test_review_tools_keep_wwn_world_methods_and_drop_speech_and_ooc():
    rule_set = content_library().rule_sets["worldswithoutnumber"]
    offered = {
        spec["function"]["name"] for spec in tool_specs(rule_set.tool_calls, review_step=True) or []
    }
    assert {"undo_narration", "damage_faction_asset"} <= offered
    assert {"speak", "ooc"} & offered == set()


def test_the_review_sees_the_record_and_speech_but_not_the_state_step_draft(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _place_pc(client, campaign_id)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (
        _ReviewProvider(recorder, state_text="I weigh the warning before narrating."),
        model_id,
    )
    script = _script(
        {
            "rounds": [[SPEAK_MARA]],
            "speech": {"Mara Thistledown": "The bridge is out."},
            "text": "Mara warns you about the bridge.",
        }
    )
    assert _send(client, campaign_id, script).status_code == 303

    review = _review_calls(recorder)[0]
    blob = json.dumps(review["messages"])
    # The state step's tool call and its result reach the review through the turn messages.
    assert '"name": "speak"' in blob
    assert "dialogue queued for the narration" in blob
    # The state step's prose is a draft, not the record, so the review does not read it.
    assert "I weigh the warning before narrating." not in blob
    # The review must see the speech step's profile and intent to judge the dialogue.
    assert "Speech step (round 1)" in blob
    assert "Who they are: The village elder" in blob
    assert "Intent: Warn them about the bridge." in blob
    # The narration instruction tells the narrator not to call tools; the review must not see it.
    assert "Do not call tools." not in blob


def test_the_rewrite_sees_the_reviewers_fault_text(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    fault = "The narration invents an advancement point Ada does not have."
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (_ReviewProvider(recorder, fault=fault), model_id)
    script = _script(
        {
            "rounds": [[]],
            "text": "Ada still has one advancement point to spend.",
            "rewrite": "Ada has no advancement point left.",
            "review": [[[UNDO]]],
        }
    )

    assert _send(client, campaign_id, script).status_code == 303

    narration_calls = _narration_calls(recorder, script)
    assert len(narration_calls) == 2
    rewrite = json.dumps(narration_calls[1])
    assert fault in rewrite
    assert "Turn review fault (round 1)" in rewrite
    undone = _undone_events(app, campaign_id)
    assert len(undone) == 1
    # The reviewer's explicit undo reason is kept; its whole review is a context note.
    assert json.loads(undone[0].payload)["reason"] == UNDO["arguments"]["reason"]


def test_a_narration_that_writes_a_tool_call_is_retried_and_not_saved(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    markup = (
        "<｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="speak">\n'
        '<｜｜DSML｜｜ parameter name="intent" string="true">Warn them.'
        "</｜｜DSML｜ ｜ parameter>\n"
        "</｜｜DSML｜ ｜ invoke>\n"
        "</｜｜DSML｜｜ calls>"
    )
    script = _script(
        {
            "rounds": [[]],
            "text": markup,
            "retry": "Mara warns you about the bridge.",
        }
    )

    events = _stream(client, campaign_id, script)

    assert [e["type"] for e in events].count("retract") == 1
    assert _deltas(events).endswith("Mara warns you about the bridge.")
    assert _saved(app, campaign_id)[-1] == ("assistant", "Mara warns you about the bridge.")
    # The malformed attempt is never reviewed or saved.
    review = _review_calls(recorder)[0]
    assert markup not in json.dumps(review["messages"])


def test_a_narration_that_is_always_a_tool_call_fails_without_saving_it(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    markup = 'speak({"npc": "Mara Thistledown", "intent": "Warn them about the bridge."})'
    script = _script({"rounds": [[]], "text": markup})

    response = _send(client, campaign_id, script)

    assert response.status_code == 502
    assert "wrote a tool call instead of the narration" in response.text
    assert all(markup not in text for role, text in _saved(app, campaign_id) if role == "assistant")


def test_a_malformed_undo_still_retracts_with_a_fallback_reason(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    # A blank undo with no review prose cannot run, but the narration still must not stand.
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[]],
            "text": "A wrong narration.",
            "rewrite": "A corrected narration.",
            "review": [[[BAD_UNDO]]],
        }
    )

    events = _stream(client, campaign_id, script)

    assert [e["type"] for e in events].count("retract") == 1
    assert _saved(app, campaign_id)[-1] == ("assistant", "A corrected narration.")
    assert len(_narration_calls(recorder, script)) == 2
    undone = _undone_events(app, campaign_id)
    assert len(undone) == 1
    assert json.loads(undone[0].payload)["reason"] == "the narration did not match the record"


def test_a_passing_review_records_its_verdict_in_the_turn_context(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    verdict = "The turn passes: the record and the narration agree."
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (_ReviewProvider(recorder, verdict=verdict), model_id)
    script = _script({"rounds": [[]], "text": "Ada waits."})
    assert _send(client, campaign_id, script).status_code == 303

    reply_id = _saved_ids(app, campaign_id)[-1]
    with Session(app.state.engine) as db:
        owner_id = db.get(Campaign, campaign_id).owner_id
    parts = get_turn_context(app.state.engine, owner_id, campaign_id, reply_id)
    verdicts = [part for part in parts if part.source.startswith("Turn review verdict")]
    assert [(part.source, part.content) for part in verdicts] == [
        ("Turn review verdict (round 1)", verdict)
    ]
    with Session(app.state.engine) as db:
        steps = list(
            db.exec(
                select(Event).where(
                    Event.campaign_id == campaign_id, Event.kind == "turn.review_step"
                )
            ).all()
        )
    assert any(json.loads(step.payload)["messages"] == [] for step in steps)


@pytest.mark.parametrize("call", [LIST_LOOKUPS, LIST_DICE_TABLES])
def test_a_read_only_list_tool_does_not_rewrite_the_narration(
    client_factory: Callable[[], TestClient], app: FastAPI, call: dict[str, Any]
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[]],
            "text": "Ada waits.",
            "rewrite": "Ada waits, rewritten.",
            "review": [[[call]]],
        }
    )

    events = _stream(client, campaign_id, script)

    assert [e["type"] for e in events].count("retract") == 0
    assert _saved(app, campaign_id)[-1] == ("assistant", "Ada waits.")
    assert len(_narration_calls(recorder, script)) == 1
    assert _undone_events(app, campaign_id) == []
