"""Offers a rule set's tool calls to the GM and runs the ones it requests."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cygnusforge.content.library import CONTEXT_ARGUMENT, PROMPT_ARGUMENT, ToolCall
from cygnusforge.llm.base import LLMError, ToolRequest
from cygnusforge.rules.context import ToolContext
from cygnusforge.services.common import InvalidInput, NotFound

logger = logging.getLogger(__name__)

_PYTHON_TYPES: dict[str, type] = {"string": str, "array": list, "object": dict}


@dataclass(frozen=True)
class Ask:
    """The tool needs the player to roll `dice` or type a reply to `text` first."""

    text: str
    dice: str
    details: str | None = None


@dataclass(frozen=True)
class RollAnswer:
    total: int


@dataclass(frozen=True)
class TextAnswer:
    text: str


@dataclass(frozen=True)
class ToolRun:
    output: str
    activity: str | None


def tool_specs(tool_calls: Mapping[str, ToolCall]) -> list[dict[str, Any]] | None:
    """The tool list for a chat request, or None when the rule set has no tools."""
    if not tool_calls:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tool_calls.values()
    ]


def find_tool(tool_calls: Mapping[str, ToolCall], name: str) -> ToolCall | None:
    return next((t for t in tool_calls.values() if t.name == name), None)


async def run_tool(
    tool_calls: Mapping[str, ToolCall],
    request: ToolRequest,
    answer: RollAnswer | TextAnswer | None = None,
    ctx: ToolContext | None = None,
) -> ToolRun | Ask:
    """Run one requested tool.

    A tool with `player_input` needs an answer first. Without one this returns an `Ask`
    for the player. Problems are returned as an `error` object rather than raised, so the
    model can recover. Provider failures (`LLMError`) are raised, because the turn cannot go on."""
    tool = find_tool(tool_calls, request.name)
    if tool is None:
        return ToolRun(_error(f"there is no tool named {request.name!r}"), None)
    try:
        arguments = json.loads(request.arguments)
    except ValueError:
        return ToolRun(_error("the arguments were not valid JSON"), None)
    problem = _check_arguments(tool.parameters, arguments)
    if problem:
        return ToolRun(_error(problem), None)
    if tool.player_input is None:
        return await _call(tool, arguments, ctx)
    if answer is None:
        return Ask(
            text=arguments[PROMPT_ARGUMENT],
            dice=tool.player_input.dice,
            details=_details(tool, arguments),
        )
    arguments.pop(PROMPT_ARGUMENT, None)
    if isinstance(answer, TextAnswer):
        return ToolRun(json.dumps({"player_text": answer.text}), None)
    arguments[tool.player_input.param] = answer.total
    return await _call(tool, arguments, ctx)


async def _call(tool: ToolCall, arguments: dict[str, Any], ctx: ToolContext | None) -> ToolRun:
    call_arguments = dict(arguments)
    if tool.takes_context:
        if ctx is None:
            return ToolRun(_error("this tool cannot run without a campaign"), None)
        call_arguments[CONTEXT_ARGUMENT] = ctx
    try:
        result = tool.function(**call_arguments)
        if inspect.isawaitable(result):
            result = await result
        if hasattr(result, "to_dict"):
            result = result.to_dict()
    except LLMError:
        raise
    except (InvalidInput, NotFound) as exc:
        return ToolRun(_error(str(exc)), None)
    except Exception:
        logger.exception("tool %s failed", tool.name)
        return ToolRun(_error("the tool failed"), None)
    return ToolRun(json.dumps(result), _activity(tool, arguments, result))


def _activity(tool: ToolCall, arguments: Mapping[str, Any], result: Any) -> str | None:
    """The player-facing line for a tool run, or None when the tool has none."""
    if tool.activity is None or not isinstance(result, Mapping):
        return None
    return _fill(tool, tool.activity, {**arguments, **result}, "an activity template")


def _details(tool: ToolCall, arguments: Mapping[str, Any]) -> str | None:
    """The line shown with a tool's prompt, such as its modifiers, or None."""
    if tool.player_input is None or tool.player_input.details is None:
        return None
    return _fill(tool, tool.player_input.details, arguments, "a details template")


def _fill(tool: ToolCall, template: str, values: Mapping[str, Any], what: str) -> str | None:
    try:
        return template.format_map(values)
    except (KeyError, IndexError, ValueError, TypeError):
        logger.exception("tool %s has %s that does not fit its values", tool.name, what)
        return None


def _check_arguments(schema: Mapping[str, Any], arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return "the arguments must be a JSON object"
    properties = schema["properties"]
    for name in schema.get("required", []):
        if name not in arguments:
            return f"missing required argument {name!r}"
    for name, value in arguments.items():
        if name not in properties:
            return f"unknown argument {name!r}"
        spec = properties[name]
        expected = spec.get("type") if isinstance(spec, dict) else None
        if isinstance(expected, str) and not _has_type(value, expected):
            return f"argument {name!r} must be {expected}"
    return None


def _has_type(value: Any, json_type: str) -> bool:
    if json_type == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if json_type == "integer":
        return isinstance(value, int)
    if json_type == "number":
        return isinstance(value, int | float)
    return isinstance(value, _PYTHON_TYPES.get(json_type, object))


def _error(message: str) -> str:
    return json.dumps({"error": message})
