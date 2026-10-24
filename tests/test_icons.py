"""Tests for the static SVG icon set and its registry."""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cygnusforge.web.icons import ICON_DIR, REGISTRY_PATH, icon_path

SVG_NS = "{http://www.w3.org/2000/svg}"

REQUIRED = {
    "terrain": [
        "plains",
        "forest",
        "hills",
        "mountain",
        "swamp",
        "water",
        "river",
        "road",
        "ruins",
        "cave",
        "town",
        "village",
        "castle",
        "shore",
        "fen",
    ],
    "objects": [
        "door",
        "chest",
        "locked-chest",
        "table",
        "bed",
        "torch",
        "lantern",
        "rope",
        "ladder",
        "stairs-up",
        "stairs-down",
        "statue",
        "altar",
        "pillar",
        "cart",
        "boat",
        "tree",
        "rock",
        "bones",
        "campfire",
        "well",
        "bridge",
        "gate",
        "portcullis",
        "hatch",
    ],
    "items": [
        "weapon",
        "bow",
        "shield",
        "armor",
        "potion",
        "scroll",
        "coin",
        "gem",
        "key",
        "food",
        "tool",
        "book",
        "lantern-item",
        "generic-item",
    ],
    "tokens": ["pc", "npc", "hireling", "monster", "group", "neutral"],
}

_EXTERNAL_REF = re.compile(r"(https?:|data:|//)", re.IGNORECASE)


def _registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _svg_files() -> list[Path]:
    return sorted(ICON_DIR.glob("*/*.svg"))


def test_every_registry_entry_has_a_file():
    registry = _registry()
    assert registry, "registry should not be empty"
    for icon_id, entry in registry.items():
        assert {"category", "path", "label"} <= entry.keys(), icon_id
        assert entry["label"].strip(), icon_id
        assert (ICON_DIR / entry["path"]).is_file(), f"{icon_id}: missing {entry['path']}"
        assert entry["path"].startswith(entry["category"] + "/"), icon_id


def test_every_svg_on_disk_is_registered():
    registered = {entry["path"] for entry in _registry().values()}
    on_disk = {p.relative_to(ICON_DIR).as_posix() for p in _svg_files()}
    assert on_disk == registered


@pytest.mark.parametrize("svg_file", _svg_files(), ids=lambda p: p.name)
def test_svg_is_well_formed_and_self_contained(svg_file: Path):
    root = ET.fromstring(svg_file.read_text(encoding="utf-8"))
    assert root.tag == SVG_NS + "svg"
    assert root.get("viewBox") == "0 0 64 64"

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        assert tag not in {"script", "image", "foreignObject"}, svg_file.name
        for name, value in element.attrib.items():
            local = name.rsplit("}", 1)[-1]
            if local in {"href", "src"}:
                assert value.startswith("#"), f"{svg_file.name}: external {local}={value}"
            if local == "style" or local == "fill" or local == "stroke":
                assert "url(" not in value or "url(#" in value, svg_file.name
            assert not _EXTERNAL_REF.search(value), f"{svg_file.name}: {name}={value}"


def test_icon_path_unknown_id_raises_key_error():
    with pytest.raises(KeyError):
        icon_path("no-such-icon")


def test_icon_path_returns_static_url_for_known_id():
    assert icon_path("forest") == "/static/icons/terrain/forest.svg"
    assert icon_path("gem") == "/static/icons/items/gem.svg"


@pytest.mark.parametrize(
    ("category", "icon_id"),
    [(category, icon_id) for category, ids in REQUIRED.items() for icon_id in ids],
)
def test_required_icon_is_in_registry(category: str, icon_id: str):
    registry = _registry()
    assert icon_id in registry, f"missing required {category} icon {icon_id}"
    assert registry[icon_id]["category"] == category
    assert icon_path(icon_id).startswith("/static/icons/" + category + "/")
