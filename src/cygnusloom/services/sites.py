"""Generated places: the kinds of site a city can offer, the rooms they are made of, and the
scavenging loop that turns one room at a time into granular loot. The site type is content; the
engine rolls the loot, tracks the searched zones, raises the place's danger, and carries what fits.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import ItemTemplate
from cygnusloom.content.library import (
    SITE_CATEGORIES,
    SITE_FINDS,
    SiteTypeDefinition,
    SiteZone,
    ThreatDefinition,
)
from cygnusloom.services import inventory as inventory_service
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.events import SiteSearched, load_state, name_key, record
from cygnusloom.services.state import Item, alias_key

# The conditions a search can leave an item in. A strong search finds things better kept; a
# rushed one finds things worn or broken.
_BAND_CONDITIONS = {
    "10+": ("fine", "serviceable"),
    "7-9": ("serviceable", "worn"),
}


def resolve_site_type(
    site_types: Mapping[str, SiteTypeDefinition], name: str
) -> SiteTypeDefinition | None:
    """A site type by id or display name, without case or punctuation."""
    wanted = alias_key(name)
    if not wanted:
        return None
    for candidate in site_types.values():
        if alias_key(candidate.id) == wanted or alias_key(candidate.name) == wanted:
            return candidate
    return None


def resolve_zone(site_type: SiteTypeDefinition, name: str) -> SiteZone | None:
    """A zone of a site type by id or display name."""
    wanted = alias_key(name)
    for zone in site_type.zones:
        if alias_key(zone.id) == wanted or alias_key(zone.name) == wanted:
            return zone
    return None


def site_type_card(site_type: SiteTypeDefinition) -> dict[str, Any]:
    """The compact line a list of site types shows the GM."""
    return {
        "id": site_type.id,
        "name": site_type.name,
        "category": site_type.category,
        "tier": site_type.tier,
        "find": site_type.find,
        "description": site_type.description,
        "find_hint": site_type.find_hint,
        "tags": list(site_type.tags),
        "zones": len(site_type.zones),
    }


def _item_line(
    entry: Mapping[str, Any], listed: Callable[[str], ItemTemplate | None]
) -> dict[str, Any]:
    template = listed(str(entry.get("item", "")))
    uses = entry.get("uses")
    return {
        "item": str(entry.get("item", "")),
        "name": template.name if template is not None else str(entry.get("item", "")),
        "uses": template.uses if uses is None and template is not None else (uses or 0),
    }


def site_type_view(
    site_type: SiteTypeDefinition,
    listed: Callable[[str], ItemTemplate | None],
    threats: Mapping[str, ThreatDefinition],
) -> dict[str, Any]:
    """Everything the GM needs to make this place unique and run its rooms."""
    return {
        "site_type": site_type_card(site_type),
        "zones": [
            {
                "id": zone.id,
                "name": zone.name,
                "description": zone.description,
                "noise": zone.noise,
                "loot": [_item_line(entry, listed) for entry in zone.loot],
            }
            for zone in site_type.zones
        ],
        "loot": [_item_line(entry, listed) for entry in site_type.loot],
        "threats": [
            {
                "id": threat_id,
                "name": threats[threat_id].name if threat_id in threats else threat_id,
                "harm": threats[threat_id].harm if threat_id in threats else 0,
            }
            for threat_id in site_type.threats
        ],
        "details": list(site_type.details),
        "secrets": list(site_type.secrets),
        "complications": list(site_type.complications),
    }


def filters(
    find: str = "", category: str = "", tier: int | None = None
) -> tuple[str, str, int | None]:
    """Check the filters a site-type list was asked for."""
    find_text = find.strip()
    if find_text and find_text not in SITE_FINDS:
        raise InvalidInput(f"{find!r} is not a find level; choose one of {', '.join(SITE_FINDS)}")
    category_text = category.strip()
    if category_text and category_text not in SITE_CATEGORIES:
        raise InvalidInput(
            f"{category!r} is not a site category; choose one of {', '.join(SITE_CATEGORIES)}"
        )
    tier_value = None if tier is None or tier <= 0 else whole_number(tier, "tier", 1, 5)
    return find_text, category_text, tier_value


def _band(total: int) -> str:
    if total >= 10:
        return "10+"
    if total >= 7:
        return "7-9"
    return "6-"


def _pick(
    rng: random.Random,
    primary: tuple[Mapping[str, Any], ...],
    fallback: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any] | None:
    pool = primary or fallback
    return rng.choice(pool) if pool else None


def roll_site_loot(
    site_type: SiteTypeDefinition,
    zone: SiteZone,
    total: int,
    rng: random.Random,
    listed: Callable[[str], ItemTemplate | None],
    condition_override: str | None = None,
) -> list[dict[str, Any]]:
    """The granular items one search finds. A 10+ gives two; a 7-9 gives one; a 6- gives none.
    A move's poor-condition cost overrides the condition the band would give."""
    band = _band(total)
    if band == "6-":
        return []
    count = 2 if band == "10+" else 1
    found: list[dict[str, Any]] = []
    for index in range(count):
        entry = _pick(
            rng,
            zone.loot if index == 0 or not site_type.loot else site_type.loot,
            zone.loot,
        )
        if entry is None:
            continue
        template = listed(str(entry.get("item", "")))
        if template is None:
            continue
        condition = condition_override or _BAND_CONDITIONS[band][min(index, 1)]
        uses = int(entry.get("uses") or template.uses)
        if condition == "worn" and uses > 1:
            uses = max(1, uses - 1)
        found.append(
            {
                "item": template.id,
                "name": template.name,
                "uses": uses,
                "condition": condition,
                "taken": False,
            }
        )
    return found


def _carried(
    template: ItemTemplate, entry: Mapping[str, Any], item_sizes: Mapping[str, int]
) -> Item:
    """A catalogue item exactly as the search found it: its listed size and description, the uses
    and condition the roll gave, and no live ammunition unless the item is a loaded firearm."""
    if template.size not in item_sizes:
        raise InvalidInput(f"{template.name} has no known item size ({template.size!r})")
    flags: tuple[str, ...] = ()
    if template.armor_mode in {"shield", "shield_flat"}:
        flags = ("shield",)
    elif template.armor > 0:
        flags = ("armor",)
    uses = int(entry.get("uses", template.uses))
    return Item(
        template.name,
        template.size,
        item_sizes[template.size],
        text=template.description,
        damage=template.damage or "",
        uses=uses,
        max_uses=template.uses,
        flags=flags,
        loaded=template.capacity,
        capacity=template.capacity,
        caliber=template.caliber,
        condition=str(entry.get("condition", template.condition)),
    )


def search_site(
    db: Session,
    campaign_id: int,
    site_types: Mapping[str, SiteTypeDefinition],
    character_name: str,
    site: str,
    zone: str,
    total: int,
    rng: random.Random,
    *,
    listed: Callable[[str], ItemTemplate | None],
    inventory: Any,
    item_sizes: Mapping[str, int],
    take: bool,
    condition: str = "",
) -> dict[str, Any]:
    """Search one zone of a typed site. The zone is marked searched, the site's danger rises, and
    the loot the roll finds is carried when it fits. Returns the loot, the danger, the complication
    the site raises, and the zones still unsearched."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(character_name))
    if character is None:
        raise InvalidInput(f"there is no character named {character_name!r}")
    place = state.places.get(name_key(site))
    if place is None:
        raise InvalidInput(f"there is no location named {site!r}")
    if place.site_type is None:
        raise InvalidInput(
            f"{place.name} has no site type; create it with create_location's site_type, "
            "or search an ordinary place with roll_move and give_item"
        )
    site_type = resolve_site_type(site_types, place.site_type)
    if site_type is None:
        raise InvalidInput(f"{place.name}'s site type {place.site_type!r} is not in this rule set")
    if character.location is None or name_key(character.location) != name_key(place.name):
        raise InvalidInput(f"{character.name} is not at {place.name}")
    chosen_zone: SiteZone | None = None
    if zone.strip():
        chosen_zone = resolve_zone(site_type, zone)
        if chosen_zone is None:
            names = ", ".join(zone.name for zone in site_type.zones)
            raise InvalidInput(
                f"{site_type.name} has no zone named {zone!r}; the zones are {names}"
            )
        if chosen_zone.id in place.searched_zones:
            left = [z.name for z in site_type.zones if z.id not in place.searched_zones]
            raise InvalidInput(
                f"{chosen_zone.name} at {place.name} has already been searched; "
                f"the unsearched zones are {', '.join(left) or 'none'}"
            )
    else:
        chosen_zone = next(
            (zone for zone in site_type.zones if zone.id not in place.searched_zones), None
        )
        if chosen_zone is None:
            raise InvalidInput(f"every zone at {place.name} has already been searched")
    roll = whole_number(total, "total", -30, 60)
    danger = min(6, place.site_danger + 1)
    found = roll_site_loot(
        site_type, chosen_zone, roll, rng, listed, condition.strip().lower() or None
    )
    searched = [*place.searched_zones, chosen_zone.id]
    left_behind: list[dict[str, Any]] = []
    if take and inventory is not None:
        for entry in found:
            template = listed(str(entry["item"]))
            if template is None:
                continue
            if not inventory_service.fits_listed(
                db, campaign_id, inventory, item_sizes, character.name, template
            ):
                entry["left_because"] = f"{character.name}'s pack is full"
                left_behind.append(entry)
                continue
            try:
                inventory_service.carry_item(
                    db,
                    campaign_id,
                    inventory,
                    character.name,
                    _carried(template, entry, item_sizes),
                    cause=f"found at {place.name}",
                )
            except InvalidInput as exc:
                entry["left_because"] = str(exc)
                left_behind.append(entry)
            else:
                entry["taken"] = True
    else:
        left_behind.extend(found)
    complication = ""
    if roll <= 6:
        complication = (
            rng.choice(site_type.complications)
            if site_type.complications
            else "The search goes wrong; the GM makes a hard move."
        )
    elif danger in {3, 5, 6}:
        complication = (
            rng.choice(site_type.complications)
            if site_type.complications
            else "The site has noticed the party."
        )
    record(
        db,
        campaign_id,
        SiteSearched(
            site=place.name,
            zone=chosen_zone.id,
            character=character.name,
            total=roll,
            danger=danger,
            searched_zones=searched,
            found=found,
        ),
    )
    db.commit()
    return {
        "site": place.name,
        "site_type": site_type.id,
        "zone": chosen_zone.id,
        "zone_name": chosen_zone.name,
        "character": character.name,
        "total": roll,
        "band": _band(roll),
        "danger": danger,
        "danger_max": 6,
        "searched_zones": searched,
        "remaining_zones": [z.name for z in site_type.zones if z.id not in searched],
        "found": found,
        "left_behind": left_behind,
        "complication": complication,
        "suggested_threats": list(site_type.threats),
        "zone_noise": chosen_zone.noise,
        "cleared": len(searched) >= len(site_type.zones),
    }
