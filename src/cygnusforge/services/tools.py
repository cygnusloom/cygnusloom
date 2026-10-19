"""Offers a rule set's tool calls to the GM and runs the ones it requests."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from cygnusforge.content.library import ToolCall
from cygnusforge.llm.base import ToolRequest

logger = logging.getLogger(__name__)

_PYTHON_TYPES: dict[str, type] = {"string": str, "array": list, "object": dict}


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


def run_tool(tool_calls: Mapping[str, ToolCall], request: ToolRequest) -> str:
    """Run one requested tool and return its JSON result.

    Problems are returned as an `error` object rather than raised, so the model can recover."""
    tool = next((t for t in tool_calls.values() if t.name == request.name), None)
    if tool is None:
        return _error(f"there is no tool named {request.name!r}")
    try:
        arguments = json.loads(request.arguments)
    except ValueError:
        return _error("the arguments were not valid JSON")
    problem = _check_arguments(tool.parameters, arguments)
    if problem:
        return _error(problem)
    try:
        result = tool.function(**arguments)
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        return json.dumps(result)
    except Exception:
        logger.exception("tool %s failed", tool.name)
        return _error("the tool failed")


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
