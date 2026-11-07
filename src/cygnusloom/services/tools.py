"""Offers a rule set's tool calls to the GM and runs the ones it requests."""

from __future__ import annotations

import difflib
import inspect
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cygnusloom.content.library import (
    CONTEXT_ARGUMENT,
    PROMPT_ARGUMENT,
    MoveChoice,
    PlayerInput,
    ToolCall,
)
from cygnusloom.llm.base import LLMError, ToolRequest
from cygnusloom.rules.context import ToolContext
from cygnusloom.rules.dice import is_formula
from cygnusloom.services.attacks import modifier_name
from cygnusloom.services.common import InvalidInput, NotFound

logger = logging.getLogger(__name__)

_PYTHON_TYPES: dict[str, type] = {"string": str, "array": list, "object": dict}
# The turn review must not speak for an NPC or restart the out-of-character step. Those tools
# stay in the state step's list, but are left out of the review's.
_REVIEW_STEP_EXCLUDED = frozenset({"speak", "ooc"})


@dataclass(frozen=True)
class Ask:
    """The tool needs the player to roll `dice` or type a reply to `text` first. A prompt with
    `choices` is a move band's cost the player picks after the roll; `move`, `band`, `roll`, and
    `total` identify the roll the choice belongs to."""

    text: str
    dice: str
    details: str | None = None
    choices: tuple[MoveChoice | Mapping[str, str], ...] = ()
    move: str = ""
    band: str = ""
    roll: int = 0
    total: int = 0


@dataclass(frozen=True)
class RollAnswer:
    total: int


@dataclass(frozen=True)
class TextAnswer:
    text: str


@dataclass(frozen=True)
class ChoiceAnswer:
    """The player picked one of the choices a move band offered."""

    choice: str


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
    step gets the usual tools. The review loop refuses a record-changing tool until the review
    has set the narration aside, so the record and the narration stay in step."""
    if journal_step:
        selected = [tool for tool in tool_calls.values() if tool.journal_step]
    elif review_step:
        selected = [
            tool
            for tool in tool_calls.values()
            if not tool.journal_step and tool.name not in _REVIEW_STEP_EXCLUDED
        ]
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
    answer: RollAnswer | TextAnswer | ChoiceAnswer | None = None,
    ctx: ToolContext | None = None,
) -> ToolRun | Ask:
    """Run one requested tool.

    A tool with `player_input` needs an answer first. Without one this returns an `Ask`
    for the player. When a move's result offers choices, the same `Ask` carries them so the
    player can pick the cost of the band the roll reached. Problems are returned as an `error`
    object rather than raised, so the model can recover. Provider failures (`LLMError`) are
    raised, because the turn cannot go on."""
    tool = find_tool(tool_calls, request.name)
    if tool is None:
        return ToolRun(_error(_unknown_tool(tool_calls, request.name)), None)
    arguments, problem = _parse_arguments(request.arguments)
    if problem:
        return ToolRun(_error(problem), None)
    arguments = _coerce_arguments(tool.parameters, arguments)
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
    prompt_text = str(arguments.get(PROMPT_ARGUMENT, ""))
    details = _details(tool, arguments)
    arguments.pop(PROMPT_ARGUMENT, None)
    if isinstance(answer, TextAnswer):
        return ToolRun(json.dumps({"player_text": answer.text}), None)
    if isinstance(answer, ChoiceAnswer):
        return ToolRun(_error("that tool does not take a choice"), None)
    if tool.player_input.dice_param:
        arguments.pop(tool.player_input.dice_param, None)
    if tool.player_input.param is not None:
        arguments[tool.player_input.param] = answer.total
    if tool.player_input.weapon_roll_param is not None:
        arguments[tool.player_input.weapon_roll_param] = answer.total
    result = await _call(tool, arguments, ctx)
    # A move band may offer the player a cost to choose. The roll has happened, so the engine
    # asks which one the player picks instead of ending the tool here. The band's own text is
    # shown with the prompt, so the player reads the outcome they are choosing within.
    if isinstance(answer, RollAnswer) and tool.player_input.move_param is not None:
        payload = _json_object(result.output)
        choices = payload.get("choices") if payload is not None else None
        if isinstance(choices, list) and choices:
            band_text = str(payload.get("text", "")).strip()
            text = f"{prompt_text}\n\n{band_text}".strip() if band_text else prompt_text
            return Ask(
                text=text,
                dice="",
                details=details,
                choices=tuple(choices),
                move=str(payload.get("move", "")),
                band=str(payload.get("band", "")),
                roll=_whole(payload.get("roll")),
                total=_whole(payload.get("total")),
            )
    return result


def _unknown_tool(tool_calls: Mapping[str, ToolCall], name: str) -> str:
    """The error for a tool name that is not offered, naming the closest ones if any."""
    names = [tool.name for tool in tool_calls.values()]
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
    if close:
        options = ", ".join(repr(option) for option in close)
        return f"there is no tool named {name!r}; did you mean {options}?"
    return f"there is no tool named {name!r}"


def _parse_arguments(raw: str) -> tuple[Any, str | None]:
    """Parse a tool call's arguments. Models sometimes send a trailing comma or wrap the object
    in a JSON string, so those two slips are repaired before the schema check."""
    text = raw
    for _ in range(3):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            repaired = re.sub(r",\s*([}\]])", r"\1", text)
            if repaired == text:
                return None, "the arguments were not valid JSON"
            text = repaired
            continue
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            text = value
            continue
        return value, None
    return None, "the arguments were not valid JSON"


def _coerce_arguments(schema: Mapping[str, Any], arguments: Any) -> Any:
    """Fit a model's arguments to the tool's schema where that is unambiguous: a string where a
    list belongs, a numeric string where a number belongs, a JSON string where an object belongs,
    and enum spellings that differ only in case or separators. Anything still wrong is left for
    `check_arguments` to report."""
    if not isinstance(arguments, Mapping):
        return arguments
    properties = schema.get("properties", {})
    return {
        name: _coerce_value(properties[name], value)
        if isinstance(properties.get(name), Mapping)
        else value
        for name, value in arguments.items()
    }


def _coerce_value(schema: Mapping[str, Any], value: Any) -> Any:
    expected = schema.get("type")
    if expected == "object":
        if isinstance(value, str):
            parsed = _json_or_none(value)
            if isinstance(parsed, Mapping):
                value = parsed
        if isinstance(value, Mapping):
            value = _coerce_arguments(schema, value)
    elif expected == "array":
        value = _as_list(value)
        items = schema.get("items")
        if isinstance(items, Mapping) and isinstance(value, list):
            value = [_coerce_value(items, item) for item in value]
    elif expected == "integer":
        value = _as_integer(value)
    elif expected == "number":
        value = _as_number(value)
    elif expected == "boolean":
        value = _as_boolean(value)
    elif expected == "string":
        value = _as_string(value)
    if "enum" in schema:
        value = _enum_match(value, schema["enum"])
    return value


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> Any:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        parsed = _json_or_none(text)
        if isinstance(parsed, list):
            return parsed
        return [part.strip() for part in re.split(r"[,\n]", text) if part.strip()]
    return [value]


def _as_integer(value: Any) -> Any:
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if not isinstance(value, str):
        return value
    text = value.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return value
    return int(number) if number.is_integer() else value


def _as_number(value: Any) -> Any:
    if isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        return value
    try:
        return int(value.strip())
    except ValueError:
        pass
    try:
        return float(value.strip())
    except ValueError:
        return value


def _as_boolean(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _as_string(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return value


def _enum_match(value: Any, options: Any) -> Any:
    """An enum value the model spelled differently but unambiguously, such as `Deep Snow` for
    `deep_snow`."""
    if not isinstance(options, list) or value in options:
        return value
    if not isinstance(value, str):
        return value
    key = _normal(value)
    for option in options:
        if isinstance(option, str) and _normal(option) == key:
            return option
    matches = difflib.get_close_matches(value, [str(option) for option in options], n=1, cutoff=0.8)
    if matches:
        return next(option for option in options if str(option) == matches[0])
    return value


def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _json_object(output: str) -> Mapping[str, Any] | None:
    """The JSON object a tool returned, or None when it did not return one."""
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _whole(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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
    if player_input.move_param is not None:
        if ctx is None:
            return "", "this tool cannot run without a campaign"
        move = ctx.world.moves.get(str(arguments.get(player_input.move_param, "")))
        if move is None:
            return "", "there is no such move"
        return move.dice, None
    if player_input.check_param is not None:
        if ctx is None:
            return "", "this tool cannot run without a campaign"
        check = ctx.world.checks.get(str(arguments.get(player_input.check_param, "")))
        if check is None:
            return "", "there is no such check"
        return check.dice, None
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
            return _unknown_argument(child, name, properties)
        spec = properties[name]
        if isinstance(spec, dict):
            problem = _check_value(spec, item, child)
            if problem:
                return problem
    return None


def _unknown_argument(child: str, name: str, properties: Mapping[str, Any]) -> str:
    allowed = ", ".join(repr(option) for option in sorted(properties))
    if not allowed:
        return f"unknown argument {child!r}; this tool takes no arguments"
    hint = difflib.get_close_matches(name, list(properties), n=1, cutoff=0.6)
    suggestion = f"did you mean {hint[0]!r}? " if hint else ""
    return f"unknown argument {child!r}; {suggestion}this tool takes {allowed}"


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
