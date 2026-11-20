"""Cygnus Loom 4 -> 5 campaign payload rewrites.

Cygnus Loom 5 made items their own content type and moved money out of feature
counts. This module rewrites the 4.x event payloads that still used the old shapes so
the live event classes and the item catalogue can read them without compatibility
branches. It also fills catalogue use counts on old inventory items.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary
from cygnusloom.db.models import Campaign, Event
from cygnusloom.services.events import EVENT_TYPES
from cygnusloom.services.inventory import same_item_name
from cygnusloom.services.state import alias_key, name_key

FROM_MAJOR = 4
TO_MAJOR = 5

CURRENCY_KINDS = frozenset(
    {
        "character.currency_changed",
        "character.debug_currency_added",
    }
)
FEATURE_MAP_KINDS = frozenset(
    {
        "pc.added",
        "npc.added",
        "npc.statted",
        "hireling.added",
        "npc.hired",
        "character.respecced",
    }
)
FEATURE_LEARNED = "character.feature_learned"
ITEM_ADDED = "character.item_added"
ITEM_USES_SET = "character.item_uses_set"
LOCATION_ADDED = "location.added"

# 4.x event fields that 5.x requires. Each value is the meaning 4.x gave an absent field.
REQUIRED_DEFAULTS: Mapping[str, Mapping[str, Any]] = {
    "actor.moved": {"quiet": False},
    "map.token_placed": {"quiet": True},
    "map.token_removed": {"quiet": False},
    "object.placed": {"item_text": ""},
}


def rewrite(
    rows: list[Event],
    library: ContentLibrary,
    campaign: Campaign,
) -> str:
    """Rewrite every 4.x-only payload in place. Returns no source version."""
    rule_set = library.rule_sets[campaign.rule_set]
    spec = rule_set.character
    item_ids = _item_ids(library, campaign, rule_set.character.items)
    feature_ids = item_ids - set(spec.features)
    character_features = _character_features(rows)
    _normalize_locations(rows, library, campaign)

    for row in rows:
        payload = _payload(row)
        defaults = REQUIRED_DEFAULTS.get(row.kind)
        if defaults is not None and _fill_defaults(payload, defaults):
            _store(row, payload)
        if row.kind in CURRENCY_KINDS:
            if _rename_currency(payload):
                _store(row, payload)
            continue
        if row.kind in FEATURE_MAP_KINDS:
            if _drop_item_features(payload, feature_ids):
                _store(row, payload)
            continue
        if row.kind == ITEM_USES_SET:
            # 4.x could set a maximum use count through this event; 5.x keeps it on the item.
            if _drop_keys(payload, ("max_uses",)):
                _store(row, payload)
            continue
        if row.kind == FEATURE_LEARNED:
            owned = character_features.get(name_key(str(payload.get("name", ""))), set())
            if _rewrite_item_learned(row, payload, spec, item_ids, owned):
                _store(row, payload, kind="character.feature_choice_made")
            continue
        if row.kind == ITEM_ADDED and _restore_uses(payload, rule_set.character.items):
            _store(row, payload)
    return ""


def _payload(row: Event) -> dict[str, Any]:
    raw = json.loads(row.payload)
    if not isinstance(raw, dict):
        raise TypeError(f"event {row.seq} has a payload that is not an object")
    return raw


def _store(row: Event, payload: Mapping[str, Any], *, kind: str = "") -> None:
    if kind:
        event_class = EVENT_TYPES.get(kind)
        if event_class is None:
            raise RuntimeError(f"v4 migration maps {row.kind!r} to unknown event {kind!r}")
        row.kind = kind
        row.channel = event_class.channel.value
    row.payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _drop_keys(payload: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """Remove payload keys the current event no longer has."""
    changed = False
    for key in keys:
        if key in payload:
            payload.pop(key)
            changed = True
    return changed


def _fill_defaults(payload: dict[str, Any], defaults: Mapping[str, Any]) -> bool:
    """Add the fields an older payload left out, using the value it used to imply."""
    changed = False
    for key, value in defaults.items():
        if key not in payload:
            payload[key] = value
            changed = True
    return changed


def _normalize_locations(
    rows: list[Event],
    library: ContentLibrary,
    campaign: Campaign,
) -> None:
    """Key every authored route by its destination's location id.

    4.x could key a route by a destination's name or slug, and some old location events
    carried no location id. The destination is resolved from the world first, then from
    the other places the log records. A route whose destination no longer exists is
    dropped; a name-keyed route that remained would never be usable again.
    """
    records = [(row, _payload(row)) for row in rows if row.kind == LOCATION_ADDED]
    if not records:
        return
    world = library.world_templates.get(campaign.world_template)
    world_ids = (
        {alias_key(place.name): place.id for place in world.locations} if world is not None else {}
    )
    for row, payload in records:
        if payload.get("location_id"):
            continue
        name = payload.get("name")
        if not isinstance(name, str):
            continue
        payload["location_id"] = world_ids.get(alias_key(name)) or _place_id(name)
        _store(row, payload)

    ids = {payload["location_id"] for _, payload in records if payload.get("location_id")}
    destinations: dict[str, str] = {}
    for _, payload in records:
        ident = payload.get("location_id")
        name = payload.get("name")
        if not isinstance(ident, str) or not isinstance(name, str):
            continue
        for key in (name_key(name), alias_key(name)):
            destinations.setdefault(key, ident)

    for row, payload in records:
        routes = payload.get("routes")
        if not isinstance(routes, Mapping) or not routes:
            continue
        normalized: dict[str, Any] = {}
        changed = False
        for key, route in routes.items():
            if key in ids:
                target = key
            else:
                target = destinations.get(name_key(key)) or destinations.get(alias_key(key), "")
            if not target:
                changed = True
                continue
            if target != key:
                changed = True
            normalized[target] = route
        if changed:
            payload["routes"] = normalized
            _store(row, payload)


def _place_id(name: str) -> str:
    """A stable id for a place the world data does not name."""
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.casefold()).strip("-")
    return f"place-{slug}"


def _rename_currency(payload: dict[str, Any]) -> bool:
    """4.x called the currency item a `feature`; 5.x calls it `item`."""
    if "feature" not in payload:
        return False
    feature = payload.pop("feature")
    if not payload.get("item"):
        payload["item"] = feature
    return True


def _drop_item_features(payload: dict[str, Any], item_ids: set[str]) -> bool:
    """Old character payloads carried item ids among their feature counts."""
    features = payload.get("features")
    if not isinstance(features, Mapping):
        return False
    kept = {
        feature_id: count for feature_id, count in features.items() if feature_id not in item_ids
    }
    if kept == features:
        return False
    payload["features"] = kept
    return True


def _rewrite_item_learned(
    row: Event,
    payload: dict[str, Any],
    spec: Any,
    item_ids: set[str],
    owned: set[str],
) -> bool:
    """Weapon-mastery picks were learned as item features; record them as choices."""
    feature = payload.get("feature")
    if feature not in item_ids:
        return False
    picker = _item_picker(spec, str(payload.get("cause", "")), owned)
    payload["feature"] = picker[0] if picker else str(payload.get("cause", ""))
    payload["pick"] = picker[1] if picker else "items"
    payload["choice"] = feature
    payload.pop("feature_name", None)
    payload.pop("cause", None)
    payload.pop("repeat", None)
    return True


def _item_picker(spec: Any, cause: str, owned: set[str]) -> tuple[str, str]:
    """The feature and pick that offered an item, matched by its displayed cause.

    Several classes can offer a feature with the same name, such as Weapon Mastery. The
    character's own feature ids disambiguate them; when they do not, the first match in
    rule-set order is used.
    """
    candidates = [
        feature
        for feature in spec.features.values()
        if not cause
        or cause.casefold() == feature.name.casefold()
        or cause.casefold() == feature.id.casefold()
    ]
    for feature in (*[c for c in candidates if c.id in owned], *candidates):
        for pick in feature.picks:
            if pick.feature_type == "item":
                return feature.id, pick.id
    return "", ""


def _character_features(rows: list[Event]) -> dict[str, set[str]]:
    """Every feature id the log gives each character, keyed by folded name."""
    found: dict[str, set[str]] = {}
    for row in rows:
        if row.kind not in FEATURE_MAP_KINDS:
            continue
        payload = _payload(row)
        name = payload.get("name")
        features = payload.get("features")
        if isinstance(name, str) and isinstance(features, Mapping):
            found.setdefault(name_key(name), set()).update(str(key) for key in features)
    return found


def _restore_uses(payload: dict[str, Any], items: Mapping[str, Any]) -> bool:
    """Give old inventory items the catalogue's use count once."""
    name = payload.get("item")
    if not isinstance(name, str):
        return False
    listed = next((item for item in items.values() if same_item_name(item.name, name)), None)
    if listed is None or listed.uses < 1:
        return False
    if int(payload.get("max_uses") or 0) > 0:
        return False
    payload["max_uses"] = listed.uses
    payload["uses"] = listed.uses
    return True


def _item_ids(
    library: ContentLibrary,
    campaign: Campaign,
    rule_set_items: Mapping[str, Any],
) -> set[str]:
    """Every item id this campaign's rule set and world define."""
    items = set(rule_set_items)
    try:
        items.update(library.world_content(campaign.world_template, campaign.rule_set).items)
    except (ContentError, KeyError):
        pass
    return items
