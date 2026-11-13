"""Reads the JSON definitions that make up a world template or rule set."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContentError(Exception):
    """A content directory is missing, malformed, or refers to something that does not exist."""


DEFAULTS_TYPE = "defaults"


@dataclass(frozen=True)
class Definition:
    """One object from a JSON list. `data` is the whole object, including `type` and `id`."""

    type: str
    id: str
    data: Mapping[str, Any]


def range_bands(value: object, label: str) -> dict[str, int]:
    """A ranged attack's bands, mapping each band name to its farthest distance in feet. An empty
    object means the attack has no ranged bands."""
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: ranges must map band names to distances in feet")
    result: dict[str, int] = {}
    for name, feet in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ContentError(f"{label}: each range band needs a name")
        key = name.strip().casefold()
        if key in result:
            raise ContentError(f"{label}: range band {name!r} is listed twice")
        if not isinstance(feet, int) or isinstance(feet, bool) or feet < 1:
            raise ContentError(
                f"{label}: range band {name!r} must be a whole number of feet, at least 1"
            )
        result[key] = feet
    return result


@dataclass(frozen=True)
class _FileDefaults:
    """Fields merged under definitions in one file, and the definitions they match."""

    label: str
    when: Mapping[str, Any]
    values: Mapping[str, Any]


def _file_defaults(item: Mapping[str, Any], label: str) -> _FileDefaults:
    """Read one file-defaults entry. `when` matches top-level field values, with null meaning
    the field is absent; `set` supplies values for fields the definition itself omits."""
    allowed = {"type", "id", "when", "set"}
    extra = set(item) - allowed
    if extra:
        names = ", ".join(repr(name) for name in sorted(extra))
        raise ContentError(f"{label}: a defaults entry may not carry {names}")
    ident = required_text(item, "id", label)
    when = item.get("when", {})
    if not isinstance(when, Mapping):
        raise ContentError(f"{label}: defaults {ident!r}: 'when' must be an object")
    values = item.get("set")
    if not isinstance(values, Mapping):
        raise ContentError(f"{label}: defaults {ident!r}: 'set' must be an object")
    if "type" in values or "id" in values:
        raise ContentError(f"{label}: defaults {ident!r}: 'set' may not change type or id")
    return _FileDefaults(label=f"{label}: defaults {ident!r}", when=when, values=values)


def _matches_defaults(data: Mapping[str, Any], when: Mapping[str, Any]) -> bool:
    """Whether a definition's top-level fields match a defaults entry. A null expected value
    means the field must be absent."""
    for key, expected in when.items():
        if expected is None:
            if key in data:
                return False
        elif data.get(key) != expected:
            return False
    return True


def _fill_missing(target: dict[str, Any], defaults: Mapping[str, Any]) -> None:
    """Fill fields a definition omits. A nested object merges key by key; lists and scalars are
    supplied only when the definition has no value, so the definition always wins."""
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, Mapping) and isinstance(target[key], dict):
            _fill_missing(target[key], value)


def _apply_file_defaults(item: Mapping[str, Any], defaults: list[_FileDefaults]) -> dict[str, Any]:
    """Merge every matching file default under a definition. The definition's own fields win,
    and an earlier matching defaults entry wins over a later one."""
    merged = copy.deepcopy(dict(item))
    for entry in defaults:
        if _matches_defaults(item, entry.when):
            _fill_missing(merged, entry.values)
    return merged


def read_definitions(directory: Path) -> list[Definition]:
    """Read every `*.json` file under `directory`, in path order.

    Each file must hold a JSON list of objects, and each object needs a non-empty string
    `type` and `id`. A `(type, id)` pair may appear only once in a directory.

    A file may start with one or more `defaults` entries. Each supplies fields for every
    definition in that file that matches its `when` object. A definition's own fields win, so
    defaults are a shorthand for constant or conditional fields, not a new definition type."""
    files = sorted(directory.rglob("*.json"))
    if not files:
        raise ContentError(f"{directory.name}: no JSON files found")
    seen: dict[tuple[str, str], str] = {}
    definitions: list[Definition] = []
    for path in files:
        where = path.relative_to(directory).as_posix()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContentError(f"{where}: {exc}") from exc
        if not isinstance(raw, list):
            raise ContentError(f"{where}: top level must be a list of objects")
        defaults: list[_FileDefaults] = []
        for index, item in enumerate(raw):
            label = f"{where}[{index}]"
            if not isinstance(item, dict):
                raise ContentError(f"{label}: must be an object")
            kind = required_text(item, "type", label)
            if kind == DEFAULTS_TYPE:
                defaults.append(_file_defaults(item, label))
        for index, item in enumerate(raw):
            label = f"{where}[{index}]"
            kind = required_text(item, "type", label)
            if kind == DEFAULTS_TYPE:
                continue
            ident = required_text(item, "id", label)
            key = (kind, ident)
            if key in seen:
                raise ContentError(
                    f"{label}: duplicate {kind} id {ident!r} (first defined in {seen[key]})"
                )
            seen[key] = where
            definitions.append(
                Definition(type=kind, id=ident, data=_apply_file_defaults(item, defaults))
            )
    return definitions


def required_text(data: Mapping[str, Any], key: str, label: str) -> str:
    """A non-empty string field from an object, or a ContentError naming the object."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContentError(f"{label}: {key!r} must be a non-empty string")
    return value
