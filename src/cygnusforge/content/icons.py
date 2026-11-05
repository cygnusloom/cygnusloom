"""The hand-drawn icon registry, and the resolution of `looks_like` chains at content load time.

The registry lives beside the static files it names. Content loaders use `resolve_icon` so a
definition can name an icon outright, borrow another definition's looks, or fall back to the icon
its kind usually shows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from cygnusforge.content.definitions import ContentError

ICON_DIR = Path(__file__).resolve().parent.parent / "web" / "static" / "icons"
REGISTRY_PATH = ICON_DIR / "registry.json"

# The icon a definition of each kind shows when it names no icon and borrows no looks.
FALLBACK_MONSTER = "monster"
FALLBACK_PLACE = "town"
FALLBACK_ITEM = "generic-item"
FALLBACK_WEAPON = "weapon"
FALLBACK_CREATURE = "beast"


@lru_cache(maxsize=1)
def icon_registry() -> dict[str, dict[str, str]]:
    """Every registered icon, by id. Read once for the life of the process."""
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def icon_ids() -> frozenset[str]:
    """Every icon id an author may name."""
    return frozenset(icon_registry())


def check_icon(icon_id: str, label: str) -> str:
    """Return `icon_id` when it is registered, or raise a ContentError naming the definition."""
    if icon_id not in icon_ids():
        raise ContentError(f"{label}: 'icon' names no icon ({icon_id!r})")
    return icon_id


def resolve_icon(
    icon: str | None,
    looks_like: str | None,
    *,
    label: str,
    entries: Mapping[str, tuple[str | None, str | None]] | None = None,
    fallback: str | None = None,
) -> str | None:
    """The icon a definition shows.

    An explicit `icon` must be registered. Otherwise `looks_like` names either a registered icon or
    another definition in `entries` (id -> (icon, looks_like)), and the chain is followed. A cycle
    or a name that is neither an icon nor a definition is a ContentError. When neither field is
    given, `fallback` is returned.
    """
    if icon is not None:
        return check_icon(icon, label)
    if looks_like is None:
        return fallback
    chain: list[str] = []
    target = looks_like
    while True:
        if target in icon_ids():
            return target
        if target in chain:
            seen = " -> ".join([*chain, target])
            raise ContentError(f"{label}: 'looks_like' forms a cycle ({seen})")
        chain.append(target)
        entry = (entries or {}).get(target)
        if entry is None:
            raise ContentError(f"{label}: 'looks_like' names no icon or definition ({target!r})")
        icon, target = entry
        if icon is not None:
            return check_icon(icon, label)
        if target is None:
            return fallback
