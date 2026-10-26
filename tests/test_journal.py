from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusforge.db.models import Event
from cygnusforge.llm.base import ChatReply
from cygnusforge.services.campaigns import OPENING_PROMPT
from cygnusforge.services.events import CampaignState, load_state
from tests.conftest import is_journal_messages
from tests.test_campaigns import (
    _answer,
    _campaign_id,
    _create_character,
    _new_campaign,
    _send,
    _signed_in,
)
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_tools import _recording_provider, _saved, _script
from tests.test_world import _tool


def _journal_state(app: FastAPI, campaign_id: int) -> CampaignState:
    with Session(app.state.engine, expire_on_commit=False) as db:
        return load_state(db, campaign_id)


def _day_campaign(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    """A Cairn campaign with its player character made and its opening done. Only Cairn has the
    clock that advance_time moves."""
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(client, campaign_id, fields=_complete_form(client, campaign_id))
    assert created.status_code == 303
    _answer(client, campaign_id, "continue")
    return client, campaign_id


def _journal_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [call for call in calls if is_journal_messages(call["messages"])]


def test_the_gm_appends_replaces_and_deletes_text_in_the_journal(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    added = _tool(
        app, campaign_id, "append_journal", {"text": "Day 1: Ada crossed the Spore Bridge."}
    )
    assert added == {"added": "journal", "day": 1}
    _tool(app, campaign_id, "append_journal", {"text": "Day 1: Old Hob kept the key."})
    assert _journal_state(app, campaign_id).journal == (
        "Day 1: Ada crossed the Spore Bridge.\nDay 1: Old Hob kept the key."
    )

    replaced = _tool(
        app,
        campaign_id,
        "replace_in_journal",
        {
            "old_text": "Ada crossed the Spore Bridge.",
            "new_text": "Ada crossed the Spore Bridge, which groaned.",
        },
    )
    assert replaced == {"replaced": "journal", "deleted": False}
    assert _journal_state(app, campaign_id).journal.startswith(
        "Day 1: Ada crossed the Spore Bridge, which groaned.\n"
    )

    deleted = _tool(
        app,
        campaign_id,
        "replace_in_journal",
        {"old_text": "Day 1: Old Hob kept the key.", "new_text": ""},
    )
    assert deleted == {"replaced": "journal", "deleted": True}
    assert (
        _journal_state(app, campaign_id).journal
        == "Day 1: Ada crossed the Spore Bridge, which groaned."
    )


def test_view_journal_gives_numbered_lines_in_a_range(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    for line in ["Day 1: First.", "Day 2: Second.", "Day 3: Third."]:
        _tool(app, campaign_id, "append_journal", {"text": line})

    viewed = _tool(app, campaign_id, "view_journal", {"start_line": 2, "end_line": 3})
    assert viewed == {
        "total_lines": 3,
        "lines": [
            {"number": 2, "text": "Day 2: Second."},
            {"number": 3, "text": "Day 3: Third."},
        ],
    }
    single = _tool(app, campaign_id, "view_journal", {"start_line": 1})
    assert single["lines"] == [{"number": 1, "text": "Day 1: First."}]


def test_search_is_fuzzy_and_returns_the_lines_around_each_match(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    for line in [
        "Day 1: Ada reached the village.",
        "Day 1: The Spore Bridge groaned.",
        "Day 2: Old Hob kept the key.",
        "Day 3: Ada crossed the bridge at dawn.",
    ]:
        _tool(app, campaign_id, "append_journal", {"text": line})

    found = _tool(app, campaign_id, "search_journal", {"query": "bridje", "context": 0})
    assert found == {
        "passages": [
            {"from_line": 2, "to_line": 2, "text": "Day 1: The Spore Bridge groaned."},
            {"from_line": 4, "to_line": 4, "text": "Day 3: Ada crossed the bridge at dawn."},
        ]
    }
    wide = _tool(app, campaign_id, "search_journal", {"query": "bridge", "context": 1})
    assert wide["passages"] == [
        {
            "from_line": 1,
            "to_line": 4,
            "text": (
                "Day 1: Ada reached the village.\nDay 1: The Spore Bridge groaned.\n"
                "Day 2: Old Hob kept the key.\nDay 3: Ada crossed the bridge at dawn."
            ),
        }
    ]


def test_journal_edits_and_views_reject_bad_input(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "append_journal", {"text": "Ada crossed the bridge."})
    _tool(app, campaign_id, "append_journal", {"text": "Ada crossed the river."})

    assert "error" in _tool(
        app, campaign_id, "replace_in_journal", {"old_text": "Hob", "new_text": "Nothing."}
    )
    ambiguous = _tool(
        app, campaign_id, "replace_in_journal", {"old_text": "Ada crossed the", "new_text": "X"}
    )
    assert "error" in ambiguous and "matches 2 places" in ambiguous["error"]
    assert "error" in _tool(
        app, campaign_id, "replace_in_journal", {"old_text": "  ", "new_text": "X"}
    )
    assert "error" in _tool(app, campaign_id, "append_journal", {"text": "  "})
    assert "error" in _tool(app, campaign_id, "view_journal", {"start_line": 3, "end_line": 1})
    assert _journal_state(app, campaign_id).journal == (
        "Ada crossed the bridge.\nAda crossed the river."
    )


def test_the_journal_is_not_capped_in_length(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    chunk = "x" * 1900
    for _ in range(20):
        assert "error" not in _tool(app, campaign_id, "append_journal", {"text": chunk})
    viewed = _tool(app, campaign_id, "view_journal", {"start_line": 20, "end_line": 20})
    assert viewed["lines"][0]["text"] == chunk


def test_a_routine_turn_skips_the_journal_step(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script({"text": "Ada looks over the empty yard."})

    assert _send(client, campaign_id, script).status_code == 303

    assert _journal_calls(recorder.calls) == []
    assert _journal_state(app, campaign_id).journal == ""
    assert _saved(app, campaign_id)[1:] == [
        ("user", script),
        ("assistant", "Ada looks over the empty yard."),
    ]


def test_a_discovery_within_a_day_skips_the_journal_step(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _tool(app, campaign_id, "create_location", {"name": "Gate Room", "description": "A gate."})
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[{"name": "discover_location", "arguments": {"name": "Gate Room"}}]],
            "text": "Ada finds the gate.",
        }
    )

    assert _send(client, campaign_id, script).status_code == 303

    assert _journal_calls(recorder.calls) == []
    assert _journal_state(app, campaign_id).journal == ""


def test_a_day_end_turn_ends_with_a_journal_step_that_writes_the_journal(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _day_campaign(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[{"name": "advance_time", "arguments": {"days": 1}}]],
            "text": "The bridge groans under Ada's boots.",
            "journal": [
                [
                    {
                        "name": "append_journal",
                        "arguments": {"text": "Day 2: Ada crossed the bridge."},
                    }
                ]
            ],
        }
    )

    response = _send(client, campaign_id, script)

    assert response.status_code == 303
    assert _saved(app, campaign_id)[1:] == [
        ("user", script),
        ("assistant", "The bridge groans under Ada's boots."),
    ]
    assert _journal_state(app, campaign_id).journal == "Day 2: Ada crossed the bridge."

    journal_calls = _journal_calls(recorder.calls)
    assert journal_calls, "the journal step should have run"
    for call in journal_calls:
        assert {spec["function"]["name"] for spec in call["tools"]} == {
            "append_journal",
            "replace_in_journal",
        }
        assert any("Campaign day 2." in (m.get("content") or "") for m in call["messages"])
    narration = next(i for i, call in enumerate(recorder.calls) if call["tools"] is None)
    assert recorder.calls.index(journal_calls[0]) < narration
    for call in recorder.calls:
        if call not in journal_calls and call["tools"] is not None:
            offered = {spec["function"]["name"] for spec in call["tools"]}
            assert "append_journal" not in offered and "replace_in_journal" not in offered


def _journal_steps(app: FastAPI, campaign_id: int) -> int:
    with Session(app.state.engine) as db:
        return len(
            db.exec(
                select(Event).where(
                    Event.campaign_id == campaign_id, Event.kind == "journal.started"
                )
            ).all()
        )


def _log_text(call: dict[str, Any]) -> str:
    return "\n".join(
        m["content"]
        for m in call["messages"]
        if "Log since the journal" in (m.get("content") or "")
    )


def test_a_multi_day_jump_runs_the_journal_step_once(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _day_campaign(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {"rounds": [[{"name": "advance_time", "arguments": {"days": 3}}]], "text": "Ok."}
    )

    assert _send(client, campaign_id, script).status_code == 303

    assert _journal_steps(app, campaign_id) == 1


def test_each_journal_step_covers_only_the_log_since_the_last_one(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _day_campaign(client_factory)
    _tool(app, campaign_id, "create_location", {"name": "Gate Room", "description": "A gate."})
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    first = _script(
        {"rounds": [[{"name": "advance_time", "arguments": {"days": 1}}]], "text": "Day one."}
    )
    second = _script(
        {
            "rounds": [
                [{"name": "discover_location", "arguments": {"name": "Gate Room"}}],
                [{"name": "advance_time", "arguments": {"days": 1}}],
            ],
            "text": "Day two.",
        }
    )

    assert _send(client, campaign_id, first).status_code == 303
    assert _send(client, campaign_id, second).status_code == 303

    assert _journal_steps(app, campaign_id) == 2
    first_log, second_log = dict.fromkeys(_log_text(c) for c in _journal_calls(recorder.calls))
    assert "Gate Room" not in first_log
    assert "Gate Room" in second_log
    assert not set(first_log.splitlines()[1:]) & set(second_log.splitlines()[1:])


def test_the_state_step_reasoning_goes_back_to_the_journal_and_narration(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _day_campaign(client_factory)
    recorder = _recording_provider()
    chat = recorder.chat

    async def thinking_chat(
        model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ChatReply:
        reply = await chat(model, messages, tools)
        if tools is not None and reply.tool_requests and not is_journal_messages(messages):
            reply = replace(reply, reasoning="Ada wants the bridge noted.")
        return reply

    recorder.chat = thinking_chat
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[{"name": "advance_time", "arguments": {"days": 1}}]],
            "text": "The bridge groans under Ada's boots.",
            "journal": [
                [
                    {
                        "name": "append_journal",
                        "arguments": {"text": "Day 2: Ada crossed the bridge."},
                    }
                ]
            ],
        }
    )

    assert _send(client, campaign_id, script).status_code == 303

    journal_calls = _journal_calls(recorder.calls)
    assert journal_calls, "the journal step should have run"
    narration_call = next(call for call in recorder.calls if call["tools"] is None)
    for call in [journal_calls[0], narration_call]:
        tool_turns = [
            m for m in call["messages"] if m["role"] == "assistant" and m.get("tool_calls")
        ]
        assert [m.get("reasoning_content") for m in tool_turns] == ["Ada wants the bridge noted."]


def test_the_journal_panel_shows_the_text_and_has_an_empty_state(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    journal_view = page[page.index('data-view="journal"') : page.index('id="drawer-notes"')]
    assert "The journal is empty so far." in journal_view

    _tool(app, campaign_id, "append_journal", {"text": "Day 1: First entry about the hollow."})
    _tool(app, campaign_id, "append_journal", {"text": "Day 2: Second entry about the bridge."})
    page = client.get(f"/campaigns/{campaign_id}").text
    journal_view = page[page.index('data-view="journal"') : page.index('id="drawer-notes"')]
    assert "The journal is empty" not in journal_view
    assert "<p>Day 1: First entry about the hollow.</p>" in journal_view
    assert journal_view.index("First entry") < journal_view.index("Second entry")


def test_the_opening_introduction_is_written_into_the_journal(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client = _signed_in(client_factory)
    campaign_id = int(_new_campaign(client).headers["location"].rsplit("/", 1)[1])
    assert _create_character(client, campaign_id).status_code == 303
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    intro = "Welcome to **Twin Lakes**, Ada. The hollow waits."
    script = _script(
        {
            "rounds": [[{"name": "ooc", "arguments": {"no_narration": True}}]],
            "ooc": intro,
            "journal": [
                [
                    {
                        "name": "append_journal",
                        "arguments": {"text": "Day 1: Ada arrives at Twin Lakes."},
                    }
                ]
            ],
        }
    )
    chat, stream = recorder.chat, recorder.stream

    def with_script(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The opening is sent without a player message, so the script rides on its instruction."""
        return [
            {**m, "content": f"{m['content']}\n{script}"}
            if m["role"] == "user" and OPENING_PROMPT in m["content"]
            else m
            for m in messages
        ]

    async def scripted_chat(
        model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ChatReply:
        return await chat(model, with_script(messages), tools)

    async def scripted_stream(
        model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[Any]:
        async for item in stream(model, with_script(messages), tools):
            yield item

    recorder.chat = scripted_chat
    recorder.stream = scripted_stream

    _answer(client, campaign_id, "continue")

    assert _journal_state(app, campaign_id).journal == "Day 1: Ada arrives at Twin Lakes."
    journal_calls = _journal_calls(recorder.calls)
    assert journal_calls, "the journal step should have run for the opening"
    assert any(m.get("content") == intro for m in journal_calls[0]["messages"])
    assert _saved(app, campaign_id)[-1] == ("assistant", intro)


def test_a_thinking_model_gets_the_reasoning_of_earlier_gm_replies_with_its_tools(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    """DeepSeek's thinking mode rejects a request with tools unless every earlier assistant message
    carries the reasoning it was written with, so the opening's text-only reply must keep its."""
    client = _signed_in(client_factory)
    campaign_id = int(_new_campaign(client).headers["location"].rsplit("/", 1)[1])
    assert _create_character(client, campaign_id).status_code == 303
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    intro = "Welcome to **Twin Lakes**, Ada. The hollow waits."
    script = _script(
        {
            "rounds": [[{"name": "ooc", "arguments": {"no_narration": True}}]],
            "ooc": intro,
            "journal": [[{"name": "append_journal", "arguments": {"text": "Day 1: Ada arrives."}}]],
        }
    )
    missing: list[str] = []

    def thinking(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> None:
        if tools is None:
            return
        for m in messages:
            if m["role"] == "assistant" and "reasoning_content" not in m:
                missing.append(str(m.get("content")))

    def with_script(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**m, "content": f"{m['content']}\n{script}"}
            if m["role"] == "user" and OPENING_PROMPT in m["content"]
            else m
            for m in messages
        ]

    chat, stream = recorder.chat, recorder.stream

    async def thinking_chat(
        model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ChatReply:
        messages = with_script(messages)
        thinking(messages, tools)
        return replace(await chat(model, messages, tools), reasoning="thinking")

    async def thinking_stream(
        model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[Any]:
        messages = with_script(messages)
        thinking(messages, tools)
        async for item in stream(model, messages, tools):
            yield replace(item, reasoning="thinking") if isinstance(item, ChatReply) else item

    recorder.chat = thinking_chat
    recorder.stream = thinking_stream

    _answer(client, campaign_id, "continue")
    _send(client, campaign_id, f"I look around.\n{_script({'rounds': [[]], 'text': 'The lake.'})}")

    assert missing == []
