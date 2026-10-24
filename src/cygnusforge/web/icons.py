"""Lookup for the hand-drawn SVG icon set served from /static/icons."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ICON_DIR = Path(__file__).parent / "static" / "icons"
REGISTRY_PATH = ICON_DIR / "registry.json"
ICON_URL_PREFIX = "/static/icons/"


@lru_cache(maxsize=1)
def _registry() -> dict[str, dict[str, str]]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def icon_path(icon_id: str) -> str:
    """Return the static URL path for an icon id.

    Raises KeyError if the id is not in the registry.
    """
    return ICON_URL_PREFIX + _registry()[icon_id]["path"]
