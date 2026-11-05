"""Lookup for the hand-drawn SVG icon set served from /static/icons."""

from __future__ import annotations

from collections.abc import Mapping

from cygnusloom.content.icons import ICON_DIR, REGISTRY_PATH, icon_registry

__all__ = [
    "ICON_DIR",
    "REGISTRY_PATH",
    "icon_path",
    "item_icon_id",
    "place_icon_path",
]

ICON_URL_PREFIX = "/static/icons/"


def _registry() -> dict[str, dict[str, str]]:
    return icon_registry()


def icon_path(icon_id: str) -> str:
    """Return the static URL path for an icon id.

    Raises KeyError if the id is not in the registry.
    """
    return ICON_URL_PREFIX + _registry()[icon_id]["path"]


PLACE_FALLBACK_ICON = "town"
# Item kinds the icon set has a picture for, used when a rule set names no icon for a kind.
ITEM_KIND_ICONS = {"spellbook": "book", "scroll": "scroll"}
WEAPON_ICON = "weapon"


def place_icon_path(icon_id: str | None) -> str:
    """The icon for a map place, falling back to a settlement for places without one."""
    return icon_path(icon_id or PLACE_FALLBACK_ICON)


def item_icon_id(kind: str, damage: str, kind_icons: Mapping[str, str] | None = None) -> str | None:
    """The icon id for an item: its kind's icon if the rule set or the fallback names one, else the
    weapon icon for an item with damage dice, else None."""
    if kind:
        icons = kind_icons or {}
        return icons.get(kind) or ITEM_KIND_ICONS.get(kind)
    return WEAPON_ICON if damage else None
