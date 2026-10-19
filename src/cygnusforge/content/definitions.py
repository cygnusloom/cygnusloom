"""Reads the JSON definitions that make up a world template or rule set."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContentError(Exception):
    """A content directory is missing, malformed, or refers to something that does not exist."""


@dataclass(frozen=True)
class Definition:
    """One object from a JSON list. `data` is the whole object, including `type` and `id`."""

    type: str
    id: str
    data: Mapping[str, Any]


def read_definitions(directory: Path) -> list[Definition]:
    """Read every `*.json` file under `directory`, in path order.

    Each file must hold a JSON list of objects, and each object needs a non-empty string
    `type` and `id`. A `(type, id)` pair may appear only once in a directory."""
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
        for index, item in enumerate(raw):
            label = f"{where}[{index}]"
            if not isinstance(item, dict):
                raise ContentError(f"{label}: must be an object")
            kind = required_text(item, "type", label)
            ident = required_text(item, "id", label)
            key = (kind, ident)
            if key in seen:
                raise ContentError(
                    f"{label}: duplicate {kind} id {ident!r} (first defined in {seen[key]})"
                )
            seen[key] = where
            definitions.append(Definition(type=kind, id=ident, data=item))
    return definitions


def required_text(data: Mapping[str, Any], key: str, label: str) -> str:
    """A non-empty string field from an object, or a ContentError naming the object."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContentError(f"{label}: {key!r} must be a non-empty string")
    return value
