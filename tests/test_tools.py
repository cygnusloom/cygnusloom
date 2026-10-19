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
from cygnusforge.services.tools import run_tool, tool_specs
from tests.test_campaigns import _campaign_id, _send

ROLL = {"name": "social_roll", "arguments": {"relationship": 1, "circumstance": 0}}


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
    assert set(spec["function"]["parameters"]["properties"]) == {"relationship", "circumstance"}


def test_social_roll_runs_and_returns_its_parts():
    result = json.loads(
        run_tool(
            _freeform_tools(),
            _request("social_roll", '{"relationship": 2, "circumstance": -1}'),
        )
    )
    assert set(result) == {"die", "relationship", "circumstance", "total"}
    assert (result["relationship"], result["circumstance"]) == (2, -1)
    assert 1 <= result["die"] <= 20
    assert result["total"] == result["die"] + 1


def test_plain_dict_results_are_serialized():
    tools = _tool(lambda n: {"doubled": n * 2})
    assert json.loads(run_tool(tools, _request("t", '{"n": 4}'))) == {"doubled": 8}


def test_raising_tool_returns_a_generic_error():
    def boom(n: int) -> None:
        raise RuntimeError("secret internal detail")

    result = run_tool(_tool(boom), _request("t", '{"n": 1}'))
    assert _error_of(result) == "the tool failed"
    assert "secret" not in result


def test_unknown_tool_is_reported():
    error = _error_of(run_tool(_freeform_tools(), _request("nope", "{}")))
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
        assert expected in _error_of(run_tool(tools, _request("t", arguments)))
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
    script = _script({"text": "Checking the dice.", "tool_calls": [ROLL]}, {"text": "Well rolled."})

    response = _send(client, campaign_id, script)

    assert response.status_code == 303
    assert len(recorder.calls) == 2
    assert [spec["function"]["name"] for spec in recorder.calls[0]["tools"]] == ["social_roll"]
    tool_messages = [m for m in recorder.calls[1]["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 1
    result = json.loads(tool_messages[0]["content"])
    assert result["relationship"] == 1 and result["total"] == result["die"] + 1
    assert _saved(app, campaign_id) == [("user", script), ("assistant", "Well rolled.")]


def test_bad_tool_arguments_go_back_to_the_model(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    bad = {"name": "social_roll", "arguments": {"relationship": "high", "circumstance": 0}}
    script = _script({"tool_calls": [bad]}, {"text": "Let me try again."})

    response = _send(client, campaign_id, script)

    assert response.status_code == 303
    tool_message = next(m for m in recorder.calls[1]["messages"] if m["role"] == "tool")
    assert "must be integer" in _error_of(tool_message["content"])
    assert _saved(app, campaign_id)[-1] == ("assistant", "Let me try again.")


def test_three_tool_rounds_are_allowed(client_factory: Callable[[], TestClient], app: FastAPI):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    script = _script(*([{"tool_calls": [ROLL]}] * 3), {"text": "Done."})

    response = _send(client, campaign_id, script)

    assert response.status_code == 303
    assert len(recorder.calls) == 4


def test_runaway_tool_calls_stop_and_save_nothing(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    script = _script(*([{"tool_calls": [ROLL]}] * 4), {"text": "Never reached."})

    response = _send(client, campaign_id, script)

    assert response.status_code == 502
    assert len(recorder.calls) == 4
    assert _saved(app, campaign_id) == []
