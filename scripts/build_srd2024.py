#!/usr/bin/env python3
"""Build the dnd-srd-2024 rule set's JSON from the cached Open5e SRD 5.2 records.

The SRD PDF is kept in the rule set directory; this script turns the API records into the
engine's content definitions. Run it from the repository root:

    .venv/bin/python scripts/build_srd2024.py

It reads /tmp/srd2024_raw (created by scripts/fetch_srd2024.py) and writes the rule set's JSON
files. The generated files are committed; this script is kept so the content can be regenerated
when the source data or the engine's schema changes.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW = Path("/tmp/srd2024_raw")
OUT = ROOT / "src/cygnusloom/data/rulesets/dnd-srd-2024"

ABILITIES = {
    "strength": "str",
    "dexterity": "dex",
    "constitution": "con",
    "intelligence": "int",
    "wisdom": "wis",
    "charisma": "cha",
}
ABILITY_NAMES = {
    "str": "Strength",
    "dex": "Dexterity",
    "con": "Constitution",
    "int": "Intelligence",
    "wis": "Wisdom",
    "cha": "Charisma",
}
SKILLS = {
    "acrobatics": ("Acrobatics", "dex"),
    "animal_handling": ("Animal Handling", "wis"),
    "arcana": ("Arcana", "int"),
    "athletics": ("Athletics", "str"),
    "deception": ("Deception", "cha"),
    "history": ("History", "int"),
    "insight": ("Insight", "wis"),
    "intimidation": ("Intimidation", "cha"),
    "investigation": ("Investigation", "int"),
    "medicine": ("Medicine", "wis"),
    "nature": ("Nature", "int"),
    "perception": ("Perception", "wis"),
    "performance": ("Performance", "cha"),
    "persuasion": ("Persuasion", "cha"),
    "religion": ("Religion", "int"),
    "sleight_of_hand": ("Sleight of Hand", "dex"),
    "stealth": ("Stealth", "dex"),
    "survival": ("Survival", "wis"),
}
SKILL_ICONS = {
    "acrobatics": "boots",
    "animal_handling": "horse",
    "arcana": "book",
    "athletics": "rope",
    "deception": "speech",
    "history": "scroll",
    "insight": "eye",
    "intimidation": "fist",
    "investigation": "eye",
    "medicine": "potion",
    "nature": "plant",
    "perception": "eye",
    "performance": "lute",
    "persuasion": "handshake",
    "religion": "altar",
    "sleight_of_hand": "ring",
    "stealth": "boots",
    "survival": "campfire",
}
CLASS_ICONS = {
    "barbarian": "axe",
    "bard": "lute",
    "cleric": "altar",
    "druid": "plant",
    "fighter": "sword",
    "monk": "fist",
    "paladin": "shield",
    "ranger": "bow",
    "rogue": "dagger",
    "sorcerer": "crystal",
    "warlock": "eye",
    "wizard": "staff",
}
SPECIES_ICONS = {
    "dragonborn": "dragon",
    "dwarf": "helm",
    "elf": "tree",
    "gnome": "mushroom",
    "goliath": "mountain",
    "halfling": "campfire",
    "human": "humanoid",
    "orc": "axe",
    "tiefling": "spirit",
}
CREATURE_ICONS = {
    "aberration": "aberration",
    "beast": "beast",
    "celestial": "spirit",
    "construct": "construct",
    "dragon": "dragon",
    "elemental": "elemental",
    "fey": "fey",
    "fiend": "spirit",
    "giant": "giant",
    "humanoid": "humanoid",
    "monstrosity": "monster",
    "ooze": "ooze",
    "plant": "plant",
    "swarm": "swarm",
    "undead": "undead",
}
SIZE_SLOTS = {
    "tiny": "tiny",
    "small": "small",
    "medium": "normal",
    "large": "large",
    "huge": "huge",
    "gargantuan": "massive",
}
RARITY_PRICE_GP = {
    "common": 100,
    "uncommon": 400,
    "rare": 4000,
    "very-rare": 40000,
    "legendary": 200000,
    "artifact": 500000,
}

CLASS_SKILLS = {
    "barbarian": [
        "animal_handling",
        "athletics",
        "intimidation",
        "nature",
        "perception",
        "survival",
    ],
    "bard": list(SKILLS),
    "cleric": ["history", "insight", "medicine", "persuasion", "religion"],
    "druid": [
        "animal_handling",
        "arcana",
        "insight",
        "medicine",
        "nature",
        "perception",
        "religion",
        "survival",
    ],
    "fighter": [
        "acrobatics",
        "animal_handling",
        "athletics",
        "history",
        "insight",
        "intimidation",
        "perception",
        "survival",
    ],
    "monk": ["acrobatics", "athletics", "history", "insight", "religion", "stealth"],
    "paladin": ["athletics", "insight", "intimidation", "medicine", "persuasion", "religion"],
    "ranger": [
        "animal_handling",
        "athletics",
        "insight",
        "investigation",
        "nature",
        "perception",
        "stealth",
        "survival",
    ],
    "rogue": [
        "acrobatics",
        "athletics",
        "deception",
        "insight",
        "intimidation",
        "investigation",
        "perception",
        "persuasion",
        "sleight_of_hand",
        "stealth",
    ],
    "sorcerer": ["arcana", "deception", "insight", "intimidation", "persuasion", "religion"],
    "warlock": ["arcana", "deception", "history", "intimidation", "investigation", "nature",
                "religion"],
    "wizard": ["arcana", "history", "insight", "investigation", "medicine", "nature", "religion"],
}

CLASS_SKILL_COUNTS = {
    "barbarian": 2,
    "bard": 3,
    "cleric": 2,
    "druid": 2,
    "fighter": 2,
    "monk": 2,
    "paladin": 2,
    "ranger": 3,
    "rogue": 4,
    "sorcerer": 2,
    "warlock": 2,
    "wizard": 2,
}

# The class's A starting kit: catalogue names, worn armor, wielded shield, and gold pieces.
CLASS_KITS = {
    "barbarian": {
        "items": ["Greataxe", "Handaxe", "Handaxe", "Handaxe", "Handaxe", "Explorer's Pack"],
        "equip": [],
        "gold": 15,
    },
    "bard": {
        "items": ["Dagger", "Dagger", "Entertainer's Pack", "Musical Instrument"],
        "equip": ["Leather Armor"],
        "gold": 19,
    },
    "cleric": {
        "items": ["Mace", "Holy Symbol", "Priest's Pack"],
        "equip": ["Chain Shirt", "Shield"],
        "gold": 7,
    },
    "druid": {
        "items": ["Sickle", "Quarterstaff", "Explorer's Pack", "Herbalism Kit"],
        "equip": ["Leather Armor", "Shield"],
        "gold": 9,
    },
    "fighter": {
        "items": ["Greatsword", "Flail", "Javelin", "Javelin", "Javelin", "Javelin",
                  "Javelin", "Javelin", "Javelin", "Javelin", "Dungeoneer's Pack"],
        "equip": ["Chain Mail"],
        "gold": 4,
    },
    "monk": {
        "items": ["Spear", "Dagger", "Dagger", "Dagger", "Dagger", "Dagger",
                  "Explorer's Pack"],
        "equip": [],
        "gold": 11,
    },
    "paladin": {
        "items": ["Longsword", "Javelin", "Javelin", "Javelin", "Javelin", "Javelin",
                  "Javelin", "Holy Symbol", "Priest's Pack"],
        "equip": ["Chain Mail", "Shield"],
        "gold": 9,
    },
    "ranger": {
        "items": ["Scimitar", "Shortsword", "Longbow", "Arrows (20)", "Quiver",
                  "Explorer's Pack"],
        "equip": ["Studded Leather Armor"],
        "gold": 7,
    },
    "rogue": {
        "items": ["Dagger", "Dagger", "Shortsword", "Shortbow", "Arrows (20)", "Quiver",
                  "Thieves' Tools", "Burglar's Pack"],
        "equip": ["Leather Armor"],
        "gold": 8,
    },
    "sorcerer": {
        "items": ["Spear", "Dagger", "Dagger", "Arcane Focus", "Dungeoneer's Pack"],
        "equip": [],
        "gold": 28,
    },
    "warlock": {
        "items": ["Sickle", "Dagger", "Dagger", "Arcane Focus", "Book", "Scholar's Pack"],
        "equip": ["Leather Armor"],
        "gold": 15,
    },
    "wizard": {
        "items": ["Dagger", "Dagger", "Arcane Focus", "Robe", "Spellbook", "Scholar's Pack"],
        "equip": [],
        "gold": 5,
    },
}

CLASS_ROLES = {
    "barbarian": "A furious warrior who trades armour for raw stamina and rage.",
    "bard": "A spellcaster whose music and wit inspire allies and unravel foes.",
    "cleric": "A divine spellcaster who channels a deity's power to heal and smite.",
    "druid": "A nature spellcaster who shapes the wild and takes beast form.",
    "fighter": "A disciplined master of weapons and armour.",
    "monk": "An unarmed warrior who turns inner focus into speed and power.",
    "paladin": "A holy warrior bound by an oath, blending steel and divine magic.",
    "ranger": "A hunter and tracker who guards the wild places.",
    "rogue": "A precise striker who thrives on stealth, skill, and opportunity.",
    "sorcerer": "A spellcaster whose magic is inborn and shaped by will.",
    "warlock": "A spellcaster bound to a mysterious patron.",
    "wizard": "A scholar of magic who prepares spells from a spellbook.",
}

CANTrips = {
    "barbarian": [0] * 20,
    "bard": [2, 2, 2, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],
    "cleric": [3, 3, 3, 4, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5],
    "druid": [2, 2, 2, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],
    "fighter": [0] * 20,
    "monk": [0] * 20,
    "paladin": [0] * 20,
    "ranger": [0] * 20,
    "rogue": [0] * 20,
    "sorcerer": [4, 4, 4, 5, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6],
    "warlock": [2, 2, 2, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4],
    "wizard": [3, 3, 3, 4, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5],
}
CLASS_CANTRIPS = CANTrips

CLASS_SPELLS = {
    "barbarian": [0] * 20,
    "bard": [4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 16, 18, 19, 19, 20, 20, 22, 22, 22],
    "cleric": [4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 16, 16, 17, 17, 18, 18, 19, 20, 21, 22],
    "druid": [4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 16, 16, 17, 17, 18, 18, 19, 20, 21, 22],
    "fighter": [0] * 20,
    "monk": [0] * 20,
    "paladin": [2, 3, 4, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13],
    "ranger": [2, 3, 4, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13],
    "rogue": [0] * 20,
    "sorcerer": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
    "warlock": [2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 11, 11, 12, 12, 13, 13, 14, 14, 15, 15],
    "wizard": [4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 16, 16, 17, 18, 19, 20, 21, 22, 23, 24],
}

XP_STANDARD = [
    0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000,
    85000, 100000, 120000, 140000, 165000, 195000, 225000, 265000, 305000, 355000,
]

FULL_SLOTS = [
    [2, 0, 0, 0, 0, 0, 0, 0, 0],
    [3, 0, 0, 0, 0, 0, 0, 0, 0],
    [4, 2, 0, 0, 0, 0, 0, 0, 0],
    [4, 3, 0, 0, 0, 0, 0, 0, 0],
    [4, 3, 2, 0, 0, 0, 0, 0, 0],
    [4, 3, 3, 0, 0, 0, 0, 0, 0],
    [4, 3, 3, 1, 0, 0, 0, 0, 0],
    [4, 3, 3, 2, 0, 0, 0, 0, 0],
    [4, 3, 3, 3, 1, 0, 0, 0, 0],
    [4, 3, 3, 3, 2, 0, 0, 0, 0],
    [4, 3, 3, 3, 2, 1, 0, 0, 0],
    [4, 3, 3, 3, 2, 1, 0, 0, 0],
    [4, 3, 3, 3, 2, 1, 1, 0, 0],
    [4, 3, 3, 3, 2, 1, 1, 0, 0],
    [4, 3, 3, 3, 2, 1, 1, 1, 0],
    [4, 3, 3, 3, 2, 1, 1, 1, 0],
    [4, 3, 3, 3, 2, 1, 1, 1, 1],
    [4, 3, 3, 3, 3, 1, 1, 1, 1],
    [4, 3, 3, 3, 3, 2, 1, 1, 1],
    [4, 3, 3, 3, 3, 2, 2, 1, 1],
]
HALF_SLOTS = [
    [2, 0, 0, 0, 0, 0, 0, 0, 0],
    [2, 0, 0, 0, 0, 0, 0, 0, 0],
    [3, 0, 0, 0, 0, 0, 0, 0, 0],
    [3, 0, 0, 0, 0, 0, 0, 0, 0],
    [4, 2, 0, 0, 0, 0, 0, 0, 0],
    [4, 2, 0, 0, 0, 0, 0, 0, 0],
    [4, 3, 0, 0, 0, 0, 0, 0, 0],
    [4, 3, 0, 0, 0, 0, 0, 0, 0],
    [4, 3, 2, 0, 0, 0, 0, 0, 0],
    [4, 3, 2, 0, 0, 0, 0, 0, 0],
    [4, 3, 3, 0, 0, 0, 0, 0, 0],
    [4, 3, 3, 0, 0, 0, 0, 0, 0],
    [4, 3, 3, 1, 0, 0, 0, 0, 0],
    [4, 3, 3, 1, 0, 0, 0, 0, 0],
    [4, 3, 3, 2, 0, 0, 0, 0, 0],
    [4, 3, 3, 2, 0, 0, 0, 0, 0],
    [4, 3, 3, 3, 1, 0, 0, 0, 0],
    [4, 3, 3, 3, 1, 0, 0, 0, 0],
    [4, 3, 3, 3, 2, 0, 0, 0, 0],
    [4, 3, 3, 3, 2, 0, 0, 0, 0],
]
PACT_SLOTS = [
    [1, 1],
    [2, 1],
    [2, 2],
    [2, 2],
    [2, 3],
    [2, 3],
    [2, 4],
    [2, 4],
    [2, 5],
    [2, 5],
    [3, 5],
    [3, 5],
    [3, 5],
    [3, 5],
    [3, 5],
    [3, 5],
    [4, 5],
    [4, 5],
    [4, 5],
    [4, 5],
]


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((RAW / f"{name}.json").read_text(encoding="utf-8"))


def slug(text: str) -> str:
    text = text.strip().casefold()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "x"


def key_id(key: str) -> str:
    return key.removeprefix("srd-2024_")


def write(name: str, data: Any) -> None:
    path = OUT / name
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {name} ({len(data)} definitions)")


def gp_to_sp(value: str | int | float | None) -> int:
    if value in (None, ""):
        return 0
    try:
        return max(0, round(float(value) * 10))
    except (TypeError, ValueError):
        match = re.search(r"[\d.]+", str(value))
        return max(0, round(float(match.group(0)) * 10)) if match else 0


def description_from_traits(record: dict[str, Any]) -> str:
    parts = []
    if record.get("desc"):
        parts.append(record["desc"])
    for trait in record.get("traits", []):
        parts.append(f"{trait['name']}. {trait['desc']}")
    return "\n\n".join(part for part in parts if part).strip() or record.get("name", "")


def spell_description(spell: dict[str, Any]) -> str:
    pieces = [spell.get("desc", "")]
    bits = [
        f"Casting Time: {spell.get('casting_time') or 'action'}",
        f"Range: {spell.get('range_text') or 'self'}",
    ]
    components = [
        name
        for name, used in (
            ("V", spell.get("verbal")),
            ("S", spell.get("somatic")),
            ("M", spell.get("material")),
        )
        if used
    ]
    if spell.get("material_specified"):
        components[-1] = f"M ({spell['material_specified']})"
    if components:
        bits.append(f"Components: {', '.join(components)}")
    duration = spell.get("duration") or "instantaneous"
    if spell.get("concentration"):
        duration = f"Concentration, up to {duration}"
    bits.append(f"Duration: {duration}")
    if spell.get("ritual"):
        bits.append("Ritual")
    if spell.get("saving_throw_ability"):
        bits.append(f"Save: {spell['saving_throw_ability'].title()}")
    if spell.get("damage_roll"):
        types = ", ".join(spell.get("damage_types") or [])
        bits.append(f"Damage: {spell['damage_roll']} {types}".strip())
    if spell.get("attack_roll"):
        bits.append("Spell attack")
    pieces.append(" | ".join(bits))
    if spell.get("higher_level"):
        pieces.append(f"At Higher Levels. {spell['higher_level']}")
    return "\n\n".join(piece for piece in pieces if piece).strip()


def feature(
    ident: str,
    name: str,
    description: str,
    *,
    feature_type: str = "trait",
    tag: str = "",
    rank: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    data = {
        "type": "feature",
        "id": ident,
        "name": name,
        "feature_type": feature_type,
        "description": description,
        "tag": tag,
        "rank": rank,
    }
    data.update(extra)
    return data


def build_rule_set() -> None:
    write(
        "rule_set.json",
        [
            {
                "type": "rule_set",
                "id": "dnd-srd-2024",
                "icon": "sword",
                "name": "Dungeons & Dragons 5.5e (SRD 5.2)",
                "blurb": (
                    "The 2024 D&D rules from the System Reference Document: d20 checks, "
                    "twelve classes, nine species, spells, and a bestiary of monsters."
                ),
                "third_party": True,
                "description": (
                    "Character creation and play for the 2024 edition of the world's best-known "
                    "fantasy roleplaying game, using the System Reference Document 5.2.1. "
                    "Abilities are assigned from the standard array, species and background "
                    "shape the character, and one of twelve classes carries them from level 1 "
                    "to 20. Checks and saves are d20 plus an ability modifier and the "
                    "proficiency bonus; spellcasters track slots level by level. The full SRD "
                    "spell list, bestiary, and magic item catalogue are included. Text adapted "
                    "from the System Reference Document 5.2.1, licensed under CC BY 4.0."
                ),
                "capabilities": {
                    "magic": "prepared",
                    "travel": "none",
                    "wilderness": False,
                    "dungeon": False,
                    "combat": True,
                    "social": ["conditions", "counters", "checks"],
                },
                "plus": "highest",
                "item_sizes": {
                    "tiny": 0,
                    "small": 1,
                    "normal": 1,
                    "large": 2,
                    "huge": 4,
                    "massive": 8,
                },
                "stat_presets": {
                    "abilities": {
                        "formula": "3d6",
                        "swap_group": "abilities",
                    }
                },
                "table_genres": ["generic", "fantasy"],
                "skill_progression": {
                    "untrained": 0,
                    "first_level": 2,
                    "creation_maximum": 4,
                    "maximum": 12,
                },
                "condition_ends": ["rest", "long_rest", "save", "time", "magic"],
                "npc_hp_stat": "hp",
                "npc_hp": {
                    "average": 4,
                    "hardier": 11,
                    "serious": 22,
                    "elite": 45,
                    "heroic": 90,
                },
                "default_campaign": {
                    "rule_set": "dnd-srd-2024",
                    "world_template": "unwritten-world",
                },
                "recommended_worlds": ["unwritten-world"],
            },
            {
                "type": "encounter_grid",
                "id": "dnd-srd-2024",
                "cell_feet": 5,
                "move_feet": 30,
            },
            {
                "type": "python_module",
                "id": "dnd_srd_2024",
                "module": "cygnusloom.rules.dnd_srd_2024",
            },
        ],
    )


def build_stats() -> None:
    bands = [
        [1, -5],
        [2, -4],
        [4, -3],
        [6, -2],
        [8, -1],
        [10, 0],
        [12, 1],
        [14, 2],
        [16, 3],
        [18, 4],
        [20, 5],
        [22, 6],
        [24, 7],
        [26, 8],
        [28, 9],
        [30, 10],
    ]
    standard = {"str": 15, "dex": 14, "con": 13, "int": 12, "wis": 10, "cha": 8}
    stats = [
        {
            "type": "stat",
            "id": ident,
            "name": ABILITY_NAMES[ident],
            "preset": "abilities",
            "standard": standard[ident],
            "modifier": bands,
        }
        for ident in ABILITY_NAMES
    ]
    stats += [
        {
            "type": "stat",
            "id": "hp",
            "name": "Hit Points",
            "formula": "1d1",
            "plus_modifier": "con",
            "minimum": 1,
            "hidden": True,
        },
        {
            "type": "stat",
            "id": "attack_bonus",
            "name": "Proficiency Bonus",
            "formula": "1d1",
            "hidden": True,
        },
    ]
    write("stats.json", stats)


def build_skills() -> None:
    write(
        "skills.json",
        [
            {
                "type": "skill",
                "id": ident,
                "name": name,
                "icon": SKILL_ICONS[ident],
                "description": (
                    f"A {ABILITY_NAMES[ability]} check to {ident.replace('_', ' ')}. "
                    "Add the proficiency bonus when the character is proficient."
                ),
            }
            for ident, (name, ability) in SKILLS.items()
        ],
    )


def build_conditions() -> None:
    conditions = load("conditions")
    out = []
    for condition in conditions:
        descriptions = condition.get("descriptions") or []
        text = descriptions[0]["desc"] if descriptions else condition["name"]
        ends = ["save", "time", "magic"] if condition["key"] != "exhaustion" else ["long_rest"]
        out.append(
            {
                "type": "condition",
                "id": condition["key"],
                "label": condition["name"],
                "description": text,
                "ends": ends,
            }
        )
    write("conditions.json", out)


def build_counters() -> None:
    counters = [
        {
            "type": "counter",
            "id": "level",
            "label": "Level",
            "min": 1,
            "max": 20,
            "initial": 1,
            "description": "The character's class level, from 1 to 20.",
        },
        {
            "type": "counter",
            "id": "xp",
            "label": "Experience",
            "min": 0,
            "initial": 0,
            "description": "Cumulative experience points.",
        },
        {
            "type": "counter",
            "id": "hit_dice",
            "label": "Hit Point Dice",
            "min": 0,
            "max": 20,
            "initial": 0,
            "description": "Hit Point Dice available to spend on a short rest.",
        },
        {
            "type": "counter",
            "id": "death_saves",
            "label": "Death Save Failures",
            "min": 0,
            "max": 3,
            "initial": 0,
            "description": "Failed death saves; three kills the character.",
        },
        {
            "type": "counter",
            "id": "death_successes",
            "label": "Death Save Successes",
            "min": 0,
            "max": 3,
            "initial": 0,
            "description": "Successful death saves; three stabilize the character.",
        },
        {
            "type": "counter",
            "id": "spell_picks",
            "label": "Spells to Learn",
            "min": 0,
            "initial": 0,
            "description": "New leveled spells the character may learn.",
        },
        {
            "type": "counter",
            "id": "cantrip_picks",
            "label": "Cantrips to Learn",
            "min": 0,
            "initial": 0,
            "description": "New cantrips the character may learn.",
        },
        {
            "type": "counter",
            "id": "asi_picks",
            "label": "Ability Score Improvements",
            "min": 0,
            "initial": 0,
            "description": "Feat or ability score choices waiting at this level.",
        },
        {
            "type": "counter",
            "id": "fighting_style_picks",
            "label": "Fighting Style Picks",
            "min": 0,
            "initial": 0,
            "description": "Fighting Style feats the character may choose.",
        },
        {
            "type": "counter",
            "id": "subclass_picks",
            "label": "Subclass Picks",
            "min": 0,
            "initial": 0,
            "description": "A subclass choice waiting at level 3.",
        },
        {
            "type": "counter",
            "id": "expertise_picks",
            "label": "Expertise Picks",
            "min": 0,
            "initial": 0,
            "description": "Proficient skills the character may sharpen into expertise.",
        },
    ]
    for number in range(1, 10):
        counters.append(
            {
                "type": "counter",
                "id": f"slot_{number}",
                "label": f"Level {number} Spell Slots",
                "min": 0,
                "max": 4,
                "initial": 0,
                "description": f"Unspent level {number} spell slots.",
            }
        )
    write("counters.json", counters)


def build_combat() -> None:
    sight = [
        "wall",
        "tree",
        "boulder",
        "pillar",
        "statue",
        "altar",
        "gate",
        "portcullis",
        "bookshelf",
        "cart",
        "fountain",
        "throne",
        "obelisk",
        "pine",
        "cabinet",
        "shelf",
        "shrine",
        "wagon",
        "fireplace",
        "barrel",
        "crate",
        "table",
        "counter",
    ]
    write(
        "combat.json",
        [
            {
                "type": "combat",
                "id": "dnd-srd-2024",
                "damage": [{"kind": "harm", "stat": "hp"}],
                "check_die": "1d20",
                "initiative_stat": "dex",
                "defeat": {
                    "player_character": "unconscious",
                    "player_character_dead": "dead",
                    "important_npc": "unconscious",
                    "npc": "dead",
                    "nonlethal": "unconscious",
                },
                "outside_harm": ["hp"],
                "healing_stat": "hp",
                "hit_mode": "roll_vs_defense",
                "hit_bonus_stat": "attack_bonus",
                "attribute_damage": True,
                "defeat_at_zero": ["hp"],
                "dying_hours": 0,
                "dying_rounds": 0,
                "armor_max": 0,
                "multi_attack": "each",
                "defense_base": 10,
                "defense_modifier": "dex",
                "natural_one_misses": True,
                "natural_max_hits": True,
                "total_defense": 0,
                "range_bands": {"normal": 0, "long": 0},
                "ranges": {"melee": 5, "sight_blocked_by": sight},
                "attacks_per_turn": 1,
                "unarmed": {
                    "damage": "1d1",
                    "names": ["unarmed", "fist", "fists", "punch", "kick"],
                    "attributes": ["str", "dex"],
                },
                "save": {"mode": "roll_over"},
                "initiative": {"die": "1d20", "bonus_stat": "dex", "side": False},
                "initiative_mode": "roll_order",
            }
        ],
    )


def build_rests() -> None:
    write(
        "rest.json",
        [
            {
                "type": "rest",
                "id": "dnd-srd-2024",
                "lengths": {
                    "short": {
                        "hours": 1,
                        "full": False,
                        "restores": [],
                        "clears": [],
                        "ends": ["rest"],
                        "upkeep": False,
                    },
                    "long": {
                        "hours": 8,
                        "full": True,
                        "restores": ["hp"],
                        "clears": [],
                        "ends": ["rest", "long_rest"],
                        "upkeep": False,
                    },
                },
            },
            {
                "type": "hooks",
                "id": "dnd-srd-2024",
                "python_module": "dnd_srd_2024",
                "after_rest": "after_rest",
            },
        ],
    )


def build_inventory() -> None:
    write(
        "inventory.json",
        [
            {
                "type": "inventory",
                "id": "dnd-srd-2024",
                "capacity": 30,
                "penalty_name": "Encumbered",
                "penalty_lasts": "until you drop enough gear",
                "penalty_item": "encumbered",
                "penalty_slots": 0,
            }
        ],
    )


def build_check() -> None:
    write(
        "checks.json",
        [
            {
                "type": "check",
                "id": "ability",
                "label": "Ability check",
                "dice": "1d20",
                "unskilled": 0,
                "default_difficulty": 15,
                "minimum_difficulty": 1,
                "maximum_difficulty": 30,
            }
        ],
    )


def build_tables() -> None:
    tables = [
        {
            "type": "table",
            "id": "all_skills",
            "options": [ident for ident in SKILLS],
        },
        {
            "type": "table",
            "id": "names",
            "options": [
                "Aelric", "Bryn", "Calla", "Daric", "Elowen", "Fenn", "Galen", "Hesta",
                "Ilyana", "Jorunn", "Kael", "Lira", "Marek", "Nessa", "Orin", "Perrin",
                "Quilla", "Rhea", "Sable", "Tamsin", "Ulric", "Vessa", "Wren", "Yara",
            ],
        },
        {
            "type": "campaign_field",
            "id": "xp_rate",
            "name": "Experience",
            "description": "The experience track this campaign follows.",
            "table": "xp_rate",
        },
        {
            "type": "table",
            "id": "xp_rate",
            "options": [
                {
                    "name": "Standard",
                    "description": "Levels every few encounters, at the SRD's standard pace.",
                }
            ],
        },
    ]
    for class_id in CLASS_SKILLS:
        tables.append(
            {
                "type": "table",
                "id": f"skills_{class_id}",
                "options": CLASS_SKILLS[class_id],
            }
        )
    write("tables.json", tables)


def build_feature_types() -> None:
    write(
        "feature_types.json",
        [
            {
                "type": "feature_type",
                "id": "spell",
                "name": "Spell",
                "plural": "Spells",
                "group": "Spells",
                "rank_label": "Level",
                "tag_label": "School",
                "unique": True,
            },
            {
                "type": "feature_type",
                "id": "trait",
                "name": "Trait",
                "plural": "Traits",
                "group": "Traits",
            },
            {
                "type": "feature_type",
                "id": "feat",
                "name": "Feat",
                "plural": "Feats",
                "group": "Feats",
                "rank_label": "Level",
            },
            {
                "type": "feature_type",
                "id": "item",
                "name": "Item",
                "plural": "Items",
                "group": "Items",
                "inventory": True,
            },
        ],
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    build_rule_set()
    build_stats()
    build_skills()
    build_conditions()
    build_counters()
    build_combat()
    build_rests()
    build_inventory()
    build_check()
    build_tables()
    build_feature_types()
    build_equipment()
    spell_lists = build_spells()
    build_feats()
    build_species()
    build_backgrounds()
    build_classes(spell_lists)
    build_monsters()
    build_rules()
    build_prompts()
    build_tool_calls()
    build_clock()
    build_world_terrain()


def weapon_attributes(record: dict[str, Any]) -> list[str]:
    props = {entry["property"]["name"].casefold() for entry in record.get("properties", [])}
    if "finesse" in props:
        return ["str", "dex"]
    if record.get("range") and not any(p in props for p in ("thrown",)):
        return ["dex"]
    if "thrown" in props:
        return ["str", "dex"]
    return ["str"]


def weapon_size(record: dict[str, Any]) -> str:
    props = {entry["property"]["name"].casefold() for entry in record.get("properties", [])}
    return "large" if props & {"heavy", "two-handed"} else "normal"


def weapon_damage(record: dict[str, Any]) -> str:
    dice = str(record.get("damage_dice") or "1")
    return f"{dice}d1" if dice.isdigit() else dice


def weapon_ranges(record: dict[str, Any]) -> dict[str, int]:
    range_value = record.get("range") or 0
    if not range_value:
        return {}
    long_range = record.get("long_range") or 0
    return {"normal": int(range_value), "long": int(long_range or range_value)}


def weapon_reach(record: dict[str, Any]) -> int:
    props = {entry["property"]["name"].casefold() for entry in record.get("properties", [])}
    return 10 if "reach" in props else 5


def weapon_description(record: dict[str, Any]) -> str:
    kind = "Simple" if record.get("is_simple") else "Martial"
    damage_type = (record.get("damage_type") or {}).get("name", "")
    lines = [f"{kind} weapon. Damage {weapon_damage(record)} {damage_type}".strip() + "."]
    for entry in record.get("properties", []):
        prop = entry["property"]
        detail = f" ({entry['detail']})" if entry.get("detail") else ""
        lines.append(f"{prop['name']}{detail}. {prop.get('desc', '')}".strip())
    return "\n\n".join(lines)


def armor_feature_data(record: dict[str, Any]) -> dict[str, Any]:
    name = record["name"]
    if name.casefold() == "shield":
        return {
            "armor": 2,
            "size": "small",
            "armor_mode": "shield_flat",
            "description": "A shield. While you wield it, you gain a +2 bonus to Armor Class.",
        }
    base = int(record.get("ac_base") or 10)
    adds_dex = bool(record.get("ac_add_dexmod"))
    cap = record.get("ac_cap_dexmod")
    cap = 0 if not adds_dex else (int(cap) if cap is not None else None)
    text = f"Armor Class {record.get('ac_display') or base}."
    if record.get("strength_score_required"):
        text += f" Strength {record['strength_score_required']} required to wear it well."
    if record.get("grants_stealth_disadvantage"):
        text += " The wearer has disadvantage on Dexterity (Stealth) checks."
    return {
        "armor": max(0, base - 10),
        "size": "large" if record.get("category") == "heavy" else "normal",
        "defense_dex_cap": cap,
        "armor_mode": "max",
        "description": text,
    }


def item_size_for(record: dict[str, Any]) -> str:
    category = (record.get("category") or {}).get("key", "")
    name = record.get("name", "").casefold()
    if category in ("mount", "land-vehicle", "waterborne-vehicle"):
        return "massive"
    if category in ("ammunition", "potion", "scroll", "ring"):
        return "tiny"
    if category == "equipment-pack":
        return "large"
    if category == "wondrous-item":
        return "small"
    if category == "armor":
        return "normal"
    if category == "weapon":
        return "normal"
    try:
        weight = float(record.get("weight") or 0)
    except (TypeError, ValueError):
        weight = 0
    if weight and weight < 1:
        return "tiny"
    return "normal"


def item_description(record: dict[str, Any]) -> str:
    pieces = [record.get("desc", "")]
    if record.get("requires_attunement"):
        detail = record.get("attunement_detail") or ""
        pieces.append(f"Requires attunement{(' ' + detail) if detail else ''}.")
    if record.get("rarity"):
        pieces.insert(0, f"{record['rarity']['name']} magic item.")
    return "\n\n".join(piece for piece in pieces if piece).strip() or record["name"]


def build_equipment() -> None:
    weapons = load("weapons")
    armor = load("armor")
    items = load("items")
    magic = load("magicitems")
    item_costs = {item["key"]: item.get("cost") for item in items}
    features: list[dict[str, Any]] = [
        feature(
            "silver",
            "Silver Pieces",
            "Silver pieces, the standard coin of the SRD economy: ten silver to a gold piece. "
            "Prices in the catalogue are given in silver.",
            feature_type="item",
            size="tiny",
        ),
        feature(
            "encumbered",
            "Encumbered",
            "Carrying more than the pack holds. Drop enough gear to move freely again.",
            feature_type="trait",
        ),
        # Generic kit items the API leaves out.
        feature(
            "musical-instrument",
            "Musical Instrument",
            "A lute, flute, drum, or another instrument of the player's choosing.",
            feature_type="item",
            size="normal",
        ),
        feature(
            "arcane-focus",
            "Arcane Focus",
            "A crystal, orb, rod, staff, or wand that channels arcane magic.",
            feature_type="item",
            size="small",
        ),
        feature(
            "holy-symbol",
            "Holy Symbol",
            "A divine emblem worn or held to channel a deity's power.",
            feature_type="item",
            size="tiny",
        ),
        feature(
            "spellbook",
            "Spellbook",
            "A leather-bound book of spells, worth 50 gp to a wizard.",
            feature_type="item",
            size="normal",
        ),
    ]
    shop: list[dict[str, Any]] = [
        {"type": "shop_item", "id": "shop-silver", "feature": "silver", "category": "coin",
         "price": 1, "size": "tiny"},
        {"type": "shop_item", "id": "shop-musical-instrument",
         "feature": "musical-instrument", "category": "tool", "price": 300, "size": "normal"},
        {"type": "shop_item", "id": "shop-arcane-focus", "feature": "arcane-focus",
         "category": "focus", "price": 100, "size": "small"},
        {"type": "shop_item", "id": "shop-holy-symbol", "feature": "holy-symbol",
         "category": "focus", "price": 50, "size": "tiny"},
        {"type": "shop_item", "id": "shop-spellbook", "feature": "spellbook",
         "category": "tool", "price": 500, "size": "normal"},
    ]
    for record in weapons:
        ident = f"weapon-{key_id(record['key'])}"
        damage = weapon_damage(record)
        props = {entry["property"]["name"].casefold() for entry in record.get("properties", [])}
        extra: dict[str, Any] = {
            "damage": damage,
            "attributes": weapon_attributes(record),
            "size": weapon_size(record),
        }
        ranges = weapon_ranges(record)
        if ranges:
            extra["ranges"] = ranges
        if weapon_reach(record) > 5:
            extra["reach"] = weapon_reach(record)
        features.append(
            feature(
                ident,
                record["name"],
                weapon_description(record),
                feature_type="item",
                **extra,
            )
        )
        key = record["key"]
        shop.append(
            {
                "type": "shop_item",
                "id": f"shop-{key_id(key)}",
                "feature": ident,
                "category": "weapon",
                "price": gp_to_sp(item_costs.get(key, 0)),
                "armor_mode": "add",
                "size": weapon_size(record),
            }
        )
    for record in armor:
        ident = f"armor-{key_id(record['key'])}"
        data = armor_feature_data(record)
        mode = data.pop("armor_mode")
        features.append(
            feature(
                ident,
                record["name"],
                data.pop("description"),
                feature_type="item",
                **data,
            )
        )
        shop.append(
            {
                "type": "shop_item",
                "id": f"shop-{key_id(record['key'])}",
                "feature": ident,
                "category": "armor",
                "price": gp_to_sp(item_costs.get(record["key"], 0)),
                "armor_mode": mode,
            }
        )
    handled = {record["key"] for record in weapons} | {record["key"] for record in armor}
    for record in items:
        if record["key"] in handled:
            continue
        ident = f"item-{key_id(record['key'])}"
        category = (record.get("category") or {}).get("key", "gear")
        extra: dict[str, Any] = {"size": item_size_for(record)}
        if record.get("weapon") and record["weapon"].get("damage_dice"):
            weapon = record["weapon"]
            extra["damage"] = weapon_damage(weapon)
            extra["attributes"] = weapon_attributes(weapon)
        if record.get("armor") and record["armor"].get("ac_base") is not None:
            data = armor_feature_data(record["armor"])
            data.pop("armor_mode", None)
            data.pop("description", None)
            extra.update(data)
        features.append(
            feature(
                ident,
                record["name"],
                record.get("desc", "") or record["name"],
                feature_type="item",
                **extra,
            )
        )
        shop.append(
            {
                "type": "shop_item",
                "id": f"shop-{key_id(record['key'])}",
                "feature": ident,
                "category": "gear" if category not in ("weapon", "armor") else category,
                "price": gp_to_sp(record.get("cost")),
                "armor_mode": "max",
            }
        )
    for record in magic:
        ident = f"magic-{key_id(record['key'])}"
        extra: dict[str, Any] = {"size": item_size_for(record)}
        mode = "max"
        if record.get("weapon"):
            weapon = record["weapon"]
            extra["damage"] = weapon_damage(weapon)
            extra["attributes"] = weapon_attributes(weapon)
            extra["size"] = weapon_size(weapon)
        if record.get("armor"):
            data = armor_feature_data(record["armor"])
            mode = data.pop("armor_mode", "max")
            data.pop("description", None)
            extra.update(data)
        features.append(
            feature(
                ident,
                record["name"],
                item_description(record),
                feature_type="item",
                **extra,
            )
        )
        rarity = (record.get("rarity") or {}).get("key", "uncommon")
        shop.append(
            {
                "type": "shop_item",
                "id": f"shop-{key_id(record['key'])}",
                "feature": ident,
                "category": "magic",
                "price": gp_to_sp(RARITY_PRICE_GP.get(rarity, 400)),
                "armor_mode": mode,
            }
        )
    definitions = [
        {
            "type": "marketplace",
            "id": "dnd-srd-2024",
            "currency": "silver",
        },
        *features,
        *shop,
    ]
    write("equipment.json", definitions)



def build_spells() -> dict[str, dict[int, list[str]]]:
    spells = load("spells")
    definitions = []
    spell_lists: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    class_ids = {
        "barbarian", "bard", "cleric", "druid", "fighter", "monk", "paladin", "ranger",
        "rogue", "sorcerer", "warlock", "wizard",
    }
    for spell in spells:
        ident = f"spell-{key_id(spell['key'])}"
        definitions.append(
            feature(
                ident,
                spell["name"],
                spell_description(spell),
                feature_type="spell",
                tag=(spell.get("school") or {}).get("key", ""),
                rank=int(spell.get("level") or 0),
            )
        )
        for class_ref in spell.get("classes", []):
            class_id = key_id(class_ref["key"])
            if class_id in class_ids:
                spell_lists[class_id][int(spell.get("level") or 0)].append(ident)
    write("spells.json", definitions)
    return {
        class_id: {rank: sorted(ids) for rank, ids in sorted(ranks.items())}
        for class_id, ranks in spell_lists.items()
    }


def feat_tag(feat: dict[str, Any]) -> str:
    kind = (feat.get("type") or "").casefold()
    if kind == "origin":
        return "origin"
    if kind == "fighting style":
        return "fighting-style"
    if kind == "epic boon":
        return "epic-boon"
    return "general"


def feat_rank(feat: dict[str, Any]) -> int:
    prerequisite = feat.get("prerequisite") or ""
    match = re.search(r"level (\d+)", prerequisite, re.IGNORECASE)
    return int(match.group(1)) if match else 1


def build_feats() -> list[dict[str, Any]]:
    feats = load("feats")
    by_key = {feat["key"]: feat for feat in feats}
    definitions: list[dict[str, Any]] = [
        feature(
            "feat-ability-score-improvement",
            "Ability Score Improvement",
            "Increase one ability score by 2, or two ability scores by 1. No ability can rise "
            "above 20. You may take this feat more than once.",
            feature_type="feat",
            tag="general",
            rank=4,
            repeatable=True,
            picks=[
                {
                    "id": "first",
                    "kind": "stat",
                    "label": "First ability",
                    "grant": "score",
                    "amount": 1,
                    "choices": list(ABILITY_NAMES),
                },
                {
                    "id": "second",
                    "kind": "stat",
                    "label": "Second ability",
                    "grant": "score",
                    "amount": 1,
                    "choices": list(ABILITY_NAMES),
                },
            ],
        )
    ]
    for feat in feats:
        if feat["key"] == "srd-2024_ability-score-improvement":
            continue
        ident = f"feat-{key_id(feat['key'])}"
        benefits = "\n\n".join(
            benefit.get("desc", "") for benefit in feat.get("benefits", [])
        ).strip()
        text = "\n\n".join(part for part in (feat.get("desc", ""), benefits) if part)
        extra: dict[str, Any] = {}
        if ident == "feat-skilled":
            extra["picks"] = [
                {
                    "id": "skills",
                    "kind": "skill",
                    "label": "Skills",
                    "grant": "skill",
                    "amount": 1,
                    "count": 3,
                }
            ]
        definitions.append(
            feature(
                ident,
                feat["name"],
                text or feat["name"],
                feature_type="feat",
                tag=feat_tag(feat),
                rank=feat_rank(feat),
                **extra,
            )
        )
    for skill_id, (skill_name, _) in SKILLS.items():
        definitions.append(
            feature(
                f"expertise-{skill_id}",
                f"Expertise: {skill_name}",
                f"Double the proficiency bonus on {skill_name} checks.",
                feature_type="trait",
                tag="expertise",
                rank=0,
            )
        )
    write("feats.json", definitions)
    return definitions


SPECIES_TRAIT_TAGS = {
    "Dragonborn": "Dragonborn",
    "Dwarf": "Dwarf",
}


def build_species() -> None:
    species = load("species")
    definitions: list[dict[str, Any]] = []
    for record in species:
        species_id = key_id(record["key"])
        trait_ids = []
        for trait in record.get("traits", []):
            ident = f"species-{species_id}-{slug(trait['name'])}"
            extra: dict[str, Any] = {}
            if trait["name"] in ("Draconic Ancestry", "Elven Lineage", "Gnomish Lineage",
                                 "Giant Ancestry", "Fiendish Legacy", "Versatile"):
                extra["picks"] = [
                    {
                        "id": "choice",
                        "kind": "text",
                        "label": trait["name"],
                    }
                ]
            definitions.append(
                feature(
                    ident,
                    f"{record['name']}: {trait['name']}",
                    trait["desc"],
                    feature_type="trait",
                    tag="species",
                    rank=0,
                    **extra,
                )
            )
            trait_ids.append(ident)
        size = next((t["desc"] for t in record["traits"] if t["name"] == "Size"), "Medium")
        speed = next((t["desc"] for t in record["traits"] if t["name"] == "Speed"), "30 feet")
        definitions.append(
            {
                "type": "template",
                "id": species_id,
                "kind": "species",
                "name": record["name"],
                "icon": SPECIES_ICONS.get(species_id, "humanoid"),
                "description": (
                    f"{record['name']}. {size}. Speed {speed}. "
                    + " ".join(t["name"] + "." for t in record["traits"])
                ),
                "features": trait_ids,
            }
        )
    write("species.json", definitions)


def background_abilities(record: dict[str, Any]) -> list[str]:
    for benefit in record.get("benefits", []):
        if benefit.get("type") == "ability_score":
            return [
                ABILITIES[name.strip().casefold()]
                for name in benefit.get("desc", "").split(",")
                if name.strip().casefold() in ABILITIES
            ]
    return list(ABILITY_NAMES)[:3]


def background_skills(record: dict[str, Any]) -> list[str]:
    skills = []
    for benefit in record.get("benefits", []):
        if benefit.get("type") != "skill_proficiency":
            continue
        for name in re.split(r",| and ", benefit.get("desc", "")):
            wanted = name.strip().casefold().replace(" ", "_")
            if wanted in SKILLS:
                skills.append(wanted)
    return skills


def background_feat(record: dict[str, Any]) -> str:
    for benefit in record.get("benefits", []):
        if benefit.get("type") == "feat":
            name = benefit.get("desc", "")
            if name.startswith("Magic Initiate"):
                return "feat-magic-initiate"
            return f"feat-{slug(name)}"
    return ""


def build_backgrounds() -> None:
    records = load("backgrounds")
    definitions: list[dict[str, Any]] = []
    for record in records:
        ident = key_id(record["key"])
        abilities = background_abilities(record)
        skills = background_skills(record)
        feat_id = background_feat(record)
        asi_id = f"background-asi-{ident}"
        definitions.append(
            feature(
                asi_id,
                f"{record['name']}: Ability Scores",
                (
                    "Increase the three abilities this background trains by 1 each: "
                    + ", ".join(ABILITY_NAMES[ability] for ability in abilities)
                    + "."
                ),
                feature_type="trait",
                tag="background",
                picks=[
                    {
                        "id": "scores",
                        "kind": "stat",
                        "label": "Abilities",
                        "grant": "score",
                        "amount": 1,
                        "count": 3,
                        "choices": abilities,
                    }
                ],
            )
        )
        features = [asi_id]
        if feat_id:
            features.append(feat_id)
        tool = next(
            (
                benefit.get("desc", "")
                for benefit in record.get("benefits", [])
                if benefit.get("type") == "tool_proficiency"
            ),
            "",
        )
        equipment = next(
            (
                benefit.get("desc", "")
                for benefit in record.get("benefits", [])
                if benefit.get("type") == "equipment"
            ),
            "",
        )
        description = f"{record['name']}."
        if tool:
            description += f" Tool proficiency: {tool}."
        if equipment:
            description += f" Equipment: {equipment}"
        definitions.append(
            {
                "type": "template",
                "id": ident,
                "kind": "background",
                "name": record["name"],
                "icon": {
                    "acolyte": "altar",
                    "criminal": "key",
                    "sage": "book",
                    "soldier": "sword",
                }.get(ident, "scroll"),
                "description": description,
                "skills": {skill: 1 for skill in skills},
                "features": features,
            }
        )
    write("backgrounds.json", definitions)



CLASS_SKIP_FEATURES = {
    "Ability Score Improvement",
    "Epic Boon",
    "Fighting Style",
    "Proficiency Bonus",
}
CASTING_ABILITY = {
    "bard": "cha",
    "cleric": "wis",
    "druid": "wis",
    "paladin": "cha",
    "ranger": "wis",
    "sorcerer": "cha",
    "warlock": "cha",
    "wizard": "int",
}
SLOT_TABLE = {
    "bard": "full",
    "cleric": "full",
    "druid": "full",
    "sorcerer": "full",
    "wizard": "full",
    "paladin": "half",
    "ranger": "half",
    "warlock": "pact",
}


def class_feature_levels(record: dict[str, Any], default: int = 1) -> list[int]:
    levels = [int(entry["level"]) for entry in record.get("gained_at", []) if int(entry["level"]) > 0]
    if levels:
        return sorted(set(levels))
    match = re.search(r"level (\d+)", record.get("desc", ""), re.IGNORECASE)
    if match and int(match.group(1)) > 0:
        return [int(match.group(1))]
    return [default] if not record.get("gained_at") else []


def class_feature_id(class_id: str, record: dict[str, Any]) -> str:
    key = key_id(record["key"])
    prefix = f"{class_id}_"
    if key.startswith(prefix):
        key = key[len(prefix):]
    return f"class-{class_id}-{slug(key)}"


def feature_choice_picks(name: str) -> list[dict[str, Any]]:
    if name in ("Divine Order", "Primal Order", "Weapon Mastery", "Metamagic",
                "Eldritch Invocations", "Fighting Style", "Pact Magic"):
        return [{"id": "choice", "kind": "text", "label": name}]
    return []


def build_classes(spell_lists: dict[str, dict[int, list[str]]]) -> None:
    records = load("classes")
    base = {key_id(record["key"]): record for record in records if not record.get("subclass_of")}
    subclass_records = [record for record in records if record.get("subclass_of")]
    definitions: list[dict[str, Any]] = []
    progression_classes: dict[str, Any] = {}
    for class_id, record in base.items():
        level_features: dict[int, list[str]] = defaultdict(list)
        asi_levels: set[int] = set()
        epic_levels: set[int] = set()
        fighting_levels: set[int] = set()
        expertise_levels: set[int] = set()
        subclass_level = 3
        for entry in record.get("features", []):
            if entry.get("feature_type") != "CLASS_LEVEL_FEATURE":
                continue
            name = entry["name"]
            levels = class_feature_levels(entry)
            if not levels:
                continue
            if name == "Ability Score Improvement":
                asi_levels.update(levels)
                continue
            if name == "Epic Boon":
                epic_levels.update(levels)
                continue
            if name == "Fighting Style":
                fighting_levels.update(levels)
                continue
            if name == "Expertise":
                expertise_levels.update(levels)
                continue
            if "Subclass" in name:
                subclass_level = min(levels)
                continue
            if name in CLASS_SKIP_FEATURES:
                continue
            ident = class_feature_id(class_id, entry)
            rank = min(levels)
            details = "; ".join(
                f"Level {item['level']}: {item['detail']}"
                for item in entry.get("gained_at", [])
                if item.get("detail")
            )
            text = entry.get("desc", "").strip()
            if details:
                text = f"{text}\n\n{details}".strip()
            definitions.append(
                feature(
                    ident,
                    name,
                    text or name,
                    feature_type="trait",
                    tag=f"class:{class_id}",
                    rank=rank,
                    picks=feature_choice_picks(name),
                )
            )
            for level in levels:
                level_features[level].append(ident)
        if epic_levels:
            asi_levels.update(epic_levels)
        if class_id == "rogue":
            expertise_levels.discard(1)
        kits = CLASS_KITS[class_id]
        counters: dict[str, int] = {"hit_dice": 1}
        if CLASS_SPELLS[class_id][0]:
            counters["slot_1"] = 1 if class_id == "warlock" else 2
        if CLASS_CANTRIPS[class_id][0]:
            counters["cantrip_picks"] = CLASS_CANTRIPS[class_id][0]
        if CLASS_SPELLS[class_id][0]:
            counters["spell_picks"] = CLASS_SPELLS[class_id][0]
        if class_id == "rogue":
            counters["expertise_picks"] = 2
        template: dict[str, Any] = {
            "type": "template",
            "id": class_id,
            "kind": "class",
            "name": record["name"],
            "icon": CLASS_ICONS[class_id],
            "description": (
                f"{CLASS_ROLES[class_id]} Hit Point Die: d{record['hit_dice'][1:]}. "
                f"Saving Throw Proficiencies: "
                f"{', '.join(save['name'] for save in record['saving_throws'])}."
            ),
            "stats": {"hp": int(record["hit_dice"][1:]) - 1, "attack_bonus": 1},
            "skill_picks": [
                {
                    "id": "class_skills",
                    "count": CLASS_SKILL_COUNTS[class_id],
                    "table": f"skills_{class_id}",
                    "max_level": 2,
                }
            ],
            "features": level_features.get(1, []),
            "grant": {
                "items": kits["items"],
                "equip": kits["equip"],
                "silver": kits["gold"] * 10,
            },
            "names": "names",
        }
        if class_id == "fighter":
            template["feat_picks"] = [
                {
                    "id": "fighting_style",
                    "count": 1,
                    "tag": "fighting-style",
                    "feature_type": "feat",
                    "max_rank": 1,
                }
            ]
        if counters:
            template["counters"] = counters
        definitions.append(template)
        progression_classes[class_id] = {
            "hit_die": int(record["hit_dice"][1:]),
            "saves": [ABILITIES[save["name"].casefold()] for save in record["saving_throws"]],
            "casting": (
                "pact"
                if class_id == "warlock"
                else "full"
                if class_id in ("bard", "cleric", "druid", "sorcerer", "wizard")
                else "half"
                if class_id in ("paladin", "ranger")
                else "none"
            ),
            "ability": CASTING_ABILITY.get(class_id, ""),
            "subclass_level": subclass_level,
            "asi_levels": sorted(asi_levels),
            "fighting_style_levels": sorted(fighting_levels),
            "expertise_levels": sorted(expertise_levels),
            "cantrips": CLASS_CANTRIPS[class_id],
            "spells": CLASS_SPELLS[class_id],
            "slot_table": SLOT_TABLE.get(class_id, "none"),
            "features": {str(level): ids for level, ids in sorted(level_features.items())},
        }
    progression_subclasses: dict[str, Any] = {}
    for record in subclass_records:
        subclass_id = key_id(record["key"])
        parent = key_id(record["subclass_of"]["key"])
        levels: dict[int, list[str]] = defaultdict(list)
        for entry in record.get("features", []):
            if entry.get("feature_type") != "CLASS_LEVEL_FEATURE":
                continue
            name = entry["name"]
            if name == "Ability Score Improvement":
                continue
            feature_levels = class_feature_levels(entry, default=3)
            ident = f"subclass-{subclass_id}-{slug(key_id(entry['key']).split('_')[-1])}"
            definitions.append(
                feature(
                    ident,
                    name,
                    (entry.get("desc", "") or name).strip(),
                    feature_type="trait",
                    tag=f"subclass:{subclass_id}",
                    rank=min(feature_levels),
                    picks=feature_choice_picks(name),
                )
            )
            for level in feature_levels:
                levels[level].append(ident)
        marker = f"subclass-{subclass_id}"
        definitions.append(
            feature(
                marker,
                record["name"],
                (
                    f"The {record['name']} path for a {record['subclass_of']['name']}. "
                    "Its features are gained as the class advances."
                ),
                feature_type="trait",
                tag="subclass",
                rank=3,
            )
        )
        progression_subclasses[subclass_id] = {
            "class": parent,
            "features": {str(level): ids for level, ids in sorted(levels.items())},
        }
    write("classes.json", definitions)
    progression = {
        "type": "rule_data",
        "id": "progression",
        "python_module": "dnd_srd_2024",
        "configure": "configure_progression",
        "max_level": 20,
        "xp": {"standard": XP_STANDARD},
        "classes": progression_classes,
        "subclasses": progression_subclasses,
        "spell_lists": spell_lists,
        "slots": {"none": [[0] * 9 for _ in range(20)], "full": FULL_SLOTS,
                  "half": HALF_SLOTS},
        "pact_slots": PACT_SLOTS,
    }
    write("progression.json", [progression])
    write(
        "level_up.json",
        [
            {
                "type": "level_up",
                "id": "level",
                "label": "Level Up",
                "description": (
                    "Advance one class level. Hit points, the proficiency bonus, spell slots, "
                    "and the level's class features are automatic; the choices below spend the "
                    "picks the level grants."
                ),
                "level_counter": "level",
                "xp_counter": "xp",
                "rate_option": "xp_rate",
                "xp_source": "progression.xp",
                "template_kinds": ["class"],
                "python_module": "dnd_srd_2024",
                "apply_level": "apply_level_up",
                "summary": "level_up_summary",
                "picks": [
                    {
                        "id": "spells",
                        "label": "Spells",
                        "kind": "feature",
                        "counter": "spell_picks",
                        "feature_type": "spell",
                        "options": "spell_options",
                        "apply": "apply_spell_pick",
                    },
                    {
                        "id": "cantrips",
                        "label": "Cantrips",
                        "kind": "feature",
                        "counter": "cantrip_picks",
                        "feature_type": "spell",
                        "options": "spell_options",
                        "apply": "apply_spell_pick",
                    },
                    {
                        "id": "asi",
                        "label": "Feat or Ability Scores",
                        "kind": "feature",
                        "counter": "asi_picks",
                        "feature_type": "feat",
                        "tags": ["general", "epic-boon"],
                        "options": "feat_options",
                        "apply": "apply_feat_pick",
                    },
                    {
                        "id": "fighting_style",
                        "label": "Fighting Style",
                        "kind": "feature",
                        "counter": "fighting_style_picks",
                        "feature_type": "feat",
                        "tags": ["fighting-style"],
                        "options": "fighting_style_options",
                        "apply": "apply_fighting_style_pick",
                    },
                    {
                        "id": "subclass",
                        "label": "Subclass",
                        "kind": "feature",
                        "counter": "subclass_picks",
                        "feature_type": "trait",
                        "tags": ["subclass"],
                        "options": "subclass_options",
                        "apply": "apply_subclass_pick",
                    },
                    {
                        "id": "expertise",
                        "label": "Expertise",
                        "kind": "feature",
                        "counter": "expertise_picks",
                        "feature_type": "trait",
                        "tags": ["expertise"],
                        "options": "expertise_options",
                        "apply": "apply_expertise_pick",
                    },
                ],
            }
        ],
    )
    print(f"classes: {len(progression_classes)}, subclasses: {len(progression_subclasses)}")



def monster_attack_dice(attack: dict[str, Any]) -> str:
    count = attack.get("damage_die_count")
    die = attack.get("damage_die_type")
    bonus = int(attack.get("damage_bonus") or 0)
    if not die:
        return "1d1"
    dice = f"{int(count or 1)}d{int(die.lstrip('Dd'))}"
    return f"{dice}{'+' if bonus >= 0 else '-'}{abs(bonus)}" if bonus else dice


def monster_actions(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        action
        for action in record.get("actions", [])
        if action.get("action_type") in ("ACTION", "BONUS_ACTION", "REACTION", "LEGENDARY_ACTION")
    ]


def multiattack_count(record: dict[str, Any]) -> int:
    for action in record.get("actions", []):
        if action.get("name", "").casefold() == "multiattack":
            text = action.get("desc", "")
            match = re.search(
                r"\b(one|two|three|four|five|six)\b[^.]*?\battacks?\b", text, re.IGNORECASE
            )
            if match:
                words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
                return words[match.group(1).casefold()]
            match = re.search(r"\bmakes (\d+) attacks?\b", text, re.IGNORECASE)
            if match:
                return int(match.group(1))
    return 1


def monster_description(record: dict[str, Any]) -> str:
    size = (record.get("size") or {}).get("name", "Medium")
    kind = (record.get("type") or {}).get("name", "creature")
    alignment = record.get("alignment") or "unaligned"
    speed = record.get("speed_all") or {}
    speeds = ", ".join(
        f"{name} {value} ft."
        for name, value in speed.items()
        if name in ("walk", "fly", "swim", "climb", "burrow") and value
    )
    lines = [
        f"{size} {kind.lower()}, {alignment}.",
        f"Armor Class {record.get('armor_class') or 10}"
        + (f" ({record['armor_detail']})" if record.get("armor_detail") else "")
        + ".",
        f"Hit Points {record.get('hit_points') or 1} ({record.get('hit_dice') or '1d1'}).",
        f"Speed {speeds or '30 ft.'}.",
    ]
    if record.get("languages"):
        lines.append(f"Languages: {record['languages'].get('as_string', '')}.")
    if record.get("challenge_rating") is not None:
        lines.append(f"Challenge Rating {record['challenge_rating']}.")
    return " ".join(lines)


def monster_abilities(record: dict[str, Any]) -> list[dict[str, Any]]:
    abilities = []
    for trait in record.get("traits", []) or []:
        abilities.append(
            {
                "kind": "passive",
                "name": trait.get("name", "Trait"),
                "description": trait.get("desc", "") or trait.get("name", "Trait"),
            }
        )
    for action in monster_actions(record):
        if action.get("attacks"):
            continue
        kind = "action" if action.get("action_type") == "ACTION" else "action"
        text = action.get("desc", "") or action.get("name", "")
        if action.get("usage_limits"):
            text += f" ({action['usage_limits']})"
        abilities.append(
            {
                "kind": kind,
                "name": action.get("name", "Action"),
                "description": text,
            }
        )
    return abilities


def build_monsters() -> None:
    records = load("creatures")
    definitions = []
    for record in records:
        actions = monster_actions(record)
        attacks: dict[str, Any] = {}
        to_hits: list[int] = []
        for action in actions:
            data = action.get("attacks") or []
            if not data:
                continue
            first = data[0]
            name = action.get("name", "Attack")
            entry: dict[str, Any] = {"dice": monster_attack_dice(first)}
            reach = first.get("reach")
            if reach and int(reach) > 5:
                entry["reach"] = int(reach)
            range_value = first.get("range")
            long_value = first.get("long_range")
            if range_value:
                entry["ranges"] = {
                    "normal": int(range_value),
                    "long": int(long_value or range_value),
                }
            attacks[name] = entry if set(entry) != {"dice"} else entry["dice"]
            if first.get("to_hit_mod") is not None:
                to_hits.append(int(first["to_hit_mod"]))
        abilities = monster_abilities(record)
        stats = {
            "str": max(1, int(record["ability_scores"].get("strength") or 10)),
            "dex": max(1, int(record["ability_scores"].get("dexterity") or 10)),
            "con": max(1, int(record["ability_scores"].get("constitution") or 10)),
            "int": max(1, int(record["ability_scores"].get("intelligence") or 10)),
            "wis": max(1, int(record["ability_scores"].get("wisdom") or 10)),
            "cha": max(1, int(record["ability_scores"].get("charisma") or 10)),
            "hp": max(1, int(record.get("hit_points") or 1)),
            "attack_bonus": max(to_hits, default=0) if to_hits else 0,
        }
        definition: dict[str, Any] = {
            "type": "monster",
            "id": f"monster-{key_id(record['key'])}",
            "name": record["name"],
            "description": monster_description(record),
            "icon": CREATURE_ICONS.get((record.get("type") or {}).get("key", ""), "monster"),
            "stats": stats,
            "armor": max(0, int(record.get("armor_class") or 10)),
            "attacks": attacks,
            "attacks_per_action": multiattack_count(record),
            "abilities": abilities,
        }
        definitions.append(definition)
    write("monsters.json", definitions)


DND_RULES = [
    (
        "d20-tests",
        "The d20 Test. Whenever a creature tries something that could fail, roll a d20, add "
        "the relevant ability modifier, and add the proficiency bonus when the creature is "
        "proficient. Compare the total to a Difficulty Class (DC): the check succeeds when the "
        "total equals or exceeds the DC. A natural 20 is not an automatic success outside an "
        "attack roll or death save, and a natural 1 is not an automatic failure.",
        True,
    ),
    (
        "advantage",
        "Advantage and Disadvantage. When a rule gives advantage, roll two d20s and use the "
        "higher; when it gives disadvantage, roll two and use the lower. Advantage and "
        "disadvantage cancel each other, however many of each apply. Pass advantage or "
        "disadvantage to the roll as the roll_mode argument.",
        True,
    ),
    (
        "proficiency",
        "Proficiency Bonus. A character's proficiency bonus is +2 at levels 1-4, +3 at 5-8, +4 "
        "at 9-12, +5 at 13-16, and +6 at 17-20. It is added to attack rolls, ability checks, "
        "and saving throws the character is proficient in. Expertise doubles it for a chosen "
        "skill.",
        True,
    ),
    (
        "armor-class",
        "Armor Class. An attack hits when the attack roll total equals or exceeds the target's "
        "Armor Class. Unarmored AC is 10 + Dexterity modifier. Body armor sets a base AC and "
        "may cap the Dexterity added; a shield adds +2.",
        True,
    ),
    (
        "death-saves",
        "Death Saving Throws. A player character at 0 hit points is unconscious and makes a "
        "death save at the start of each of their turns: 10 or higher is a success, 9 or lower "
        "a failure, a natural 1 counts as two failures, and a natural 20 restores 1 hit point. "
        "Three successes stabilize the character; three failures kill them. Use the death_save "
        "tool, never a normal ability save.",
        True,
    ),
    (
        "spellcasting",
        "Spellcasting. A spellcaster knows cantrips and leveled spells. Casting a leveled spell "
        "spends a slot of its level or higher; a cantrip costs no slot. Use cast_spell with the "
        "slot level when upcasting. A spell that requires concentration ends an earlier "
        "concentration spell. Long rests restore all slots.",
        True,
    ),
    (
        "rests",
        "Rests. A short rest takes 1 hour; a character may spend Hit Point Dice to regain hit "
        "points. A long rest takes 8 hours and restores all hit points, Hit Point Dice, and "
        "spell slots. Use the rest tool with length short or long, then spend_hit_die for each "
        "die a character spends.",
        True,
    ),
]


def build_rules() -> None:
    definitions = []
    for rule in load("rules"):
        text = (rule.get("desc") or "").strip()
        if not text:
            continue
        definitions.append(
            {
                "type": "rule",
                "id": f"srd-{slug(key_id(rule['key']))}",
                "text": text,
                "important": False,
            }
        )
    for ident, text, important in DND_RULES:
        definitions.append(
            {"type": "rule", "id": ident, "text": text, "important": important}
        )
    write("rules.json", definitions)



def build_prompts() -> None:
    write(
        "prompts.json",
        [
            {
                "type": "prompt",
                "id": "d20_tests",
                "step": "state",
                "text": "Every uncertain action is a d20 test. Call roll_check for an ability check and player_save or roll_save for a saving throw, and let the engine roll. A check or save is d20 plus the ability modifier plus the proficiency bonus when the character is proficient. The DC is 5 for very easy, 10 easy, 15 medium, 20 hard, 25 very hard, and 30 nearly impossible; spells and monster abilities state their own DC. Never roll a d20 yourself and never add a modifier twice: the skill levels already hold the proficiency bonus.",
            },
            {
                "type": "prompt",
                "id": "advantage_disadvantage",
                "step": "state",
                "text": "When the rules give advantage or disadvantage, pass roll_mode advantage or disadvantage and say so in the prompt; the engine rolls twice and keeps the better or worse. Advantage and disadvantage cancel, however many of each apply. Give advantage for a helpful condition the rules name, such as attacking a prone or restrained target in melee, and disadvantage for a hindering one, such as attacking while prone or at long range. A player's reported roll is used exactly as reported, so tell them to roll twice when the roll has advantage.",
            },
            {
                "type": "prompt",
                "id": "saving_throws",
                "step": "state",
                "text": "A saving throw against an effect uses that effect's DC. For a player character call player_save with the ability and DC so the player rolls; for an NPC call roll_save, which rolls the d20 itself. A save is d20 plus the ability modifier and, when the class is proficient in that save, the proficiency bonus. A failure applies what the effect says; do not mark the character defeated unless its hit points reach 0.",
            },
            {
                "type": "prompt",
                "id": "combat_actions",
                "step": "state",
                "text": "D&D combat: an attack hits when d20 plus the attack bonus and ability modifier equals or exceeds the target's Armor Class. Use player_attack for a player character and attack for an NPC; the engine rolls the d20, adds the proficiency bonus and the weapon's ability modifier, and rolls the weapon's damage. A weapon's reach is 5 feet unless it has Reach (10 feet); a ranged weapon has a normal and a long band, and a shot beyond long range is refused. Pass roll_mode for advantage or disadvantage. On a hit, subtract the damage from hit points. At 0 hit points a player character falls unconscious and begins death saves; an NPC falls. A nonlethal strike with a fist, club, or staff leaves the target unconscious instead of dead. A character cannot take a second attack action unless a feature gives Extra Attack; the engine enforces the turn's attack budget.",
            },
            {
                "type": "prompt",
                "id": "spellcasting",
                "step": "state",
                "text": "Use cast_spell when a character casts a spell they know. A cantrip costs no slot; a leveled spell spends a slot of its level or higher. Pass slot_level when the player upcasts; the spell's higher-level text applies. The result gives the spell save DC and spell attack bonus, so roll NPC saves with roll_save or let the player roll with player_save, and use the spell's own damage and area from its description. A concentration spell ends any earlier concentration spell. A spellcaster learns new spells when the level-up page offers them, and a feature such as Magic Initiate may teach one with learn_spell. Never let a character cast a spell they have not learned.",
            },
            {
                "type": "prompt",
                "id": "rests",
                "step": "state",
                "text": "A short rest takes 1 hour: call rest with length short, then spend_hit_die once for each Hit Point Die a character spends. A long rest takes 8 hours and restores all hit points, Hit Point Dice, and spell slots; call rest with length long. The engine refills slots and Hit Point Dice for you. Do not add advance_time for the same period, and do not restore hit points by narration alone.",
            },
            {
                "type": "prompt",
                "id": "death_and_dying",
                "step": "state",
                "text": "A player character at 0 hit points is unconscious and dying. At the start of each of their turns call death_save so the player rolls: 10 or more succeeds, 9 or less fails, a natural 1 is two failures, and a natural 20 restores 1 hit point and ends the dying state. Three successes stabilize the character; three failures kill them. A stabilized character remains unconscious until healed above 0 hit points; then call recover_character. An NPC at 0 hit points dies unless it is important, in which case it is unconscious.",
            },
            {
                "type": "prompt",
                "id": "conditions_and_exhaustion",
                "step": "state",
                "text": "Record every lasting condition with add_condition and end it with end_condition when its duration or save ends it. The SRD conditions are blinded, charmed, deafened, exhaustion, frightened, grappled, incapacitated, invisible, paralyzed, petrified, poisoned, prone, restrained, stunned, and unconscious. Read the condition with lookup_condition before applying its effects; the engine applies any numeric modifiers it carries, and you apply the rest. Exhaustion is tracked as a condition with its level in the description; a long rest reduces it by one.",
            },
            {
                "type": "prompt",
                "id": "equipment_and_armor",
                "step": "state",
                "text": "Armor sets a base Armor Class and caps the Dexterity modifier it allows: light armor lets the full Dexterity through, medium armor caps it at 2, and heavy armor allows none. A shield adds 2. Equip armor and shields with equip_item so the engine records the change; never adjust Armor Class by narration alone. Buying and selling uses buy_item and the marketplace's silver prices.",
            },
            {
                "type": "prompt",
                "id": "level_up_choices",
                "step": "state",
                "text": "When a character has enough experience, the player takes the level on the advancement page. Hit points, the proficiency bonus, spell slots, and class features arrive automatically; the page then offers the level's spells, Ability Score Improvement or feat, fighting style, subclass, and expertise. Never apply a level or choose a player's feat, spell, or subclass for them; the player makes those choices.",
            },
            {
                "type": "prompt",
                "id": "npc_statblocks",
                "step": "state",
                "text": "An NPC built from a bestiary entry already has its attacks, armor, and hit points. When you invent an NPC, give them sensible ability scores, hit points, and at least one attack with create_npc or update_npc; an NPC with no attacks can only stand there. Use the monster's own attack names and dice from its statblock, and never invent a bonus the statblock does not give.",
            },
            {
                "type": "prompt",
                "id": "encounter_initiative",
                "text": "D&D initiative is rolled per character: each combatant rolls 1d20 plus its Dexterity modifier, and the highest total acts first. Add every combatant, then call roll_initiative once before the first turn. The engine keeps the rolled order.",
            },
            {
                "type": "prompt",
                "id": "player_rolls",
                "text": "The player rolls their own character's attacks, saves, and checks. Call player_attack for an attack, player_save for a save, and roll_check for an ability check, so the player presses the roll button; never roll their dice yourself. The player rolls only the d20; the engine adds the ability modifier, proficiency bonus, and any situational modifier. For an NPC, use attack and roll_save, which roll the dice themselves.",
            },
            {
                "type": "prompt",
                "id": "feature_text",
                "text": "Before you resolve a spell, class feature, species trait, or feat, read it with lookup_feature. The statblock names the character's features and prints their short notes; the full text is the rule. Never invent an effect a feature does not state, and never add a bonus the feature does not give.",
            },
        ],
    )


def build_tool_calls() -> None:
    tools = [
        {
            "type": "tool_call",
            "id": "roll_check",
            "bind": {"check": "ability"},
            "description": "Ask the player to roll an ability check: d20 plus the ability modifier and the proficiency bonus when the character is proficient. Call it whenever an action is uncertain and a skill or ability applies. Give a DC: 10 easy, 15 medium, 20 hard, 25 very hard. Pass roll_mode advantage or disadvantage when the rules give it.",
            "parameters": {
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the GM says to invite the roll. The player sees this text.",
                    },
                    "character": {
                        "type": "string",
                        "description": "The player character making the check.",
                    },
                    "skill": {
                        "type": "string",
                        "enum_from": "skills",
                        "description": "The skill used, such as perception, stealth, or persuasion.",
                    },
                    "stat": {
                        "type": "string",
                        "enum": ["str", "dex", "con", "int", "wis", "cha"],
                        "description": "The ability whose modifier is added.",
                    },
                    "difficulty": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "default": 15,
                        "description": "The DC: 10 easy, 15 medium, 20 hard.",
                    },
                    "modifier": {
                        "type": "integer",
                        "minimum": -10,
                        "maximum": 10,
                        "default": 0,
                        "description": "A situational bonus or penalty.",
                    },
                    "roll_mode": {
                        "type": "string",
                        "enum": ["normal", "advantage", "disadvantage"],
                        "default": "normal",
                        "description": "Advantage or disadvantage on the d20.",
                    },
                    "cause": {
                        "type": "string",
                        "description": "What the check is for, in a few words.",
                    },
                },
                "required": ["prompt", "character", "skill", "stat"],
            },
            "player_input": {
                "param": "roll",
                "dice": "1d20",
                "dice_values_param": "dice_values",
                "details": "The engine adds your ability modifier and proficiency bonus.",
            },
        },
        {
            "type": "tool_call",
            "id": "player_save",
            "python_module": "dnd_srd_2024",
            "function": "roll_save",
            "description": "Ask the player to roll an ability save against an effect's DC. Use it for a player character; an NPC saves with roll_save. The engine adds the ability modifier and the proficiency bonus when the class is proficient in that save.",
            "parameters": {
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the GM says to invite the save. The player sees this text.",
                    },
                    "name": {"type": "string", "description": "The player character saving."},
                    "stat": {
                        "type": "string",
                        "enum": ["str", "dex", "con", "int", "wis", "cha"],
                        "description": "The ability the save uses.",
                    },
                    "dc": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 40,
                        "description": "The saving throw DC.",
                    },
                    "cause": {"type": "string", "description": "What the save is for."},
                    "roll_mode": {
                        "type": "string",
                        "enum": ["normal", "advantage", "disadvantage"],
                        "default": "normal",
                        "description": "Advantage or disadvantage on the save.",
                    },
                },
                "required": ["prompt", "name", "stat", "dc", "cause"],
            },
            "player_input": {
                "param": "roll",
                "dice": "1d20",
                "details": "The engine adds your save bonus.",
            },
        },
        {
            "type": "tool_call",
            "id": "roll_save",
            "python_module": "dnd_srd_2024",
            "function": "roll_save",
            "description": "Roll an NPC's or monster's ability save against an effect's DC. The engine rolls the d20 and adds the ability modifier. Use player_save instead for a player character.",
            "parameters": {
                "properties": {
                    "name": {"type": "string", "description": "The NPC saving."},
                    "stat": {
                        "type": "string",
                        "enum": ["str", "dex", "con", "int", "wis", "cha"],
                        "description": "The ability the save uses.",
                    },
                    "dc": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 40,
                        "description": "The saving throw DC.",
                    },
                    "cause": {"type": "string", "description": "What the save is for."},
                    "roll_mode": {
                        "type": "string",
                        "enum": ["normal", "advantage", "disadvantage"],
                        "default": "normal",
                        "description": "Advantage or disadvantage on the save.",
                    },
                },
                "required": ["name", "stat", "dc", "cause"],
            },
        },
        {
            "type": "tool_call",
            "id": "death_save",
            "python_module": "dnd_srd_2024",
            "function": "death_save",
            "description": "Ask a dying player character to roll a death save at the start of their turn. Ten or more succeeds, nine or less fails, a natural 1 counts as two failures, and a natural 20 restores 1 hit point. Three successes stabilize; three failures kill.",
            "parameters": {
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the GM says to invite the death save.",
                    },
                    "name": {"type": "string", "description": "The dying player character."}
                },
                "required": ["prompt", "name"],
            },
            "player_input": {
                "param": "roll",
                "dice": "1d20",
                "details": "Roll a death save: 10 or more succeeds.",
            },
        },
        {
            "type": "tool_call",
            "id": "spend_hit_die",
            "python_module": "dnd_srd_2024",
            "function": "spend_hit_die",
            "description": "Spend one of a character's Hit Point Dice on a short rest to regain its roll plus the Constitution modifier. Call it once per die spent, after the rest.",
            "parameters": {
                "properties": {
                    "name": {"type": "string", "description": "The character spending a Hit Point Die."}
                },
                "required": ["name"],
            },
        },
        {
            "type": "tool_call",
            "id": "cast_spell",
            "python_module": "dnd_srd_2024",
            "function": "cast_spell",
            "description": "Cast a spell a character knows, spending a slot of its level or higher. Cantrips cost no slot. Pass slot_level when upcasting. The result gives the spell save DC, spell attack bonus, and the spell's text for its effect.",
            "parameters": {
                "properties": {
                    "name": {"type": "string", "description": "The caster."},
                    "spell": {"type": "string", "description": "The spell's name or id."},
                    "slot_level": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 9,
                        "default": 0,
                        "description": "The slot level spent; 0 uses the spell's own level.",
                    },
                },
                "required": ["name", "spell"],
            },
        },
        {
            "type": "tool_call",
            "id": "learn_spell",
            "python_module": "dnd_srd_2024",
            "function": "learn_spell",
            "description": "Teach a character a spell or cantrip, for a feature such as Magic Initiate that grants one. This records the spell without spending a spell pick.",
            "parameters": {
                "properties": {
                    "name": {"type": "string", "description": "The character learning the spell."},
                    "spell": {"type": "string", "description": "The spell's name or id."},
                },
                "required": ["name", "spell"],
            },
        },
        {
            "type": "tool_call",
            "id": "learn_feature",
            "python_module": "dnd_srd_2024",
            "function": "learn_feature",
            "description": "Teach a character a feature, feat, or trait by name, for a choice a species or background grants. Do not use it to bypass a level-up choice the player has not made.",
            "parameters": {
                "properties": {
                    "name": {"type": "string", "description": "The character learning the feature."},
                    "feature": {"type": "string", "description": "The feature's name or id."},
                    "cause": {"type": "string", "description": "Why the character learns it."},
                },
                "required": ["name", "feature"],
            },
        },
    ]
    write("tool_calls.json", tools)



def build_clock() -> None:
    write(
        "clock.json",
        [
            {
                "type": "clock",
                "id": "dnd-srd-2024",
                "parts": ["morning", "afternoon", "evening", "night"],
                "stale_days": 5,
                "hours_per_part": 6,
            }
        ],
    )


def build_world_terrain() -> None:
    source = ROOT / "src/cygnusloom/data/rulesets/cairn2e/world_terrain.json"
    write("world_terrain.json", json.loads(source.read_text(encoding="utf-8")))

if __name__ == "__main__":
    main()
