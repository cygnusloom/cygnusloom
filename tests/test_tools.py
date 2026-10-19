from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusforge.content.library import ContentLibrary, ToolCall
from cygnusforge.db.models import Message
from cygnusforge.llm.base import ChatReply, ToolRequest
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.services.tools import Ask, RollAnswer, TextAnswer, ToolRun, run_tool, tool_specs
from tests.test_campaigns import _answer, _campaign_id, _pending, _send

ROLL = {
    "name": "social_roll",
    "arguments": {"prompt": "Roll to persuade.", "relationship": 1, "circumstance": 0},
}


def _freeform_tools() -> dict[str, ToolCall]:
    return ContentLibrary.load().rule_sets["freeform"].tool_calls


def _tool(function: Callable[..., Any]) -> dict[str, ToolCall]:
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    tool = ToolCall(
        id="t", name="t", description="A test tool.", parameters=schema, function=function
    )
    return {"t": tool}


def _request(name: str, arguments: str) -> ToolRequest:
    return ToolRequest(id="call_1", name=name, arguments=arguments)


def _run(tools: dict[str, ToolCall], request: ToolRequest) -> str:
    run = run_tool(tools, request)
    assert isinstance(run, ToolRun)
    return run.output


def _error_of(result: str) -> str:
    data = json.loads(result)
    assert set(data) == {"error"}
    return data["error"]


def test_no_tools_means_no_tool_list():
    assert tool_specs({}) is None


def test_specs_describe_the_social_roll_tool():
    specs = tool_specs(_freeform_tools())
    assert specs is not None and len(specs) == 1
    spec = specs[0]
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "social_roll"
    assert "d20" in spec["function"]["description"]
    assert set(spec["function"]["parameters"]["properties"]) == {
        "prompt",
        "relationship",
        "circumstance",
    }


SOCIAL_ARGUMENTS = '{"prompt": "Roll to persuade.", "relationship": 2, "circumstance": -1}'


def test_social_roll_asks_the_player_before_it_runs():
    ask = run_tool(_freeform_tools(), _request("social_roll", SOCIAL_ARGUMENTS))
    assert ask == Ask(
        text="Roll to persuade.",
        dice="1d20",
        details="Modifiers: +2 relationship, -1 circumstance.",
    )


def test_social_roll_runs_with_the_players_roll():
    run = run_tool(_freeform_tools(), _request("social_roll", SOCIAL_ARGUMENTS), RollAnswer(14))
    assert isinstance(run, ToolRun)
    assert json.loads(run.output) == {"die": 14, "relationship": 2, "circumstance": -1, "total": 15}
    assert (
        run.activity
        == "You rolled 14 on the d20, with +2 relationship and -1 circumstance, for 15."
    )


def test_social_roll_takes_a_free_form_reply_instead_of_a_roll():
    run = run_tool(
        _freeform_tools(),
        _request("social_roll", SOCIAL_ARGUMENTS),
        TextAnswer("I show him the letter"),
    )
    assert isinstance(run, ToolRun)
    assert json.loads(run.output) == {"player_text": "I show him the letter"}
    assert run.activity is None


def test_social_roll_needs_a_prompt_for_the_player():
    error = _error_of(
        _run(_freeform_tools(), _request("social_roll", '{"relationship": 2, "circumstance": 0}'))
    )
    assert error == "missing required argument 'prompt'"


def test_plain_dict_results_are_serialized():
    tools = _tool(lambda n: {"doubled": n * 2})
    assert json.loads(_run(tools, _request("t", '{"n": 4}'))) == {"doubled": 8}


def test_raising_tool_returns_a_generic_error():
    def boom(n: int) -> None:
        raise RuntimeError("secret internal detail")

    result = _run(_tool(boom), _request("t", '{"n": 1}'))
    assert _error_of(result) == "the tool failed"
    assert "secret" not in result


def test_unknown_tool_is_reported():
    error = _error_of(_run(_freeform_tools(), _request("nope", "{}")))
    assert "no tool named 'nope'" in error


def test_arguments_are_checked_before_the_tool_runs():
    calls = []

    def record(**kwargs: Any) -> None:
        calls.append(kwargs)

    tools = _tool(record)
    cases = {
        "{not json": "not valid JSON",
        "[1]": "must be a JSON object",
        "{}": "missing required argument 'n'",
        '{"n": 1, "luck": 3}': "unknown argument 'luck'",
        '{"n": "one"}': "argument 'n' must be integer",
        '{"n": true}': "argument 'n' must be integer",
    }
    for arguments, expected in cases.items():
        assert expected in _error_of(_run(tools, _request("t", arguments)))
    assert calls == []


def _recording_provider() -> Any:
    class Recorder:
        name = "fake"

        def __init__(self) -> None:
            self.inner = FakeProvider()
            self.calls: list[dict[str, Any]] = []

        async def chat(
            self,
            model: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
        ) -> ChatReply:
            self.calls.append({"messages": list(messages), "tools": tools})
            return await self.inner.chat(model, messages, tools)

        async def stream(
            self,
            model: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
        ) -> Any:
            self.calls.append({"messages": list(messages), "tools": tools})
            async for item in self.inner.stream(model, messages, tools):
                yield item

    return Recorder()


def _script(*turns: dict[str, Any]) -> str:
    return "__FAKE__:" + json.dumps({"turns": list(turns)})


def _saved_ids(app: FastAPI, campaign_id: int) -> list[int]:
    with Session(app.state.engine) as db:
        return [
            m.id for m in db.exec(select(Message).where(Message.campaign_id == campaign_id)).all()
        ]


def _saved(app: FastAPI, campaign_id: int) -> list[tuple[str, str]]:
    with Session(app.state.engine) as db:
        rows = db.exec(
            select(Message).where(Message.campaign_id == campaign_id).order_by(Message.id)
        ).all()
        return [(m.role, m.content) for m in rows]


def test_model_rolls_then_narrates_and_only_the_narration_is_saved(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    script = _script({"rounds": [[ROLL]], "text": "Well rolled."})

    response = _send(client, campaign_id, script)

    assert response.status_code == 303
    assert [spec["function"]["name"] for spec in recorder.calls[0]["tools"]] == ["social_roll"]
    assert _saved(app, campaign_id) == []
    assert _pending(app, campaign_id)["prompt"] == {
        "text": "Roll to persuade.",
        "dice": "1d20",
        "details": "Modifiers: +1 relationship, +0 circumstance.",
    }

    events = _answer(client, campaign_id, "roll")

    assert events[-1] == {"type": "saved", "message_id": _saved_ids(app, campaign_id)[-1]}
    assert len(recorder.calls) == 3
    tool_messages = [m for m in recorder.calls[1]["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 1
    result = json.loads(tool_messages[0]["content"])
    assert result["relationship"] == 1 and result["total"] == result["die"] + 1
    assert _saved(app, campaign_id) == [("user", script), ("assistant", "Well rolled.")]
    assert _pending(app, campaign_id) is None


def test_bad_tool_arguments_go_back_to_the_model(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    bad = {
        "name": "social_roll",
        "arguments": {"prompt": "Roll.", "relationship": "high", "circumstance": 0},
    }
    script = _script({"rounds": [[bad]], "text": "Let me try again."})

    response = _send(client, campaign_id, script)

    assert response.status_code == 303
    tool_message = next(m for m in recorder.calls[1]["messages"] if m["role"] == "tool")
    assert "must be integer" in _error_of(tool_message["content"])
    assert _saved(app, campaign_id)[-1] == ("assistant", "Let me try again.")
    assert _pending(app, campaign_id) is None


def test_three_tool_rounds_are_allowed(client_factory: Callable[[], TestClient], app: FastAPI):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    script = _script({"rounds": [[ROLL]] * 3, "text": "Done."})

    _send(client, campaign_id, script)
    for _ in range(3):
        _answer(client, campaign_id, "roll")

    assert len(recorder.calls) == 5
    assert _saved(app, campaign_id)[-1] == ("assistant", "Done.")


def test_runaway_tool_calls_stop_and_save_nothing(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    script = _script({"rounds": [[ROLL]] * 4, "text": "Never reached."})

    _send(client, campaign_id, script)
    for _ in range(2):
        _answer(client, campaign_id, "roll")
    events = _answer(client, campaign_id, "roll")

    assert events[-1]["type"] == "error" and events[-1]["status"] == 502
    assert len(recorder.calls) == 4
    assert _saved(app, campaign_id) == []
