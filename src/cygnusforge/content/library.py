"""World templates and rule sets, loaded once from the bundled data directory."""

from __future__ import annotations

import importlib
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


@dataclass(frozen=True)
class WorldTemplate:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class Roll:
    id: str
    description: str
    function: Callable[..., Any]


@dataclass(frozen=True)
class RuleSet:
    id: str
    name: str
    description: str
    rules: Mapping[str, str]
    rolls: Mapping[str, Roll]


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
    rolls = {}
    for d in definitions:
        if d.type != "roll":
            continue
        roll_label = f"{template}: roll {d.id}"
        module_id = required_text(d.data, "python_module", roll_label)
        if module_id not in modules:
            raise ContentError(f"{roll_label}: unknown python_module {module_id!r}")
        function_name = required_text(d.data, "function", roll_label)
        function = getattr(modules[module_id], function_name, None)
        if not callable(function):
            raise ContentError(
                f"{roll_label}: {function_name!r} is not a function in {module_id!r}"
            )
        rolls[d.id] = Roll(
            id=d.id,
            description=required_text(d.data, "description", roll_label),
            function=function,
        )
    return RuleSet(
        id=head.id,
        name=required_text(head.data, "name", label),
        description=required_text(head.data, "description", label),
        rules=rules,
        rolls=rolls,
    )


def _import_module(definition: Definition, template: str) -> ModuleType:
    label = f"{template}: python_module {definition.id}"
    name = required_text(definition.data, "module", label)
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ContentError(f"{label}: cannot import {name!r}: {exc}") from exc
