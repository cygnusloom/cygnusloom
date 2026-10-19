"""World templates and rule sets, loaded once from the bundled data directory."""

from __future__ import annotations

import importlib
import inspect
import re
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from cygnusforge.content.definitions import (
    ContentError,
    Definition,
    read_definitions,
    required_text,
)
from cygnusforge.rules.dice import is_formula

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
PROMPT_ARGUMENT = "prompt"


@dataclass(frozen=True)
class WorldTemplate:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class PlayerInput:
    """The player supplies a value for a tool before it runs.

    The player rolls `dice` (a formula such as "1d20") or types free-form text. A roll's
    total is passed to the function as keyword argument `param`. `details`, if set, is a
    template filled from the arguments and shown with the prompt, such as the modifiers."""

    dice: str
    param: str
    details: str | None = None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    description: str
    parameters: Mapping[str, Any]
    function: Callable[..., Any]
    player_input: PlayerInput | None = None
    # A str.format template shown to the player after the tool runs. Fields come from the
    # arguments and the result.
    activity: str | None = None


@dataclass(frozen=True)
class RuleSet:
    id: str
    name: str
    description: str
    rules: Mapping[str, str]
    tool_calls: Mapping[str, ToolCall]


@dataclass(frozen=True)
class ContentLibrary:
    world_templates: Mapping[str, WorldTemplate]
    rule_sets: Mapping[str, RuleSet]

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> ContentLibrary:
        """Load every world template and rule set. Each subdirectory is one of either."""
        worlds = {d.name: _load_world(d) for d in _subdirectories(data_dir / "worlds")}
        rule_sets = {d.name: _load_rule_set(d) for d in _subdirectories(data_dir / "rulesets")}
        return cls(world_templates=worlds, rule_sets=rule_sets)


def _subdirectories(path: Path) -> list[Path]:
    if not path.is_dir():
        raise ContentError(f"missing content directory {path}")
    return sorted(child for child in path.iterdir() if child.is_dir())


def _single(definitions: list[Definition], kind: str, template: str) -> Definition:
    matches = [d for d in definitions if d.type == kind]
    if len(matches) != 1:
        raise ContentError(
            f"{template}: expected exactly one {kind!r} object, found {len(matches)}"
        )
    return matches[0]


def _check_directory_id(template: str, ident: str) -> None:
    if ident != template:
        raise ContentError(f"{template}: id must match the directory name, got {ident!r}")


def _load_world(directory: Path) -> WorldTemplate:
    template = directory.name
    world = _single(read_definitions(directory), "world", template)
    _check_directory_id(template, world.id)
    label = f"{template}: world"
    return WorldTemplate(
        id=world.id,
        name=required_text(world.data, "name", label),
        description=required_text(world.data, "description", label),
    )


def _load_rule_set(directory: Path) -> RuleSet:
    template = directory.name
    definitions = read_definitions(directory)
    head = _single(definitions, "rule_set", template)
    _check_directory_id(template, head.id)
    label = f"{template}: rule_set"
    modules = {d.id: _import_module(d, template) for d in definitions if d.type == "python_module"}
    rules = {
        d.id: required_text(d.data, "text", f"{template}: rule {d.id}")
        for d in definitions
        if d.type == "rule"
    }
    tool_calls: dict[str, ToolCall] = {}
    tool_names: set[str] = set()
    for d in definitions:
        if d.type != "tool_call":
            continue
        call = _load_tool_call(d, modules, template)
        if call.name in tool_names:
            raise ContentError(f"{template}: duplicate tool name {call.name!r}")
        tool_names.add(call.name)
        tool_calls[call.id] = call
    return RuleSet(
        id=head.id,
        name=required_text(head.data, "name", label),
        description=required_text(head.data, "description", label),
        rules=rules,
        tool_calls=tool_calls,
    )


def _import_module(definition: Definition, template: str) -> ModuleType:
    label = f"{template}: python_module {definition.id}"
    name = required_text(definition.data, "module", label)
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ContentError(f"{label}: cannot import {name!r}: {exc}") from exc


def _load_tool_call(
    definition: Definition, modules: Mapping[str, ModuleType], template: str
) -> ToolCall:
    label = f"{template}: tool_call {definition.id}"
    data = definition.data
    module_id = required_text(data, "python_module", label)
    if module_id not in modules:
        raise ContentError(f"{label}: unknown python_module {module_id!r}")
    function_name = required_text(data, "function", label)
    function = getattr(modules[module_id], function_name, None)
    if not callable(function):
        raise ContentError(f"{label}: {function_name!r} is not a function in {module_id!r}")
    name = required_text(data, "name", label)
    if not TOOL_NAME.fullmatch(name):
        raise ContentError(f"{label}: 'name' must be 1 to 64 letters, digits, '_' or '-'")
    parameters = _check_parameters(data.get("parameters"), label)
    player_input = _check_player_input(data.get("player_input"), parameters, function, label)
    activity = _check_activity(data.get("activity"), label)
    _check_signature(function, parameters, label, consumed=_consumed(player_input))
    return ToolCall(
        id=definition.id,
        name=name,
        description=required_text(data, "description", label),
        parameters=parameters,
        function=function,
        player_input=player_input,
        activity=activity,
    )


def _consumed(player_input: PlayerInput | None) -> frozenset[str]:
    """Schema arguments the engine uses itself rather than passing to the function."""
    return frozenset({PROMPT_ARGUMENT}) if player_input else frozenset()


def _check_player_input(
    data: object, parameters: Mapping[str, Any], function: Callable[..., Any], label: str
) -> PlayerInput | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ContentError(f"{label}: 'player_input' must be an object")
    dice = data.get("dice")
    if not is_formula(dice):
        raise ContentError(f"{label}: 'player_input.dice' must be a formula such as '1d20'")
    param = data.get("param")
    if not isinstance(param, str) or not param:
        raise ContentError(f"{label}: 'player_input.param' must name a function parameter")
    prompt = parameters["properties"].get(PROMPT_ARGUMENT)
    if not isinstance(prompt, dict) or prompt.get("type") != "string":
        raise ContentError(
            f"{label}: a tool with 'player_input' needs a string '{PROMPT_ARGUMENT}' parameter"
        )
    if PROMPT_ARGUMENT not in parameters.get("required", []):
        raise ContentError(f"{label}: '{PROMPT_ARGUMENT}' must be required")
    if param in parameters["properties"]:
        raise ContentError(f"{label}: 'player_input.param' must not be a schema parameter")
    accepted = inspect.signature(function).parameters
    if param not in accepted:
        raise ContentError(f"{label}: function does not accept parameter {param!r}")
    details = _check_template(data.get("details"), "player_input.details", label)
    if details is not None:
        _check_fields(details, parameters["properties"], "player_input.details", label)
    return PlayerInput(dice=dice, param=param, details=details)


def _check_activity(template: object, label: str) -> str | None:
    return _check_template(template, "activity", label)


def _check_template(template: object, key: str, label: str) -> str | None:
    if template is None:
        return None
    if not isinstance(template, str) or not template.strip():
        raise ContentError(f"{label}: '{key}' must be a non-empty text template")
    try:
        list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ContentError(f"{label}: '{key}' is not a valid template: {exc}") from exc
    return template


def _check_fields(template: str, arguments: Mapping[str, Any], key: str, label: str) -> None:
    """A details template may only name schema arguments, so a typo fails at load."""
    for _, field, _, _ in string.Formatter().parse(template):
        if field and field not in arguments:
            raise ContentError(f"{label}: '{key}' names unknown argument {field!r}")


def _check_parameters(schema: object, label: str) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ContentError(f"{label}: 'parameters' must be an object schema with type 'object'")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ContentError(f"{label}: 'parameters' needs a 'properties' object")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(item, str) and item in properties for item in required
    ):
        raise ContentError(f"{label}: 'required' must list names from 'properties'")
    return schema


def _check_signature(
    function: Callable[..., Any],
    parameters: Mapping[str, Any],
    label: str,
    consumed: frozenset[str] = frozenset(),
) -> None:
    """The GM calls the function with keyword arguments taken from the schema, so both must agree."""
    accepted = inspect.signature(function).parameters
    takes_any_keyword = any(p.kind is p.VAR_KEYWORD for p in accepted.values())
    if not takes_any_keyword:
        for name in parameters["properties"]:
            if name not in accepted and name not in consumed:
                raise ContentError(f"{label}: function does not accept parameter {name!r}")
    required = set(parameters.get("required", []))
    for name, param in accepted.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is param.empty and name not in required:
            raise ContentError(
                f"{label}: parameter {name!r} has no default, so it must be required"
            )
