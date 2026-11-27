"""Tests for icon resolution: fallbacks, item kinds, and the campaign pack."""

import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.icons import FALLBACK_TEMPLATE, resolve_icon, template_fallback
from cygnusloom.web.icons import (
    PLACE_FALLBACK_ICON,
    WEAPON_ICON,
    icon_path,
    item_icon_id,
    place_icon_path,
)
from tests.test_combat import PC, _fight_client


def test_icon_path_unknown_id_raises_key_error():
    with pytest.raises(KeyError):
        icon_path("no-such-icon")


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


def test_item_icon_id_prefers_the_rule_set_icon_for_a_kind():
    assert item_icon_id("relic", "", {"relic": "potion"}) == "potion"
    assert item_icon_id("spellbook", "", {"spellbook": "tool"}) == "tool"
    assert item_icon_id("spellbook", "", {}) == "book"


def test_place_without_icon_uses_fallback():
    assert place_icon_path(None) == icon_path(PLACE_FALLBACK_ICON)
    assert place_icon_path("forest") == icon_path("forest")


def test_looks_like_inherits_an_icon_through_a_chain():
    entries = {
        "goblin": (None, "goblin-kind"),
        "goblin-kind": (None, "beast-kind"),
        "beast-kind": ("beast", None),
    }
    assert resolve_icon(None, "goblin", label="monster", entries=entries) == "beast"
    # A registered icon id also works as a direct looks_like target.
    assert resolve_icon(None, "humanoid", label="monster", entries=entries) == "humanoid"
    assert resolve_icon(None, "npc", label="monster", entries=entries, fallback="monster") == (
        "npc"
    )


def test_looks_like_rejects_missing_targets_and_cycles():
    with pytest.raises(ContentError, match="names no icon or definition"):
        resolve_icon(None, "nowhere", label="monster", entries={})
    with pytest.raises(ContentError, match="forms a cycle"):
        resolve_icon(
            None,
            "a",
            label="monster",
            entries={"a": (None, "b"), "b": (None, "a")},
        )


def test_template_icons_fall_back_by_kind():
    assert template_fallback("background") == "scroll"
    assert template_fallback("class") == "sword"
    assert template_fallback("class2") == "sword"
    assert template_fallback("tradition") == "rune-circle"
    assert template_fallback("kit") == "backpack"
    assert template_fallback("playbook") == "book"
    assert template_fallback("homebrew") == FALLBACK_TEMPLATE


def test_campaign_pack_draws_item_icons(client_factory: Callable[[], TestClient]):
    client, campaign_id, tools = _fight_client(client_factory, rolled=False)
    tools.give_item(PC, "Rope")
    page = client.get(f"/campaigns/{campaign_id}").text

    start = page.index('id="panel-inventory"')
    inventory = page[start : page.index("</section>", start)]
    weapon = f'<img class="item-icon" src="{icon_path(WEAPON_ICON)}" alt="">'
    assert weapon in inventory
    assert (
        '<a href="#compendium-items-sword" data-view-link="compendium"><strong>Sword</strong></a>'
        in inventory
    )
    # Each carried item shows the icon its own definition names, not just the weapon fallback.
    assert f'src="{icon_path("rope")}"' in inventory
    assert inventory.count('class="item-icon"') >= 2
    assert re.search(
        r"<li[^>]*>\s*(?:<img[^>]*>\s*)?<a[^>]*data-view-link=\"compendium\"[^>]*>"
        r"<strong>Rope</strong></a>",
        inventory,
    )
