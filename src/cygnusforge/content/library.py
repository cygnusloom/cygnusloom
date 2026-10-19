"""World templates and rule sets, loaded once from the bundled data directory."""

from __future__ import annotations

import importlib
import inspect
import re
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

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class WorldTemplate:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    description: str
    parameters: Mapping[str, Any]
    function: Callable[..., Any]


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
    _check_signature(function, parameters, label)
    return ToolCall(
        id=definition.id,
        name=name,
        description=required_text(data, "description", label),
        parameters=parameters,
        function=function,
    )


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
    function: Callable[..., Any], parameters: Mapping[str, Any], label: str
) -> None:
    """The GM calls the function with keyword arguments taken from the schema, so both must agree."""
    accepted = inspect.signature(function).parameters
    takes_any_keyword = any(p.kind is p.VAR_KEYWORD for p in accepted.values())
    if not takes_any_keyword:
        for name in parameters["properties"]:
            if name not in accepted:
                raise ContentError(f"{label}: function does not accept parameter {name!r}")
    required = set(parameters.get("required", []))
    for name, param in accepted.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is param.empty and name not in required:
            raise ContentError(
                f"{label}: parameter {name!r} has no default, so it must be required"
            )
