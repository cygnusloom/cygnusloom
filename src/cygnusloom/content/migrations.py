"""Migrate content written before items were their own definition type.

In version 5 an item is a `type: "item"` definition. Before that, items were `feature`
definitions with `feature_type: "item"`, and the marketplace described the same things again
as `shop_item` entries that copied a feature. This module rewrites a content tree in place:

* every item feature becomes an item entry;
* every `shop_item` becomes an item, or merges its price, category, and armor mode into the
  item it copied;
* items move into `items/<kind>.json` files, one per weapon, armor, coin, and gear kind;
* the `item` feature type and the file defaults that made features items are removed.

The migration is idempotent: a tree that has no item features or shop items is left alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cygnusloom.content.character import ITEM_KEYS
from cygnusloom.content.definitions import _matches_defaults, read_definitions

ITEM_FIELDS = frozenset(ITEM_KEYS) - {"type", "id"}
# Legacy item features carried these feature-only fields; neither means anything for an item.
LEGACY_ONLY_FIELDS = frozenset({"feature_type", "rank"})
SHOP_FIELDS = frozenset(
    {
        "name",
        "category",
        "price",
        "size",
        "armor",
        "armor_mode",
        "damage",
        "uses",
        "description",
        "timer_hours",
        "timer_label",
        "encumbrance_checks",
        "icon",
        "looks_like",
    }
)
ITEM_FILES = ("currency", "weapons", "armor", "gear")


@dataclass
class MigrationReport:
    """What one migration changed, so a command can report it."""

    converted: int = 0
    catalogued: int = 0
    standalone: int = 0
    template_grants: int = 0
    hook_options: int = 0
    files: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.converted
            or self.catalogued
            or self.standalone
            or self.template_grants
            or self.hook_options
        )

    def summary(self) -> str:
        if not self.changed:
            return "no legacy items found"
        return (
            f"converted {self.converted} item feature(s), merged {self.catalogued} "
            f"catalogue entr{'y' if self.catalogued == 1 else 'ies'}, and added "
            f"{self.standalone} standalone shop item(s), moved "
            f"{self.template_grants} template grant(s) to items, and renamed "
            f"{self.hook_options} hook option(s)"
        )


def _json_files(bundle: Path) -> list[Path]:
    return sorted(bundle.rglob("*.json"))


def _read(path: Path) -> list[Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(raw, list):
        raise TypeError(f"{path}: top level must be a list of objects")
    return raw


def _write(path: Path, data: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _defaults(entries: list[Any]) -> list[Mapping[str, Any]]:
    return [
        entry for entry in entries if isinstance(entry, Mapping) and entry.get("type") == "defaults"
    ]


def _effective_feature_type(entry: Mapping[str, Any], defaults: list[Mapping[str, Any]]) -> str:
    kind = entry.get("feature_type")
    if isinstance(kind, str):
        return kind
    for default in defaults:
        setting = default.get("set")
        when = default.get("when")
        if not isinstance(setting, Mapping) or not isinstance(when, Mapping):
            continue
        candidate = setting.get("feature_type")
        if isinstance(candidate, str) and _matches_defaults(entry, when):
            kind = candidate
    return kind if isinstance(kind, str) else ""


def _item_from_feature(entry: Mapping[str, Any], label: str) -> dict[str, Any]:
    """One item entry from a legacy item feature. Unknown keys are refused, so nothing is
    dropped silently."""
    allowed = ITEM_FIELDS | LEGACY_ONLY_FIELDS | {"type", "id"}
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ValueError(
            f"{label}: legacy item feature has keys an item does not read: "
            f"{', '.join(repr(key) for key in unknown)}"
        )
    item: dict[str, Any] = {"type": "item", "id": entry["id"]}
    for key in ITEM_FIELDS:
        if key in entry:
            item[key] = entry[key]
    return item


def _merge_shop(item: dict[str, Any], shop: Mapping[str, Any], label: str) -> None:
    """Merge one catalogue entry's own fields into its item. A field the shop authors wins."""
    unknown = sorted(set(shop) - SHOP_FIELDS - {"type", "id", "feature"})
    if unknown:
        raise ValueError(
            f"{label}: shop entry has keys an item does not read: "
            f"{', '.join(repr(key) for key in unknown)}"
        )
    for key in SHOP_FIELDS:
        if key in shop:
            item[key] = shop[key]
    if "category" not in item or "price" not in item:
        raise ValueError(f"{label}: a merged item needs both a category and a price")
    item.setdefault("armor_mode", "add")


def _item_from_shop(shop: Mapping[str, Any], label: str) -> dict[str, Any]:
    unknown = sorted(set(shop) - SHOP_FIELDS - {"type", "id"})
    if unknown:
        raise ValueError(
            f"{label}: shop entry has keys an item does not read: "
            f"{', '.join(repr(key) for key in unknown)}"
        )
    item: dict[str, Any] = {"type": "item", "id": shop["id"]}
    for key in SHOP_FIELDS:
        if key in shop:
            item[key] = shop[key]
    item.setdefault("name", str(shop["id"]).replace("_", " ").replace("-", " ").title())
    item.setdefault("description", "")
    if "category" not in item or "price" not in item:
        raise ValueError(f"{label}: a standalone shop item needs both a category and a price")
    item.setdefault("armor_mode", "add")
    return item


def _item_group(item: Mapping[str, Any]) -> str:
    if item.get("currency"):
        return "currency"
    if item.get("damage"):
        return "weapons"
    if item.get("armor") or item.get("armor_category"):
        return "armor"
    return "gear"


def _bundle_label(bundle: Path, definition: Mapping[str, Any]) -> str:
    return f"{bundle.name}: {definition.get('type')} {definition.get('id')}"


def _migrate_bundle(bundle: Path) -> MigrationReport:
    report = MigrationReport()
    files = _json_files(bundle)
    if not files:
        return report
    contents: dict[Path, list[Any]] = {path: _read(path) for path in files}
    definition = read_definitions(bundle)
    currency_ids = {
        str(entry.data["currency"])
        for entry in definition
        if entry.type == "marketplace" and "currency" in entry.data
    }
    defaults_by_file = {path: _defaults(entries) for path, entries in contents.items()}
    feature_files: dict[str, Path] = {}
    linked_shops: dict[str, list[tuple[Path, Mapping[str, Any]]]] = {}
    standalone_shops: list[tuple[Path, Mapping[str, Any]]] = []
    for path, entries in contents.items():
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            kind = entry.get("type")
            if kind == "feature":
                if _effective_feature_type(entry, defaults_by_file[path]) != "item":
                    continue
                ident = str(entry.get("id", ""))
                if ident in feature_files:
                    raise ValueError(f"{bundle.name}: item feature id {ident!r} is repeated")
                feature_files[ident] = path
            elif kind == "shop_item":
                feature = entry.get("feature")
                if isinstance(feature, str) and feature.strip():
                    linked_shops.setdefault(feature, []).append((path, entry))
                else:
                    standalone_shops.append((path, entry))
    # A rule set's template_features may grant items. Move those grants to template_items so
    # an item id never appears among feature grants.
    migrated_ids = set(feature_files) | {entry.id for entry in definition if entry.type == "item"}
    for path, entries in contents.items():
        changed = False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "hooks":
                options = entry.get("options")
                if isinstance(options, Mapping) and options.get("feature") in migrated_ids:
                    renamed = dict(options)
                    renamed["item"] = renamed.pop("feature")
                    entry["options"] = renamed
                    report.hook_options += 1
                    changed = True
                continue
            if entry.get("type") == "template":
                grants = entry.get("features")
                if not isinstance(grants, list):
                    continue
                kept = []
                moved = []
                for grant in grants:
                    ident = grant if isinstance(grant, str) else grant.get("feature")
                    if isinstance(ident, str) and ident in migrated_ids:
                        moved.append(
                            ident
                            if isinstance(grant, str)
                            else {
                                "item": ident,
                                **({"quantity": grant["quantity"]} if "quantity" in grant else {}),
                            }
                        )
                        report.template_grants += 1
                    else:
                        kept.append(grant)
                if moved:
                    entry["items"] = [*entry.get("items", []), *moved]
                    if kept:
                        entry["features"] = kept
                    else:
                        entry.pop("features", None)
                    changed = True
                continue
            if entry.get("type") != "rule_set":
                continue
            grants = entry.get("template_features")
            if not isinstance(grants, Mapping):
                continue
            moved: dict[str, list[Any]] = {}
            kept: dict[str, list[Any]] = {}
            for kind, values in grants.items():
                if not isinstance(values, list):
                    kept[kind] = values
                    continue
                for grant in values:
                    ident = grant if isinstance(grant, str) else grant.get("feature")
                    if isinstance(ident, str) and ident in migrated_ids:
                        moved.setdefault(kind, []).append(
                            ident
                            if isinstance(grant, str)
                            else {
                                "item": ident,
                                **({"quantity": grant["quantity"]} if "quantity" in grant else {}),
                            }
                        )
                        report.template_grants += 1
                    else:
                        kept.setdefault(kind, []).append(grant)
            if moved:
                existing = entry.get("template_items", {})
                merged = {**(existing if isinstance(existing, Mapping) else {})}
                for kind, values in moved.items():
                    merged[kind] = [*merged.get(kind, []), *values]
                entry["template_items"] = merged
                if kept:
                    entry["template_features"] = kept
                else:
                    entry.pop("template_features", None)
                changed = True
        if changed:
            _write(path, entries)
            report.files.append(path.relative_to(bundle).as_posix())

    if not feature_files and not standalone_shops:
        return report

    items: dict[str, dict[str, Any]] = {}
    consumed_features: set[tuple[Path, str]] = set()
    for ident, path in feature_files.items():
        entries = contents[path]
        entry = next(
            entry
            for entry in entries
            if isinstance(entry, Mapping)
            and entry.get("type") == "feature"
            and entry.get("id") == ident
        )
        label = _bundle_label(bundle, entry)
        item = _item_from_feature(entry, label)
        consumed_features.add((path, ident))
        for _, shop in linked_shops.get(ident, []):
            _merge_shop(item, shop, _bundle_label(bundle, shop))
            report.catalogued += 1
        if ident in currency_ids or item.get("category") == "coin":
            item["currency"] = True
        items[ident] = item
        report.converted += 1
    for feature, shops in linked_shops.items():
        if feature in feature_files:
            continue
        raise ValueError(
            f"{bundle.name}: shop item {shops[0][1].get('id')!r} names no item feature {feature!r}"
        )
    for path, shop in standalone_shops:
        label = _bundle_label(bundle, shop)
        item = _item_from_shop(shop, label)
        if item["id"] in currency_ids or item.get("category") == "coin":
            item["currency"] = True
        items[item["id"]] = item
        report.standalone += 1

    consumed_shops = {
        (path, str(shop.get("id"))) for shops in linked_shops.values() for path, shop in shops
    } | {(path, str(shop.get("id"))) for path, shop in standalone_shops}
    for path, entries in contents.items():
        kept: list[Any] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                kept.append(entry)
                continue
            kind = entry.get("type")
            ident = str(entry.get("id", ""))
            if kind == "feature" and (path, ident) in consumed_features:
                continue
            if kind == "shop_item" and (path, ident) in consumed_shops:
                continue
            if kind == "feature_type" and ident == "item":
                continue
            if kind == "defaults" and entry.get("set", {}).get("feature_type") == "item":
                continue
            if kind == "defaults" and entry.get("when", {}).get("type") == "shop_item":
                continue
            kept.append(entry)
        if kept != entries:
            _write(path, kept)
            report.files.append(path.relative_to(bundle).as_posix())

    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in ITEM_FILES}
    for item in items.values():
        grouped[_item_group(item)].append(item)
    if report.changed:
        directory = bundle / "items"
        for name in ITEM_FILES:
            if not grouped[name]:
                continue
            path = directory / f"{name}.json"
            existing = _read(path) if path.exists() else []
            known = {str(entry.get("id")) for entry in existing if isinstance(entry, Mapping)}
            merged = [*existing, *(item for item in grouped[name] if item["id"] not in known)]
            _write(path, merged)
            report.files.append(path.relative_to(bundle).as_posix())
    return report


def _bundles(root: Path) -> list[Path]:
    roots = [root / "rulesets", root / "worlds", root / "modules"]
    if not any(directory.is_dir() for directory in roots):
        return [root]
    bundles: list[Path] = []
    for directory in roots:
        if directory.is_dir():
            bundles.extend(sorted(path for path in directory.iterdir() if path.is_dir()))
    return bundles


def migrate_tree(root: Path) -> list[tuple[str, MigrationReport]]:
    """Migrate every content bundle under `root`. Returns each bundle's report, by folder
    name. A content root with `rulesets`, `worlds`, or `modules` folders is walked; any other
    folder is treated as one bundle."""
    reports = []
    for bundle in _bundles(root):
        if not any(bundle.rglob("*.json")):
            continue
        reports.append((bundle.name, _migrate_bundle(bundle)))
    return reports
