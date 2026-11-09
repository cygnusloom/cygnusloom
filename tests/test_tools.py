from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.content.library import PlayerInput, ToolCall
from cygnusloom.llm.base import ChatReply, ToolRequest
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.campaigns import list_messages
from cygnusloom.services.tools import Ask, RollAnswer, TextAnswer, ToolRun, run_tool, tool_specs
from tests.conftest import content_library, without_journal
from tests.factories import (
    answer,
    campaign_ready_fast,
    pending_turn,
    place_pc,
    send_message,
)

ROLL = {
    "name": "social_roll",
    "arguments": {"prompt": "Roll to persuade.", "relationship": 1, "circumstance": 0},
}


def _roll_tools() -> dict[str, ToolCall]:
    return content_library().rule_sets["rolltest"].tool_calls


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


def _call(
    tools: dict[str, ToolCall],
    request: ToolRequest,
    answer: RollAnswer | TextAnswer | None = None,
) -> ToolRun | Ask:
    return asyncio.run(run_tool(tools, request, answer))


def _run(tools: dict[str, ToolCall], request: ToolRequest) -> str:
    run = _call(tools, request)
    assert isinstance(run, ToolRun)
    return run.output


def _error_of(result: str) -> str:
    data = json.loads(result)
    assert set(data) == {"error"}
    return data["error"]


def _damage_tools() -> dict[str, ToolCall]:
    schema = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "damage_dice": {"type": "string"}},
        "required": ["prompt", "damage_dice"],
    }
    tool = ToolCall(
        id="hit",
        name="hit",
        description="A test attack.",
        parameters=schema,
        function=lambda damage: {"damage": damage},
        player_input=PlayerInput(dice_param="damage_dice", param="damage"),
    )
    return {"hit": tool}


def test_dice_param_asks_with_the_formula_the_gm_gave():
    request = _request("hit", json.dumps({"prompt": "Roll damage.", "damage_dice": "1d8+1d6"}))
    ask = _call(_damage_tools(), request)
    assert isinstance(ask, Ask)
    assert (ask.text, ask.dice) == ("Roll damage.", "1d8+1d6")


def test_dice_param_passes_the_roll_as_the_param_and_drops_the_formula():
    request = _request("hit", json.dumps({"prompt": "Roll damage.", "damage_dice": "1d8"}))
    run = _call(_damage_tools(), request, RollAnswer(7))
    assert isinstance(run, ToolRun)
    assert json.loads(run.output) == {"damage": 7}


def test_dice_param_rejects_a_formula_that_is_not_dice():
    request = _request("hit", json.dumps({"prompt": "Roll damage.", "damage_dice": "d8"}))
    run = _call(_damage_tools(), request)
    assert isinstance(run, ToolRun)
    assert "is not a dice formula" in _error_of(run.output)


def test_no_tools_means_no_tool_list():
    assert tool_specs({}) is None


def _offered_names(calls):
    return {call.name for call in calls.values() if not call.journal_step and not call.review_step}


def test_specs_describe_the_social_roll_tool():
    specs = {spec["function"]["name"]: spec for spec in tool_specs(_roll_tools()) or []}
    assert set(specs) == _offered_names(_roll_tools())
    spec = specs["social_roll"]
    assert spec["type"] == "function"
    assert "d20" in spec["function"]["description"]
    assert set(spec["function"]["parameters"]["properties"]) == {
        "prompt",
        "relationship",
        "circumstance",
    }


SOCIAL_ARGUMENTS = '{"prompt": "Roll to persuade.", "relationship": 2, "circumstance": -1}'


def test_social_roll_asks_the_player_before_it_runs():
    ask = _call(_roll_tools(), _request("social_roll", SOCIAL_ARGUMENTS))
    assert ask == Ask(
        text="Roll to persuade.",
        dice="1d20",
        details="Modifiers: +2 relationship, -1 circumstance.",
    )


def test_social_roll_runs_with_the_players_roll():
    run = _call(_roll_tools(), _request("social_roll", SOCIAL_ARGUMENTS), RollAnswer(14))
    assert isinstance(run, ToolRun)
    assert json.loads(run.output) == {"die": 14, "relationship": 2, "circumstance": -1, "total": 15}
    assert (
        run.activity
        == "You rolled 14 on the d20, with +2 relationship and -1 circumstance, for 15."
    )


def test_social_roll_takes_a_free_form_reply_instead_of_a_roll():
    run = _call(
        _roll_tools(),
        _request("social_roll", SOCIAL_ARGUMENTS),
        TextAnswer("I show him the letter"),
    )
    assert isinstance(run, ToolRun)
    assert json.loads(run.output) == {"player_text": "I show him the letter"}
    assert run.activity is None


def _move_tools(*, choices: bool) -> dict[str, ToolCall]:
    def cast(move: str, roll: int) -> dict[str, Any]:
        result: dict[str, Any] = {
            "move": "Cast a Spell",
            "roll": roll,
            "total": roll + 2,
            "band": "7-9",
            "text": "The magic works, but choose one.",
        }
        if choices:
            result["choices"] = [
                {"id": "stress", "label": "Mark 1 Stress", "text": "The working drains you."}
            ]
        return result

    schema = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "move": {"type": "string"}},
        "required": ["prompt", "move"],
    }
    tool = ToolCall(
        id="move",
        name="move",
        description="A test move.",
        parameters=schema,
        function=cast,
        player_input=PlayerInput(move_param="move", param="roll"),
    )
    return {"move": tool}


MOVE_ARGUMENTS = '{"prompt": "Roll to cast.", "move": "cast_a_spell"}'


def test_a_move_roll_with_choices_asks_the_player_to_pick():
    ask = _call(_move_tools(choices=True), _request("move", MOVE_ARGUMENTS), RollAnswer(8))
    assert isinstance(ask, Ask)
    assert ask.choices == (
        {"id": "stress", "label": "Mark 1 Stress", "text": "The working drains you."},
    )
    assert (ask.move, ask.band, ask.roll, ask.total) == ("Cast a Spell", "7-9", 8, 10)
    assert ask.dice == ""
    assert ask.text == "Roll to cast.\n\nThe magic works, but choose one."


def test_a_move_roll_without_choices_returns_its_result():
    run = _call(_move_tools(choices=False), _request("move", MOVE_ARGUMENTS), RollAnswer(8))
    assert isinstance(run, ToolRun)
    assert json.loads(run.output)["band"] == "7-9"


def test_social_roll_needs_a_prompt_for_the_player():
    error = _error_of(
        _run(_roll_tools(), _request("social_roll", '{"relationship": 2, "circumstance": 0}'))
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
    error = _error_of(_run(_roll_tools(), _request("nope", "{}")))
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


def test_arguments_are_fitted_to_the_schema_before_the_check():
    schema = {
        "type": "object",
        "properties": {
            "words": {"type": "array", "items": {"type": "string"}},
            "count": {"type": "integer"},
            "loud": {"type": "boolean"},
            "note": {"type": "object", "properties": {"text": {"type": "string"}}},
            "weather": {"type": "string", "enum": ["deep_snow", "clear"]},
        },
        "required": ["words", "count", "loud", "note", "weather"],
    }
    tool = ToolCall(
        id="t",
        name="t",
        description="A test tool.",
        parameters=schema,
        function=lambda **kwargs: kwargs,
    )
    arguments = json.dumps(
        {
            "words": "brave, curious",
            "count": "3",
            "loud": "true",
            "note": '{"text": 7}',
            "weather": "Deep Snow",
        }
    )

    result = json.loads(_run({"t": tool}, _request("t", arguments)))

    assert result == {
        "words": ["brave", "curious"],
        "count": 3,
        "loud": True,
        "note": {"text": "7"},
        "weather": "deep_snow",
    }


def test_a_trailing_comma_or_json_string_is_repaired():
    tools = _tool(lambda n: {"n": n})

    assert json.loads(_run(tools, _request("t", '{"n": 2,}'))) == {"n": 2}
    assert json.loads(_run(tools, _request("t", '"{\\"n\\": 3}"'))) == {"n": 3}


def test_an_unknown_tool_suggests_a_close_name():
    error = _error_of(_run(_roll_tools(), _request("social_role", "{}")))
    assert "there is no tool named 'social_role'" in error
    assert "'social_roll'" in error


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
    return [m.id for m in list_messages(app.state.engine, campaign_id)]


def _saved(app: FastAPI, campaign_id: int) -> list[tuple[str, str]]:
    return [(m.role, m.content) for m in list_messages(app.state.engine, campaign_id)]


def test_model_rolls_then_narrates_and_only_the_narration_is_saved(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script({"rounds": [[ROLL]], "text": "Well rolled."})

    response = send_message(client, campaign_id, script)

    assert response.status_code == 303
    offered = [spec["function"]["name"] for spec in recorder.calls[0]["tools"]]
    assert set(offered) == _offered_names(_roll_tools())
    assert _saved(app, campaign_id)[1:] == [("user", script)]
    prompt = pending_turn(app, campaign_id).prompt
    assert (prompt.text, prompt.dice, prompt.details) == (
        "Roll to persuade.",
        "1d20",
        "Modifiers: +1 relationship, +0 circumstance.",
    )

    events = answer(client, campaign_id, "roll")

    assert events[-1] == {"type": "saved", "event_id": _saved_ids(app, campaign_id)[-1]}
    assert len(without_journal(recorder.calls)) == 4
    assert "undo_narration" in {spec["function"]["name"] for spec in recorder.calls[-1]["tools"]}
    tool_messages = [m for m in recorder.calls[1]["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 1
    result = json.loads(tool_messages[0]["content"])
    assert result["relationship"] == 1 and result["total"] == result["die"] + 1
    assert _saved(app, campaign_id)[1:] == [("user", script), ("assistant", "Well rolled.")]
    assert pending_turn(app, campaign_id) is None


def test_bad_tool_arguments_go_back_to_the_model(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    bad = {
        "name": "social_roll",
        "arguments": {"prompt": "Roll.", "relationship": "high", "circumstance": 0},
    }
    script = _script({"rounds": [[bad]], "text": "Let me try again."})

    response = send_message(client, campaign_id, script)

    assert response.status_code == 303
    tool_message = next(m for m in recorder.calls[1]["messages"] if m["role"] == "tool")
    assert "must be integer" in _error_of(tool_message["content"])
    assert _saved(app, campaign_id)[-1] == ("assistant", "Let me try again.")
    assert pending_turn(app, campaign_id) is None


def test_state_steps_are_not_capped_at_a_number_of_rounds(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script({"rounds": [[ROLL]] * 6, "text": "Done."})

    send_message(client, campaign_id, script)
    for _ in range(6):
        answer(client, campaign_id, "roll")

    assert len(without_journal(recorder.calls)) == 9
    assert _saved(app, campaign_id)[-1] == ("assistant", "Done.")


SPEAK = {
    "name": "speak",
    "arguments": {"npc": "Mara Thistledown", "intent": "Greet Ada and ask for help."},
}


def test_npc_dialogue_is_queued_for_the_narration_not_sent_as_a_tool_result(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    place_pc(client, campaign_id)
    script = _script(
        {
            "rounds": [[SPEAK]],
            "speech": {"Mara Thistledown": "Mind the bog, dearie."},
            "text": "Mara looks up from the ladder and smiles.",
        }
    )

    send_message(client, campaign_id, script)

    messages = recorder.calls[-1]["messages"]
    tool_results = [m["content"] for m in messages if m["role"] == "tool"]
    assert tool_results
    assert all("Mind the bog" not in content for content in tool_results)
    queued = [
        m
        for m in messages
        if m["role"] == "system" and '"dialogue": "Mind the bog, dearie."' in m["content"]
    ]
    assert len(queued) == 1
    assert _saved(app, campaign_id)[-1] == (
        "assistant",
        "Mara looks up from the ladder and smiles.",
    )
