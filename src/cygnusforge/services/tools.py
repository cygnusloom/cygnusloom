"""Offers a rule set's tool calls to the GM and runs the ones it requests."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cygnusforge.content.library import CONTEXT_ARGUMENT, PROMPT_ARGUMENT, PlayerInput, ToolCall
from cygnusforge.llm.base import LLMError, ToolRequest
from cygnusforge.rules.context import ToolContext
from cygnusforge.rules.dice import is_formula
from cygnusforge.services.attacks import modifier_name
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


def tool_specs(
    tool_calls: Mapping[str, ToolCall],
    journal_step: bool = False,
    review_step: bool = False,
) -> list[dict[str, Any]] | None:
    """The tool list for a chat request, or None when there are no tools for it. The journal step
    gets only its own tools; the turn review step gets the usual tools plus its own; every other
    step gets the usual tools."""
    if journal_step:
        selected = [tool for tool in tool_calls.values() if tool.journal_step]
    elif review_step:
        selected = [tool for tool in tool_calls.values() if not tool.journal_step]
    else:
        selected = [
            tool for tool in tool_calls.values() if not tool.journal_step and not tool.review_step
        ]
    if not selected:
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
        for tool in selected
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
    problem = check_arguments(tool.parameters, arguments)
    if problem:
        return ToolRun(_error(problem), None)
    if tool.player_input is None:
        return await _call(tool, arguments, ctx)
    if answer is None:
        dice, problem = _dice_for(tool.player_input, arguments, ctx)
        if problem is not None:
            return ToolRun(_error(problem), None)
        if not is_formula(dice):
            return ToolRun(_error(f"{dice!r} is not a dice formula such as '1d8'"), None)
        shown = dict(arguments)
        if tool.player_input.dice_param:
            shown[tool.player_input.dice_param] = dice
        return Ask(
            text=arguments[PROMPT_ARGUMENT],
            dice=dice,
            details=_details(tool, shown),
        )
    arguments.pop(PROMPT_ARGUMENT, None)
    if isinstance(answer, TextAnswer):
        return ToolRun(json.dumps({"player_text": answer.text}), None)
    if tool.player_input.dice_param:
        arguments.pop(tool.player_input.dice_param, None)
    if tool.player_input.param is not None:
        arguments[tool.player_input.param] = answer.total
    if tool.player_input.weapon_roll_param is not None:
        arguments[tool.player_input.weapon_roll_param] = answer.total
    return await _call(tool, arguments, ctx)


def _dice_for(
    player_input: PlayerInput, arguments: Mapping[str, Any], ctx: ToolContext | None
) -> tuple[str, str | None]:
    """The dice the player rolls, and a problem if they cannot be known. An attack modifier, when
    one is named, replaces the GM's dice with its own."""
    if player_input.weapon_param is not None:
        if ctx is None:
            return "", "this tool cannot run without a campaign"
        weapon = str(arguments.get(player_input.weapon_param, ""))
        modifier = arguments.get(player_input.modifier_param) if player_input.modifier_param else ""
        try:
            return ctx.world.attack_dice(weapon, modifier or ""), None
        except InvalidInput as exc:
            return "", str(exc)
    if player_input.dice is not None:
        return player_input.dice, None
    dice = str(arguments.get(player_input.dice_param, ""))
    if player_input.modifier_param is None:
        return dice, None
    modifier = arguments.get(player_input.modifier_param)
    if not modifier_name(modifier):
        return dice, None
    if ctx is None:
        return "", "this tool cannot run without a campaign"
    try:
        modifier_dice = ctx.world.modifier_dice(str(modifier))
    except InvalidInput as exc:
        return "", str(exc)
    return modifier_dice or dice, None


def _bound_arguments(tool: ToolCall, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The keyword arguments a tool passes on: the schema's arguments under the names the function
    or method takes, the objects the tool builds from them, and the arguments the tool fixes."""
    built = {
        source
        for value in tool.bind.values()
        if isinstance(value, Mapping)
        for source in value.values()
    }
    call_arguments = {
        tool.rename.get(name, name): value for name, value in arguments.items() if name not in built
    }
    for name, value in tool.bind.items():
        call_arguments[name] = (
            {key: arguments.get(source) for key, source in value.items()}
            if isinstance(value, Mapping)
            else value
        )
    return call_arguments


async def _call(tool: ToolCall, arguments: dict[str, Any], ctx: ToolContext | None) -> ToolRun:
    call_arguments = _bound_arguments(tool, arguments)
    if tool.world_method is not None:
        if ctx is None:
            return ToolRun(_error("this tool cannot run without a campaign"), None)
        call = getattr(ctx.world, tool.world_method)
    elif tool.function is not None:
        if tool.takes_context:
            if ctx is None:
                return ToolRun(_error("this tool cannot run without a campaign"), None)
            call_arguments[CONTEXT_ARGUMENT] = ctx
        call = tool.function
    else:
        return ToolRun(_error("the tool has no function to run"), None)
    try:
        result = call(**call_arguments)
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


def check_arguments(schema: Mapping[str, Any], arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return "the arguments must be a JSON object"
    return _check_object(schema, arguments, "")


def _check_object(schema: Mapping[str, Any], value: Mapping[str, Any], path: str) -> str | None:
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in value:
            return f"missing required argument {_child(path, name)!r}"
    for name, item in value.items():
        child = _child(path, name)
        if name not in properties:
            return f"unknown argument {child!r}"
        spec = properties[name]
        if isinstance(spec, dict):
            problem = _check_value(spec, item, child)
            if problem:
                return problem
    return None


def _check_value(schema: Mapping[str, Any], value: Any, path: str) -> str | None:
    """Check a value against the JSON schema subset tools use: types, enums, integer bounds,
    objects, and arrays with unique items."""
    expected = schema.get("type")
    if isinstance(expected, str) and not _has_type(value, expected):
        return f"argument {path!r} must be {expected}"
    if "enum" in schema and value not in schema["enum"]:
        options = ", ".join(str(option) for option in schema["enum"])
        return f"argument {path!r} must be one of: {options}"
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"argument {path!r} must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"argument {path!r} must be at most {schema['maximum']}"
    if isinstance(value, dict):
        return _check_object(schema, value, path)
    if isinstance(value, list):
        if schema.get("uniqueItems") and len({repr(item) for item in value}) != len(value):
            return f"argument {path!r} must not repeat an item"
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                problem = _check_value(items, item, f"{path}[{index}]")
                if problem:
                    return problem
    return None


def _child(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


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
