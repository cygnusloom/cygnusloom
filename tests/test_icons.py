"""Tests for the static SVG icon set and its registry."""

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.web.icons import (
    ICON_DIR,
    ITEM_KIND_ICONS,
    PLACE_FALLBACK_ICON,
    REGISTRY_PATH,
    WEAPON_ICON,
    icon_path,
    item_icon_id,
    place_icon_path,
)
from tests.conftest import content_library
from tests.test_combat import PC, _fight_client
from tests.test_world import _described_campaign, _tool

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


def _seed_map_icons() -> list[str]:
    locations = content_library().world_templates["twin-lakes"].locations
    return sorted({location.icon for location in locations if location.icon})


def _assert_registered_with_file(icon_id: str) -> None:
    registry = _registry()
    assert icon_id in registry, f"{icon_id} is not in the registry"
    assert (ICON_DIR / registry[icon_id]["path"]).is_file(), icon_id


@pytest.mark.parametrize("icon_id", _seed_map_icons() + [PLACE_FALLBACK_ICON])
def test_every_map_icon_exists(icon_id: str):
    _assert_registered_with_file(icon_id)


@pytest.mark.parametrize("icon_id", [*ITEM_KIND_ICONS.values(), WEAPON_ICON])
def test_every_item_icon_exists(icon_id: str):
    _assert_registered_with_file(icon_id)


@pytest.mark.parametrize(
    ("kind", "damage", "expected"),
    [
        ("spellbook", "", "book"),
        ("scroll", "", "scroll"),
        ("", "1d8", WEAPON_ICON),
        ("", "", None),
        ("relic", "", None),
        ("fatigue", "", None),
    ],
)
def test_item_icon_id_follows_item_type(kind: str, damage: str, expected: str | None):
    assert item_icon_id(kind, damage) == expected


def test_place_without_icon_uses_fallback():
    assert place_icon_path(None) == icon_path(PLACE_FALLBACK_ICON)
    assert place_icon_path("forest") == icon_path("forest")


def test_campaign_map_draws_a_marker_icon(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _described_campaign(client_factory)
    _tool(app, campaign_id, "move_actor", {"name": "Ada", "location": "Mossbell Hollow"})
    page = client.get(f"/campaigns/{campaign_id}").text

    map_view = page[page.index('data-view="map"') : page.index('data-view="lore"')]
    assert f'<img src="{place_icon_path(None)}" alt="">' in map_view
    assert 'aria-label="MH"' in map_view


def test_campaign_pack_draws_item_icons(client_factory: Callable[[], TestClient]):
    client, campaign_id, tools = _fight_client(client_factory, rolled=False)
    tools.give_item(PC, "Rope")
    page = client.get(f"/campaigns/{campaign_id}").text

    inventory = page[page.index('id="panel-inventory"') : page.index('id="panel-notes"')]
    weapon = f'<img class="item-icon" src="{icon_path(WEAPON_ICON)}" alt=""><strong>Sword</strong>'
    assert weapon in inventory
    assert inventory.count('class="item-icon"') == inventory.count(
        f'src="{icon_path(WEAPON_ICON)}"'
    )
    assert re.search(r"<li[^>]*>\s*<strong>Rope</strong>", inventory)
