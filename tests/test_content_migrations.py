"""The content migration that turns legacy item features and shop items into items."""

from __future__ import annotations

import json
from pathlib import Path

from cygnusloom.content.library import ContentLibrary
from cygnusloom.content.migrations import migrate_tree
from tests.support import content_tree


def _legacy_tree(tmp_path: Path) -> Path:
    return content_tree(
        tmp_path,
        rule_set={
            "template_features": {
                "background": [
                    "gold",
                    {"feature": "sword", "quantity": "2"},
                ]
            }
        },
        definitions=[
            {"type": "item_size", "id": "petty", "slots": 0},
            {"type": "item_size", "id": "normal", "slots": 1},
            {
                "type": "feature",
                "id": "gold",
                "name": "Gold Pieces",
                "feature_type": "item",
                "description": "A purse of coins.",
                "size": "petty",
            },
            {
                "type": "feature",
                "id": "sword",
                "name": "Sword",
                "feature_type": "item",
                "description": "A well-balanced blade.",
                "size": "normal",
                "damage": "1d6",
                "uses": 1,
            },
            {
                "type": "shop_item",
                "id": "sword",
                "feature": "sword",
                "category": "weapons",
                "price": 10,
            },
            {"type": "shop_item", "id": "torch", "category": "gear", "price": 1},
            {"type": "marketplace", "id": "market", "currency": "gold"},
        ],
    )


def _entries(path: Path) -> list[dict[str, object]]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_legacy_item_features_and_shop_items_become_items(tmp_path):
    root = _legacy_tree(tmp_path)
    reports = migrate_tree(root)

    report = dict(reports)["testset"]
    assert report is not None
    assert report.changed
    assert (report.converted, report.catalogued, report.standalone) == (2, 1, 1)
    assert report.template_grants == 2

    bundle = root / "rulesets" / "testset"
    weapons = {entry["id"]: entry for entry in _entries(bundle / "items" / "weapons.json")}
    sword = weapons["sword"]
    assert sword["type"] == "item"
    assert sword["damage"] == "1d6"
    assert (sword["category"], sword["price"]) == ("weapons", 10)

    currency = _entries(bundle / "items" / "currency.json")
    assert [(entry["id"], entry["currency"]) for entry in currency] == [("gold", True)]

    gear = _entries(bundle / "items" / "gear.json")
    assert [(entry["id"], entry["category"], entry["price"]) for entry in gear] == [
        ("torch", "gear", 1)
    ]

    rule_set = _entries(bundle / "rule_set.json")[0]
    assert "template_features" not in rule_set
    assert rule_set["template_items"] == {
        "background": ["gold", {"item": "sword", "quantity": "2"}]
    }
    assert all(entry.get("type") != "shop_item" for entry in _entries(bundle / "rule_set.json"))
    assert all(
        entry.get("feature_type") != "item"
        for entry in _entries(bundle / "rule_set.json")
        if entry.get("type") == "feature"
    )


def test_the_migrated_content_loads(tmp_path):
    root = _legacy_tree(tmp_path)
    migrate_tree(root)

    library = ContentLibrary.load(root)
    spec = library.rule_sets["testset"].character
    assert spec.items["sword"].damage == "1d6"
    assert (spec.items["sword"].category, spec.items["sword"].price) == ("weapons", 10)
    assert spec.items["gold"].currency
    assert library.rule_sets["testset"].marketplace is not None


def test_migrating_twice_changes_nothing(tmp_path):
    root = _legacy_tree(tmp_path)
    migrate_tree(root)
    bundle = root / "rulesets" / "testset"
    before = {path.relative_to(bundle): path.read_bytes() for path in bundle.rglob("*.json")}

    reports = migrate_tree(root)

    assert all(not report.changed for _, report in reports)
    after = {path.relative_to(bundle): path.read_bytes() for path in bundle.rglob("*.json")}
    assert after == before
