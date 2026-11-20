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
# What each ability score is used for, shown beside its name when a feature asks the player to
# choose one (a background's ability increases or a feat's casting ability).
ABILITY_DESCRIPTIONS = {
    "str": "Melee attack and damage rolls, carrying capacity, and Strength saves.",
    "dex": "Ranged attack rolls, Armor Class, initiative, and Dexterity saves.",
    "con": "Hit points, Constitution saves, and endurance.",
    "int": "Arcana, History, Investigation, and Nature checks, and Intelligence saves.",
    "wis": "Animal Handling, Insight, Medicine, Perception, and Survival, and Wisdom saves.",
    "cha": "Deception, Intimidation, Performance, and Persuasion, and Charisma saves.",
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
    "athletics": "muscle",
    "deception": "speech",
    "history": "scroll",
    "insight": "eye",
    "intimidation": "fist",
    "investigation": "magnifier",
    "medicine": "potion",
    "nature": "plant",
    "perception": "ear",
    "performance": "lute",
    "persuasion": "handshake",
    "religion": "altar",
    "sleight_of_hand": "ring",
    "stealth": "footprints",
    "survival": "campfire",
}
WEAPON_ICONS = {
    "battleaxe": "axe",
    "blowgun": "blowgun",
    "club": "club",
    "dagger": "dagger",
    "dart": "dart",
    "flail": "flail",
    "glaive": "halberd",
    "greataxe": "axe",
    "greatclub": "club",
    "greatsword": "greatsword",
    "halberd": "halberd",
    "hand-crossbow": "crossbow",
    "handaxe": "axe",
    "heavy-crossbow": "crossbow",
    "javelin": "javelin",
    "lance": "lance",
    "light-crossbow": "crossbow",
    "light-hammer": "warhammer",
    "longbow": "bow",
    "longsword": "sword",
    "mace": "mace",
    "maul": "warhammer",
    "morningstar": "mace",
    "musket": "crossbow",
    "net": "net",
    "pike": "pike",
    "pistol": "crossbow",
    "quarterstaff": "staff",
    "rapier": "rapier",
    "scimitar": "scimitar",
    "shortbow": "bow",
    "shortsword": "sword",
    "sickle": "dagger",
    "sling": "sling",
    "spear": "spear",
    "trident": "trident",
    "war-pick": "warhammer",
    "warhammer": "warhammer",
    "whip": "whip",
}
ARMOR_ICONS = {
    "breastplate": "plate-armor",
    "chain-mail": "chain-mail",
    "chain-shirt": "chain-mail",
    "half-plate-armor": "plate-armor",
    "hide-armor": "leather-armor",
    "leather-armor": "leather-armor",
    "padded-armor": "leather-armor",
    "plate-armor": "plate-armor",
    "ring-mail": "chain-mail",
    "scale-mail": "chain-mail",
    "shield": "shield",
    "splint-armor": "chain-mail",
    "studded-leather-armor": "leather-armor",
}
ITEM_ICONS = {
    "acid": "potion",
    "alchemists-fire": "potion",
    "alchemists-supplies": "alchemists-supplies",
    "barrel": "barrel",
    "basket": "pouch",
    "bell": "bell",
    "brewers-supplies": "alchemists-supplies",
    "calligraphers-supplies": "scroll",
    "cartographers-tools": "navigators-tools",
    "climbers-kit": "rope",
    "bedroll": "bedroll",
    "blanket": "bedroll",
    "block-and-tackle": "grappling-hook",
    "bottle-glass": "potion",
    "bucket": "pouch",
    "candle": "torch-item",
    "case-crossbow-bolt": "ammunition",
    "chest": "chest",
    "clothes-fine": "cloak",
    "clothes-travelers": "cloak",
    "costume": "disguise-kit",
    "flask": "potion",
    "hunting-trap": "caltrops",
    "healers-kit": "herbalism-kit",
    "ink": "scroll",
    "ink-pen": "scroll",
    "jug": "waterskin",
    "ladder": "ladder",
    "lamp": "lantern-item",
    "lock": "key",
    "magnifying-glass": "magnifier",
    "net": "net",
    "painters-supplies": "alchemists-supplies",
    "perfume": "potion",
    "pole": "staff",
    "pot-iron": "pot",
    "ram-portable": "tool",
    "rope": "rope",
    "saddle-exotic": "horse",
    "saddle-military": "horse",
    "saddle-riding": "horse",
    "shovel": "shovel",
    "signal-whistle": "musical-instrument",
    "spikes-iron": "caltrops",
    "string": "rope",
    "tent": "tent",
    "tinderbox": "torch-item",
    "vial": "potion",
    "airship": "boat",
    "carriage": "cart",
    "chariot": "cart",
    "sled": "cart",
    "galley": "boat",
    "keelboat": "boat",
    "longship": "boat",
    "rowboat": "boat",
    "sailing-ship": "boat",
    "warship": "boat",
    "arrows-20": "ammunition",
    "backpack": "backpack",
    "ball-bearings": "caltrops",
    "bolts-20": "ammunition",
    "bullets-firearm-10": "ammunition",
    "bullets-sling-20": "ammunition",
    "caltrops": "caltrops",
    "carpenters-tools": "carpenters-tools",
    "chain": "chain",
    "component-pouch": "component-pouch",
    "cooks-utensils": "cooks-utensils",
    "crowbar": "crowbar",
    "disguise-kit": "disguise-kit",
    "forgery-kit": "forgery-kit",
    "gaming-set-dice": "gaming-set",
    "gaming-set-dragonchess": "gaming-set",
    "gaming-set-playing-cards": "gaming-set",
    "gaming-set-three-dragon-ante": "gaming-set",
    "grappling-hook": "grappling-hook",
    "herbalism-kit": "herbalism-kit",
    "holy-symbol-amulet": "amulet",
    "holy-symbol-emblem": "amulet",
    "holy-symbol-reliquary": "amulet",
    "holy-water": "potion",
    "lantern-bullseye": "lantern-item",
    "lantern-hooded": "lantern-item",
    "manacles": "manacles",
    "mirror": "mirror",
    "musical-instrument-bagpipes": "musical-instrument",
    "musical-instrument-drum": "musical-instrument",
    "musical-instrument-dulcimer": "musical-instrument",
    "musical-instrument-flute": "musical-instrument",
    "musical-instrument-horn": "musical-instrument",
    "musical-instrument-lute": "musical-instrument",
    "musical-instrument-lyre": "musical-instrument",
    "musical-instrument-pan-flute": "musical-instrument",
    "musical-instrument-shawm": "musical-instrument",
    "musical-instrument-viol": "musical-instrument",
    "navigators-tools": "navigators-tools",
    "needles-50": "ammunition",
    "oil": "oil-flask",
    "poison-basic": "poisoners-kit",
    "poisoners-kit": "poisoners-kit",
    "potion-of-giant-strength": "potion",
    "potion-of-healing": "potion",
    "potions-of-healing": "potion",
    "pouch": "pouch",
    "quiver": "quiver",
    "rations": "rations",
    "feed-per-day": "rations",
    "robe": "cloak",
    "sack": "pouch",
    "smiths-tools": "smiths-tools",
    "spell-scroll": "scroll",
    "spyglass": "spyglass",
    "thieves-tools": "thieves-tools",
    "tinkers-tools": "tinkers-tools",
    "torch": "torch-item",
    "woodcarvers-tools": "carpenters-tools",
    "waterskin": "waterskin",
    "case-map-or-scroll": "scroll",
    "map": "scroll",
    "paper": "scroll",
    "parchment": "scroll",
    "book": "book",
}
MAGIC_CATEGORY_ICONS = {
    "ammunition": "ammunition",
    "armor": "armor",
    "potion": "potion",
    "ring": "ring",
    "rod": "rod",
    "scroll": "scroll",
    "staff": "staff",
    "wand": "wand",
    "weapon": "weapon",
    "wondrous-item": "wondrous-item",
}
MAGIC_NAME_ICONS = (
    ("bracers", "bracers"),
    ("belt", "belt"),
    ("gloves", "gloves"),
    ("gauntlets", "gloves"),
    ("cloak", "cloak"),
    ("boots", "boots"),
    ("amulet", "amulet"),
    ("necklace", "amulet"),
    ("bag", "pouch"),
    ("pouch", "pouch"),
    ("mirror", "mirror"),
    ("crystal", "crystal"),
    ("orb", "crystal"),
    ("instrument", "musical-instrument"),
    ("horn", "musical-instrument"),
    ("tome", "book"),
    ("deck", "gaming-set"),
    ("dice", "gaming-set"),
    ("rope", "rope"),
    ("chain", "chain"),
    ("lantern", "lantern-item"),
    ("torch", "torch-item"),
    ("candle", "torch-item"),
    ("bottle", "potion"),
    ("flask", "potion"),
    ("vial", "potion"),
    ("helm", "helm"),
    ("shield", "shield"),
)
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
        "persuasion",
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
    "warlock": [
        "arcana",
        "deception",
        "history",
        "intimidation",
        "investigation",
        "nature",
        "religion",
    ],
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
        "items": [
            "Greatsword",
            "Flail",
            "Javelin",
            "Javelin",
            "Javelin",
            "Javelin",
            "Javelin",
            "Javelin",
            "Javelin",
            "Javelin",
            "Dungeoneer's Pack",
        ],
        "equip": ["Chain Mail"],
        "gold": 4,
    },
    "monk": {
        "items": ["Spear", "Dagger", "Dagger", "Dagger", "Dagger", "Dagger", "Explorer's Pack"],
        "equip": [],
        "gold": 11,
    },
    "paladin": {
        "items": [
            "Longsword",
            "Javelin",
            "Javelin",
            "Javelin",
            "Javelin",
            "Javelin",
            "Javelin",
            "Holy Symbol",
            "Priest's Pack",
        ],
        "equip": ["Chain Mail", "Shield"],
        "gold": 9,
    },
    "ranger": {
        "items": ["Scimitar", "Shortsword", "Longbow", "Arrows (20)", "Quiver", "Explorer's Pack"],
        "equip": ["Studded Leather Armor"],
        "gold": 7,
    },
    "rogue": {
        "items": [
            "Dagger",
            "Dagger",
            "Shortsword",
            "Shortbow",
            "Arrows (20)",
            "Quiver",
            "Thieves' Tools",
            "Burglar's Pack",
        ],
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

CLASS_KIT_OPTIONS = {
    "barbarian": [{"id": "b", "label": "B: 75 GP", "silver": 750}],
    "bard": [{"id": "b", "label": "B: 90 GP", "silver": 900}],
    "cleric": [{"id": "b", "label": "B: 110 GP", "silver": 1100}],
    "druid": [{"id": "b", "label": "B: 50 GP", "silver": 500}],
    "fighter": [
        {
            "id": "b",
            "label": "B: light kit",
            "items": [
                "Scimitar",
                "Shortsword",
                "Longbow",
                "Arrows (20)",
                "Quiver",
                "Dungeoneer's Pack",
            ],
            "equip": ["Studded Leather Armor"],
            "silver": 110,
        },
        {"id": "c", "label": "C: 155 GP", "silver": 1550},
    ],
    "monk": [{"id": "b", "label": "B: 50 GP", "silver": 500}],
    "paladin": [{"id": "b", "label": "B: 150 GP", "silver": 1500}],
    "ranger": [{"id": "b", "label": "B: 150 GP", "silver": 1500}],
    "rogue": [{"id": "b", "label": "B: 100 GP", "silver": 1000}],
    "sorcerer": [{"id": "b", "label": "B: 50 GP", "silver": 500}],
    "warlock": [{"id": "b", "label": "B: 100 GP", "silver": 1000}],
    "wizard": [{"id": "b", "label": "B: 55 GP", "silver": 550}],
}

CLASS_ARMOR_TRAINING = {
    "barbarian": ["light", "medium", "shield"],
    "bard": ["light"],
    "cleric": ["light", "medium", "shield"],
    "druid": ["light", "medium", "shield"],
    "fighter": ["light", "medium", "heavy", "shield"],
    "monk": [],
    "paladin": ["light", "medium", "heavy", "shield"],
    "ranger": ["light", "medium", "shield"],
    "rogue": ["light"],
    "sorcerer": [],
    "warlock": ["light"],
    "wizard": [],
}

CLASS_WEAPON_TRAINING = {
    "barbarian": ["simple", "martial"],
    "bard": ["simple"],
    "cleric": ["simple"],
    "druid": ["simple"],
    "fighter": ["simple", "martial"],
    "monk": ["simple", "martial:light"],
    "paladin": ["simple", "martial"],
    "ranger": ["simple", "martial"],
    "rogue": ["simple", "martial:finesse", "martial:light"],
    "sorcerer": ["simple"],
    "warlock": ["simple"],
    "wizard": ["simple"],
}

CLASS_TOOLS = {
    "bard": ["Musical Instrument"],
    "monk": ["Artisan's Tools or Musical Instrument"],
    "rogue": ["Thieves' Tools"],
}

# The ability scores a class's Primary Ability line names. "Fighter" accepts either Strength
# or Dexterity; every other multi-ability class requires both scores at 13 to multiclass.
CLASS_PRIMARY = {
    "barbarian": ["str"],
    "bard": ["cha"],
    "cleric": ["wis"],
    "druid": ["wis"],
    "fighter": ["str", "dex"],
    "monk": ["dex", "wis"],
    "paladin": ["str", "cha"],
    "ranger": ["dex", "wis"],
    "rogue": ["dex"],
    "sorcerer": ["cha"],
    "warlock": ["cha"],
    "wizard": ["int"],
}
CLASS_PRIMARY_ANY = {"fighter"}

# What a class grants when it is gained after the character's first class: the SRD's
# "As a Multiclass Character" traits.
MULTICLASS_ARMOR = {
    "barbarian": ["shield"],
    "bard": ["light"],
    "cleric": ["light", "medium", "shield"],
    "druid": ["light", "shield"],
    "fighter": ["light", "medium", "shield"],
    "monk": [],
    "paladin": ["light", "medium", "shield"],
    "ranger": ["light", "medium", "shield"],
    "rogue": ["light"],
    "sorcerer": [],
    "warlock": ["light"],
    "wizard": [],
}
MULTICLASS_WEAPON_TRAINING = {
    "barbarian": ["martial"],
    "fighter": ["martial"],
    "paladin": ["martial"],
    "ranger": ["martial"],
}
MULTICLASS_SKILLS = {"bard": 1, "ranger": 1, "rogue": 1}
MULTICLASS_TOOLS = {"bard": ["Musical Instrument"], "rogue": ["Thieves' Tools"]}

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
    "bard": [4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 16, 16, 17, 17, 18, 18, 19, 20, 21, 22],
    "cleric": [4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 16, 16, 17, 17, 18, 18, 19, 20, 21, 22],
    "druid": [4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 16, 16, 17, 17, 18, 18, 19, 20, 21, 22],
    "fighter": [0] * 20,
    "monk": [0] * 20,
    "paladin": [2, 3, 4, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13],
    "ranger": [2, 3, 4, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13],
    "rogue": [0] * 20,
    "sorcerer": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
    "warlock": [2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 11, 11, 12, 12, 13, 13, 14, 14, 15, 15],
    "wizard": [4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 16, 16, 17, 18, 19, 21, 22, 23, 24, 25],
}

# Open5e's class records use the 2014 save pair for two classes. The 2024 SRD is explicit.
CLASS_SAVES = {
    "fighter": ("str", "con"),
    "monk": ("dex", "str"),
}

# A few feature effects are fixed by the 2024 class text and cannot be read from its prose.
FEATURE_DEFENSE = {
    ("barbarian", "Unarmored Defense"): {
        "base": 10,
        "modifiers": ["con"],
        "requires_no_armor": True,
    },
    ("monk", "Unarmored Defense"): {
        "base": 10,
        "modifiers": ["wis"],
        "requires_no_armor": True,
        "requires_no_shield": True,
    },
}

XP_STANDARD = [
    0,
    300,
    900,
    2700,
    6500,
    14000,
    23000,
    34000,
    48000,
    64000,
    85000,
    100000,
    120000,
    140000,
    165000,
    195000,
    225000,
    265000,
    305000,
    355000,
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


def write_files(files: dict[str, list[dict[str, Any]]]) -> None:
    """Write one file per template, so adding a class, subclass, species, or background is a
    single new file. The loader reads every file under the rule set, so these names are only a
    convenience for authors."""
    for relative, definitions in sorted(files.items()):
        path = OUT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(definitions, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {relative} ({len(definitions)} definitions)")


def gp_to_sp(value: str | float | None) -> int:
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
                "name": "SRD 5.2.1 (5E compatible)",
                "blurb": (
                    "A 2024 fifth-edition-compatible fantasy d20 ruleset: d20 checks, "
                    "twelve classes, nine species, spells, and a bestiary of monsters."
                ),
                "third_party": True,
                "description": (
                    "Character creation and play for a 2024 fifth-edition-compatible fantasy "
                    "d20 ruleset, built from the System Reference Document 5.2.1. Abilities "
                    "are assigned from the standard array, species and background shape the "
                    "character, and one of twelve classes carries them from level 1 to 20. "
                    "Checks and saves are d20 plus an ability modifier and the proficiency "
                    "bonus; spellcasters track slots level by level. The full SRD spell list, "
                    "bestiary, and magic item catalogue are included. Text adapted from the "
                    "System Reference Document 5.2.1, licensed under CC BY 4.0. See content "
                    "credits for attribution."
                ),
                "capabilities": {
                    "magic": "prepared",
                    "travel": "none",
                    "wilderness": False,
                    "dungeon": False,
                    "combat": True,
                    "social": ["conditions", "counters", "checks"],
                },
                "plus": "sum",
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
                        "formula": "4d6",
                        "keep": 3,
                        "swap_group": "abilities",
                        "swap": "any",
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
            "description": ABILITY_DESCRIPTIONS[ident],
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
            "plus_modifier_per_level": True,
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
    for ident, name in ABILITY_NAMES.items():
        stats.append(
            {
                "type": "stat",
                "id": f"save_{ident}",
                "name": f"{name} Save",
                "formula": "1d1",
                "minimum": 0,
                "hidden": True,
                "sheet_hidden": True,
            }
        )
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
    effects = {
        "blinded": {"roll_modes": {"attack": "disadvantage", "attack_against": "advantage"}},
        "frightened": {"roll_modes": {"attack": "disadvantage", "check": "disadvantage"}},
        "invisible": {"roll_modes": {"attack": "advantage", "attack_against": "disadvantage"}},
        "paralyzed": {"roll_modes": {"attack_against": "advantage"}},
        "petrified": {"roll_modes": {"attack_against": "advantage"}},
        "poisoned": {"roll_modes": {"attack": "disadvantage", "check": "disadvantage"}},
        "prone": {"roll_modes": {"attack": "disadvantage", "attack_against": "advantage"}},
        "restrained": {"roll_modes": {"attack": "disadvantage", "attack_against": "advantage"}},
        "stunned": {"roll_modes": {"attack_against": "advantage"}},
        "unconscious": {"roll_modes": {"attack_against": "advantage"}},
        "exhaustion": {"modifies": {"attack": -2, "check": -2, "save": -2}},
    }
    out = []
    for condition in conditions:
        descriptions = condition.get("descriptions") or []
        text = descriptions[0]["desc"] if descriptions else condition["name"]
        ends = ["save", "time", "magic"] if condition["key"] != "exhaustion" else ["long_rest"]
        entry = {
            "type": "condition",
            "id": condition["key"],
            "label": condition["name"],
            "description": text,
            "ends": ends,
        }
        entry.update(effects.get(condition["key"], {}))
        out.append(entry)
    out.extend(
        [
            {
                "type": "condition",
                "id": "sapped",
                "label": "Sapped",
                "description": "Disadvantage on the next attack roll before the start of the attacker's next turn.",
                "ends": ["rest"],
                "roll_modes": {"attack": "disadvantage"},
            },
            {
                "type": "condition",
                "id": "slowed",
                "label": "Slowed",
                "description": "Speed reduced by 10 feet until the start of the attacker's next turn.",
                "ends": ["rest"],
            },
        ]
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
        {
            "type": "counter",
            "id": "multiclass_skill_picks",
            "label": "Multiclass Skill Picks",
            "min": 0,
            "initial": 0,
            "description": "New skill proficiencies granted by a second class.",
        },
        {
            "type": "counter",
            "id": "pact_slots",
            "label": "Pact Magic Slots",
            "min": 0,
            "max": 4,
            "initial": 0,
            "description": "Pact Magic spell slots left; they return on a short or long rest.",
        },
        {
            "type": "counter",
            "id": "metamagic_picks",
            "label": "Metamagic Picks",
            "min": 0,
            "initial": 0,
            "description": "Metamagic options the character may learn.",
        },
        {
            "type": "counter",
            "id": "invocation_picks",
            "label": "Eldritch Invocation Picks",
            "min": 0,
            "initial": 0,
            "description": "Eldritch Invocations the character may learn.",
        },
        {
            "type": "counter",
            "id": "giant_ancestry",
            "label": "Giant Ancestry",
            "min": 0,
            "max": 6,
            "initial": 0,
            "description": "Goliath Giant Ancestry uses left; all return on a long rest.",
        },
        {
            "type": "counter",
            "id": "rage",
            "label": "Rages",
            "min": 0,
            "max": 6,
            "initial": 0,
            "description": "Rages left; one returns on a short rest and all on a long rest.",
        },
        {
            "type": "counter",
            "id": "bardic_inspiration",
            "label": "Bardic Inspiration",
            "min": 0,
            "max": 20,
            "initial": 0,
            "description": "Bardic Inspiration uses left; all return on a long rest.",
        },
        {
            "type": "counter",
            "id": "channel_divinity",
            "label": "Channel Divinity",
            "min": 0,
            "max": 4,
            "initial": 0,
            "description": "Channel Divinity uses left; one returns on a short rest and all on a long rest.",
        },
        {
            "type": "counter",
            "id": "wild_shape",
            "label": "Wild Shape",
            "min": 0,
            "max": 4,
            "initial": 0,
            "description": "Wild Shape uses left; one returns on a short rest and all on a long rest.",
        },
        {
            "type": "counter",
            "id": "second_wind",
            "label": "Second Wind",
            "min": 0,
            "max": 4,
            "initial": 0,
            "description": "Second Wind uses left; one returns on a short rest and all on a long rest.",
        },
        {
            "type": "counter",
            "id": "action_surge",
            "label": "Action Surge",
            "min": 0,
            "max": 2,
            "initial": 0,
            "description": "Action Surge uses left; all return on a short or long rest.",
        },
        {
            "type": "counter",
            "id": "indomitable",
            "label": "Indomitable",
            "min": 0,
            "max": 3,
            "initial": 0,
            "description": "Indomitable uses left; all return on a long rest.",
        },
        {
            "type": "counter",
            "id": "focus_points",
            "label": "Focus Points",
            "min": 0,
            "max": 20,
            "initial": 0,
            "description": "Focus Points left; all return on a short or long rest.",
        },
        {
            "type": "counter",
            "id": "lay_on_hands",
            "label": "Lay On Hands",
            "min": 0,
            "max": 100,
            "initial": 0,
            "description": "Healing pool left; it refills on a long rest.",
        },
        {
            "type": "counter",
            "id": "sorcery_points",
            "label": "Sorcery Points",
            "min": 0,
            "max": 22,
            "initial": 0,
            "description": "Sorcery Points left; all return on a long rest.",
        },
        {
            "type": "counter",
            "id": "arcane_recovery",
            "label": "Arcane Recovery",
            "min": 0,
            "max": 1,
            "initial": 0,
            "description": "Arcane Recovery uses left; it returns on a long rest.",
        },
        {
            "type": "counter",
            "id": "favored_enemy",
            "label": "Favored Enemy",
            "min": 0,
            "max": 6,
            "initial": 0,
            "description": "Free Hunter's Mark uses left; all return on a long rest.",
        },
        {
            "type": "counter",
            "id": "tireless",
            "label": "Tireless",
            "min": 0,
            "max": 1,
            "initial": 0,
            "description": "Tireless uses left; it returns on a long rest.",
        },
        {
            "type": "counter",
            "id": "mystic_arcanum",
            "label": "Mystic Arcanum",
            "min": 0,
            "max": 4,
            "initial": 0,
            "description": "Mystic Arcanum uses left; all return on a long rest.",
        },
        {
            "type": "counter",
            "id": "dark_ones_luck",
            "label": "Dark One's Luck",
            "min": 0,
            "max": 20,
            "initial": 0,
            "description": "Dark One's Luck uses left; all return on a long rest.",
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
                "weapon_proficiency_stat": "attack_bonus",
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
                "actions_per_turn": 1,
                "bonus_actions_per_turn": 1,
                "reactions_per_round": 1,
                "critical_dice": True,
                "heal_clears_defeat": True,
                "rest_requires_positive_hp": True,
                "stabilize_without_clock": True,
                "unarmed": {
                    "damage": "1d1",
                    "names": ["unarmed", "fist", "fists", "punch", "kick"],
                    "attributes": ["str"],
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
            "id": "spell_lists",
            "options": [
                {
                    "name": "cleric",
                    "description": (
                        "Cleric spells. Divine magic of healing, protection, and judgment; the "
                        "spellcasting ability is Wisdom."
                    ),
                },
                {
                    "name": "druid",
                    "description": (
                        "Druid spells. Nature magic of plants, beasts, and weather; the "
                        "spellcasting ability is Wisdom."
                    ),
                },
                {
                    "name": "wizard",
                    "description": (
                        "Wizard spells. Arcane magic recorded in spellbooks; the spellcasting "
                        "ability is Intelligence."
                    ),
                },
            ],
        },
        {
            "type": "table",
            "id": "human_size",
            "options": [
                {
                    "name": "Medium",
                    "description": "About 4\u20137 feet tall. You occupy a 5-foot square.",
                },
                {
                    "name": "Small",
                    "description": "About 2\u20134 feet tall. You occupy a 5-foot square.",
                },
            ],
        },
        {
            "type": "table",
            "id": "names",
            "options": [
                "Aelric",
                "Bryn",
                "Calla",
                "Daric",
                "Elowen",
                "Fenn",
                "Galen",
                "Hesta",
                "Ilyana",
                "Jorunn",
                "Kael",
                "Lira",
                "Marek",
                "Nessa",
                "Orin",
                "Perrin",
                "Quilla",
                "Rhea",
                "Sable",
                "Tamsin",
                "Ulric",
                "Vessa",
                "Wren",
                "Yara",
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
    for class_id, options in CLASS_SKILLS.items():
        tables.append(
            {
                "type": "table",
                "id": f"skills_{class_id}",
                "options": options,
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


def weapon_versatile_damage(record: dict[str, Any]) -> str:
    """The two-handed damage a Versatile weapon prints in its property detail."""
    for entry in record.get("properties", []):
        if entry["property"]["name"].casefold() != "versatile":
            continue
        detail = str(entry.get("detail") or "")
        match = re.search(r"(\d*d\d+)", detail)
        if match:
            return match.group(1)
    return ""


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
            "armor_category": "shield",
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
        "armor_category": str(record.get("category") or ""),
        "description": text,
    }


def item_size_for(record: dict[str, Any]) -> str:
    category = (record.get("category") or {}).get("key", "")
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


def weapon_mastery(record: dict[str, Any]) -> str:
    """The weapon's mastery property from its source properties, such as "sap"."""
    for entry in record.get("properties", []):
        prop = entry.get("property") or {}
        if prop.get("type") == "Mastery":
            return str(prop.get("name", "")).strip().casefold()
    return ""


def _record_key(record: dict[str, Any]) -> str:
    return str(record.get("key") or "").removeprefix("srd-2024_")


def weapon_icon(record: dict[str, Any]) -> str:
    return WEAPON_ICONS.get(_record_key(record), "weapon")


def armor_icon(record: dict[str, Any]) -> str:
    return ARMOR_ICONS.get(_record_key(record), "armor")


def item_icon(record: dict[str, Any]) -> str:
    key = _record_key(record)
    if key in ITEM_ICONS:
        return ITEM_ICONS[key]
    category = (record.get("category") or {}).get("key", "")
    return {
        "adventuring-gear": "generic-item",
        "ammunition": "ammunition",
        "equipment-pack": "backpack",
        "land-vehicle": "cart",
        "mount": "horse",
        "potion": "potion",
        "scroll": "scroll",
        "spellcasting-focus": "component-pouch",
        "tools": "tool",
        "waterborne-vehicle": "boat",
        "wondrous-item": "wondrous-item",
    }.get(category, "generic-item")


def magic_icon(record: dict[str, Any]) -> str:
    if record.get("weapon"):
        return weapon_icon(record["weapon"])
    if record.get("armor"):
        return armor_icon(record["armor"])
    category = (record.get("category") or {}).get("key", "")
    name = record.get("name", "").casefold()
    if category == "weapon" and "ammunition" in name:
        return "ammunition"
    for keyword, icon in MAGIC_NAME_ICONS:
        if keyword in name:
            return icon
    return MAGIC_CATEGORY_ICONS.get(category, "wondrous-item")


def item_definition(
    ident: str,
    name: str,
    description: str,
    *,
    category: str = "",
    price: int | None = None,
    tag: str = "",
    **extra: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": "item",
        "id": ident,
        "name": name,
        "description": description,
    }
    if category:
        data["category"] = category
    if price is not None:
        data["price"] = price
    if tag:
        data["tag"] = tag
    data.update(extra)
    return data


def _item_group(item: dict[str, Any]) -> str:
    if item.get("currency"):
        return "currency"
    if item.get("damage"):
        return "weapons"
    if item.get("armor") or item.get("armor_category"):
        return "armor"
    return "gear"


def build_equipment() -> None:
    weapons = load("weapons")
    armor = load("armor")
    items = load("items")
    magic = load("magicitems")
    item_costs = {item["key"]: item.get("cost") for item in items}
    equipment: list[dict[str, Any]] = [
        item_definition(
            "silver",
            "Silver Pieces",
            "Silver pieces, the standard coin of the SRD economy: ten silver to a gold piece. "
            "Prices in the catalogue are given in silver.",
            category="coin",
            price=1,
            size="tiny",
            currency=True,
            icon="coin",
        ),
        # Generic kit items the API leaves out.
        item_definition(
            "musical-instrument",
            "Musical Instrument",
            "A lute, flute, drum, or another instrument of the player's choosing.",
            category="tool",
            price=300,
            size="normal",
            icon="musical-instrument",
        ),
        item_definition(
            "arcane-focus",
            "Arcane Focus",
            "A crystal, orb, rod, staff, or wand that channels arcane magic.",
            category="focus",
            price=100,
            size="small",
            icon="crystal",
        ),
        item_definition(
            "holy-symbol",
            "Holy Symbol",
            "A divine emblem worn or held to channel a deity's power.",
            category="focus",
            price=50,
            size="tiny",
            icon="amulet",
        ),
        item_definition(
            "spellbook",
            "Spellbook",
            "A leather-bound book of spells, worth 50 gp to a wizard.",
            category="tool",
            price=500,
            size="normal",
            icon="book",
        ),
    ]
    for record in weapons:
        ident = f"weapon-{key_id(record['key'])}"
        props = {entry["property"]["name"].casefold() for entry in record.get("properties", [])}
        extra: dict[str, Any] = {
            "damage": weapon_damage(record),
            "damage_type": (record.get("damage_type") or {}).get("name", "").casefold(),
            "attributes": weapon_attributes(record),
            "size": weapon_size(record),
            "weapon_category": "simple" if record.get("is_simple") else "martial",
            "weapon_properties": sorted(props),
            # Groups a feature pick can offer, such as a Barbarian's melee-only Weapon Mastery
            # or a Rogue's simple, finesse, and light weapons.
            "groups": sorted(
                {
                    "simple" if record.get("is_simple") else "martial",
                    "ranged" if "ammunition" in props else "melee",
                    *props,
                }
            ),
            "icon": weapon_icon(record),
            "armor_mode": "add",
        }
        mastery = weapon_mastery(record)
        if mastery:
            extra["mastery"] = mastery
        flags = sorted(
            prop for prop in props if prop in {"heavy", "light", "loading", "two-handed"}
        )
        if flags:
            extra["flags"] = flags
        versatile = weapon_versatile_damage(record)
        if versatile:
            extra["versatile_damage"] = versatile
        ranges = weapon_ranges(record)
        if ranges:
            extra["ranges"] = ranges
        if weapon_reach(record) > 5:
            extra["reach"] = weapon_reach(record)
        equipment.append(
            item_definition(
                ident,
                record["name"],
                weapon_description(record),
                category="weapon",
                price=gp_to_sp(item_costs.get(record["key"], 0)),
                tag="weapon",
                **extra,
            )
        )
    for record in armor:
        ident = f"armor-{key_id(record['key'])}"
        data = armor_feature_data(record)
        mode = data.pop("armor_mode")
        equipment.append(
            item_definition(
                ident,
                record["name"],
                data.pop("description"),
                category="armor",
                price=gp_to_sp(item_costs.get(record["key"], 0)),
                armor_mode=mode,
                icon=armor_icon(record),
                **data,
            )
        )
    handled = {record["key"] for record in weapons} | {record["key"] for record in armor}
    for record in items:
        if record["key"] in handled:
            continue
        ident = f"item-{key_id(record['key'])}"
        category = (record.get("category") or {}).get("key", "gear")
        extra: dict[str, Any] = {"size": item_size_for(record), "icon": item_icon(record)}
        if record.get("weapon") and record["weapon"].get("damage_dice"):
            weapon = record["weapon"]
            extra["damage"] = weapon_damage(weapon)
            extra["attributes"] = weapon_attributes(weapon)
            extra["weapon_category"] = "simple" if weapon.get("is_simple") else "martial"
            extra["weapon_properties"] = sorted(
                entry["property"]["name"].casefold() for entry in weapon.get("properties", [])
            )
            extra["armor_mode"] = "add"
        if record.get("armor") and record["armor"].get("ac_base") is not None:
            data = armor_feature_data(record["armor"])
            data.pop("armor_mode", None)
            data.pop("description", None)
            extra.update(data)
            extra.setdefault("armor_mode", "max")
        catalogue = category if category in ("weapon", "armor") else "gear"
        equipment.append(
            item_definition(
                ident,
                record["name"],
                record.get("desc", "") or record["name"],
                category=catalogue,
                price=gp_to_sp(record.get("cost")),
                tag="weapon" if catalogue == "weapon" and extra.get("damage") else "",
                **extra,
            )
        )
    for record in magic:
        ident = f"magic-{key_id(record['key'])}"
        extra: dict[str, Any] = {"size": item_size_for(record), "icon": magic_icon(record)}
        if _record_key(record) == "bracers-of-archery":
            # The SRD item makes its wearer proficient with these two bows.
            extra["weapon_training"] = ["weapon-longbow", "weapon-shortbow"]
        mode = "max"
        if record.get("weapon"):
            weapon = record["weapon"]
            extra["damage"] = weapon_damage(weapon)
            extra["attributes"] = weapon_attributes(weapon)
            extra["size"] = weapon_size(weapon)
            extra["weapon_category"] = "simple" if weapon.get("is_simple") else "martial"
            extra["weapon_properties"] = sorted(
                entry["property"]["name"].casefold() for entry in weapon.get("properties", [])
            )
        if record.get("armor"):
            data = armor_feature_data(record["armor"])
            mode = data.pop("armor_mode", "max")
            data.pop("description", None)
            extra.update(data)
        extras = {"size": extra.pop("size"), **extra} if "size" in extra else extra
        rarity = (record.get("rarity") or {}).get("key", "uncommon")
        equipment.append(
            item_definition(
                ident,
                record["name"],
                item_description(record),
                category="magic",
                price=gp_to_sp(RARITY_PRICE_GP.get(rarity, 400)),
                armor_mode=mode,
                **extras,
            )
        )
    groups: dict[str, list[dict[str, Any]]] = {
        "currency": [],
        "weapons": [],
        "armor": [],
        "gear": [],
    }
    for entry in equipment:
        groups[_item_group(entry)].append(entry)
    write_files({f"items/{name}.json": entries for name, entries in groups.items()})
    write(
        "equipment.json",
        [
            {
                "type": "marketplace",
                "id": "dnd-srd-2024",
                "currency": "silver",
            },
            feature(
                "encumbered",
                "Encumbered",
                "Carrying more than the pack holds. Drop enough gear to move freely again.",
                feature_type="trait",
            ),
        ],
    )


def build_spells() -> dict[str, dict[int, list[str]]]:
    spells = load("spells")
    definitions = []
    spell_lists: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    class_ids = {
        "barbarian",
        "bard",
        "cleric",
        "druid",
        "fighter",
        "monk",
        "paladin",
        "ranger",
        "rogue",
        "sorcerer",
        "warlock",
        "wizard",
    }
    cast_resources = {"action": "action", "bonus-action": "bonus_action", "reaction": "reaction"}
    for spell in spells:
        ident = f"spell-{key_id(spell['key'])}"
        spell_classes = sorted(
            key_id(class_ref["key"])
            for class_ref in spell.get("classes", [])
            if key_id(class_ref["key"]) in class_ids
        )
        definitions.append(
            feature(
                ident,
                spell["name"],
                spell_description(spell),
                feature_type="spell",
                tag=(spell.get("school") or {}).get("key", ""),
                rank=int(spell.get("level") or 0),
                turn_resource=cast_resources.get(str(spell.get("casting_time") or ""), ""),
                groups=spell_classes,
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
                    "max_value": 20,
                    "choices": list(ABILITY_NAMES),
                },
                {
                    "id": "second",
                    "kind": "stat",
                    "label": "Second ability",
                    "grant": "score",
                    "amount": 1,
                    "max_value": 20,
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
        if ident == "feat-defense":
            extra["defense_bonus"] = 1
        if ident == "feat-alert":
            extra["initiative_proficient"] = True
        if ident == "feat-savage-attacker":
            extra["weapon_damage_reroll"] = True
        if ident == "feat-archery":
            extra["attack_bonus"] = 2
            extra["weapon_kind"] = "ranged"
        if ident == "feat-magic-initiate":
            extra["picks"] = [
                {
                    "id": "list",
                    "kind": "option",
                    "label": "Spell list",
                    "table": "spell_lists",
                },
                {
                    "id": "cantrips",
                    "kind": "spell",
                    "label": "Cantrips",
                    "grant": "learn",
                    "count": 2,
                    "max_rank": 0,
                    "groups_from": "list",
                },
                {
                    "id": "spell",
                    "kind": "spell",
                    "label": "Level 1 spell",
                    "grant": "learn",
                    "count": 1,
                    "min_rank": 1,
                    "max_rank": 1,
                    "groups_from": "list",
                },
                {
                    "id": "ability",
                    "kind": "stat",
                    "label": "Casting ability",
                    "choices": ["int", "wis", "cha"],
                },
            ]
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


DRAGON_ANCESTRIES = {
    "Black": "acid",
    "Blue": "lightning",
    "Brass": "fire",
    "Bronze": "lightning",
    "Copper": "acid",
    "Gold": "fire",
    "Green": "poison",
    "Red": "fire",
    "Silver": "cold",
    "White": "cold",
}


LINEAGE_SPELLS = {
    "lineage-elf-drow": {
        0: ["Dancing Lights"],
        3: ["Faerie Fire"],
        5: ["Darkness"],
    },
    "lineage-elf-high-elf": {
        0: ["Prestidigitation"],
        3: ["Detect Magic"],
        5: ["Misty Step"],
    },
    "lineage-elf-wood-elf": {
        0: ["Druidcraft"],
        3: ["Longstrider"],
        5: ["Pass without Trace"],
    },
    "lineage-gnome-forest": {
        0: ["Minor Illusion", "Speak with Animals"],
    },
    "lineage-gnome-rock": {
        0: ["Mending", "Prestidigitation"],
    },
    "lineage-tiefling-abyssal": {
        0: ["Poison Spray"],
        3: ["Ray of Sickness"],
        5: ["Hold Person"],
    },
    "lineage-tiefling-chthonic": {
        0: ["Chill Touch"],
        3: ["False Life"],
        5: ["Ray of Enfeeblement"],
    },
    "lineage-tiefling-infernal": {
        0: ["Fire Bolt"],
        3: ["Hellish Rebuke"],
        5: ["Darkness"],
    },
}


SPECIES_LINEAGE_FEATURES = {
    "elf": [
        (
            "drow",
            "Drow Lineage",
            (
                "The range of your Darkvision increases to 120 feet. You know the Dancing Lights "
                "cantrip. At level 3 you learn Faerie Fire, and at level 5 you learn Darkness; each "
                "is always prepared and can be cast once without a spell slot per Long Rest."
            ),
        ),
        (
            "high-elf",
            "High Elf Lineage",
            (
                "You know the Prestidigitation cantrip, and you can replace it with another Wizard "
                "cantrip whenever you finish a Long Rest. At level 3 you learn Detect Magic, and at "
                "level 5 you learn Misty Step; each is always prepared and can be cast once without "
                "a spell slot per Long Rest."
            ),
        ),
        (
            "wood-elf",
            "Wood Elf Lineage",
            (
                "Your Speed increases to 35 feet. You know the Druidcraft cantrip. At level 3 you "
                "learn Longstrider, and at level 5 you learn Pass without Trace; each is always "
                "prepared and can be cast once without a spell slot per Long Rest."
            ),
        ),
    ],
    "gnome": [
        (
            "forest",
            "Forest Gnome Lineage",
            (
                "You know the Minor Illusion cantrip and always have Speak with Animals prepared. "
                "You can cast Speak with Animals without a spell slot a number of times equal to "
                "your Proficiency Bonus, regaining the uses on a Long Rest."
            ),
        ),
        (
            "rock",
            "Rock Gnome Lineage",
            (
                "You know the Mending and Prestidigitation cantrips. You can spend 10 minutes "
                "casting Prestidigitation to create a Tiny clockwork device, with up to three "
                "devices existing at once."
            ),
        ),
    ],
    "goliath": [
        (
            "cloud",
            "Cloud Giant Ancestry",
            (
                "As a Bonus Action, you can magically teleport up to 30 feet to an unoccupied "
                "space you can see. You can use this a number of times equal to your Proficiency "
                "Bonus, regaining all uses on a Long Rest."
            ),
        ),
        (
            "fire",
            "Fire Giant Ancestry",
            (
                "When you hit a target with an attack roll and deal damage, you can deal an extra "
                "1d10 Fire damage. You can use this a number of times equal to your Proficiency "
                "Bonus, regaining all uses on a Long Rest."
            ),
        ),
        (
            "frost",
            "Frost Giant Ancestry",
            (
                "When you hit a target with an attack roll and deal damage, you can deal an extra "
                "1d6 Cold damage and reduce its Speed by 10 feet until the start of your next turn. "
                "You can use this a number of times equal to your Proficiency Bonus, regaining all "
                "uses on a Long Rest."
            ),
        ),
        (
            "hill",
            "Hill Giant Ancestry",
            (
                "When you hit a Large or smaller creature with an attack roll and deal damage, you "
                "can give it the Prone condition. You can use this a number of times equal to your "
                "Proficiency Bonus, regaining all uses on a Long Rest."
            ),
        ),
        (
            "stone",
            "Stone Giant Ancestry",
            (
                "When you take damage, you can take a Reaction to roll 1d12, add your Constitution "
                "modifier, and reduce the damage by that total. You can use this a number of times "
                "equal to your Proficiency Bonus, regaining all uses on a Long Rest."
            ),
        ),
        (
            "storm",
            "Storm Giant Ancestry",
            (
                "When you take damage from a creature within 60 feet, you can take a Reaction to "
                "deal 1d8 Thunder damage to it. You can use this a number of times equal to your "
                "Proficiency Bonus, regaining all uses on a Long Rest."
            ),
        ),
    ],
    "tiefling": [
        (
            "abyssal",
            "Abyssal Legacy",
            (
                "You have Resistance to Poison damage and know the Poison Spray cantrip. At level 3 "
                "you learn Ray of Sickness, and at level 5 you learn Hold Person; each is always "
                "prepared and can be cast once without a spell slot per Long Rest."
            ),
            ["poison"],
        ),
        (
            "chthonic",
            "Chthonic Legacy",
            (
                "You have Resistance to Necrotic damage and know the Chill Touch cantrip. At level "
                "3 you learn False Life, and at level 5 you learn Ray of Enfeeblement; each is "
                "always prepared and can be cast once without a spell slot per Long Rest."
            ),
            ["necrotic"],
        ),
        (
            "infernal",
            "Infernal Legacy",
            (
                "You have Resistance to Fire damage and know the Fire Bolt cantrip. At level 3 you "
                "learn Hellish Rebuke, and at level 5 you learn Darkness; each is always prepared "
                "and can be cast once without a spell slot per Long Rest."
            ),
            ["fire"],
        ),
    ],
}


def species_lineage_features() -> list[dict[str, Any]]:
    """The named lineage choices for the species whose SRD traits offer them."""
    features: list[dict[str, Any]] = []
    for species_id, entries in SPECIES_LINEAGE_FEATURES.items():
        for entry in entries:
            slug_id, name, description, *resistance = entry
            extra: dict[str, Any] = {}
            if resistance:
                extra["resistances"] = resistance[0]
            if species_id == "goliath":
                extra["resource_counter"] = "giant_ancestry"
                if slug_id == "cloud":
                    extra["turn_resource"] = "bonus_action"
                elif slug_id in ("stone", "storm"):
                    extra["turn_resource"] = "reaction"
            if species_id in ("elf", "gnome", "tiefling"):
                extra["picks"] = [
                    {
                        "id": "ability",
                        "kind": "stat",
                        "label": "Spellcasting ability",
                        "choices": ["int", "wis", "cha"],
                    }
                ]
            features.append(
                feature(
                    f"lineage-{species_id}-{slug_id}",
                    f"{species_id.title()}: {name}",
                    description,
                    feature_type="trait",
                    tag=f"lineage-{species_id}",
                    rank=0,
                    **extra,
                )
            )
    return features


def build_species() -> None:
    species = load("species")
    spell_definitions = json.loads((OUT / "spells.json").read_text(encoding="utf-8"))
    spells_by_name = {
        str(entry["name"]).casefold(): str(entry["id"]) for entry in spell_definitions
    }
    definitions: list[dict[str, Any]] = []
    files: dict[str, list[dict[str, Any]]] = {}
    for species_order, record in enumerate(species):
        start = len(definitions)
        species_id = key_id(record["key"])
        trait_ids = []
        for trait in record.get("traits", []):
            ident = f"species-{species_id}-{slug(trait['name'])}"
            extra: dict[str, Any] = {}
            name = trait["name"]
            if species_id == "dragonborn" and name == "Draconic Ancestry":
                pass
            elif name == "Keen Senses":
                extra["picks"] = [
                    {
                        "id": "skill",
                        "kind": "skill",
                        "label": "Skill",
                        "choices": ["insight", "perception", "survival"],
                        "grant": "proficiency",
                    }
                ]
            elif name == "Dwarven Toughness":
                extra["level_hp_bonus"] = 1
            elif name == "Dwarven Resilience":
                extra["resistances"] = ["poison"]
            elif species_id == "human" and name == "Size":
                extra["picks"] = [
                    {"id": "size", "kind": "option", "label": "Size", "table": "human_size"}
                ]
            rank = 0
            min_level = 5 if name in ("Draconic Flight", "Large Form") else 0
            extra.setdefault("turn_resource", inferred_turn_resource(trait["desc"]))
            definitions.append(
                feature(
                    ident,
                    f"{record['name']}: {name}",
                    trait["desc"],
                    feature_type="trait",
                    tag="species",
                    rank=rank,
                    min_level=min_level,
                    **extra,
                )
            )
            trait_ids.append(ident)
        if species_id == "dragonborn":
            for dragon, damage_type in DRAGON_ANCESTRIES.items():
                definitions.append(
                    feature(
                        f"lineage-dragonborn-{slug(dragon)}",
                        f"Dragonborn: {dragon} Ancestry",
                        f"Your draconic ancestor is a {dragon} dragon. Your Breath Weapon deals "
                        f"{damage_type} damage, and you have Resistance to {damage_type} damage.",
                        feature_type="trait",
                        tag="lineage-dragonborn",
                        rank=0,
                        resistances=[damage_type],
                        damage_type=damage_type,
                    )
                )
        if species_id in SPECIES_LINEAGE_FEATURES:
            lineage_features = [
                item
                for item in species_lineage_features()
                if item["tag"] == f"lineage-{species_id}"
            ]
            for item in lineage_features:
                spells = LINEAGE_SPELLS.get(item["id"], {})
                base_spells = [
                    spells_by_name[name.casefold()]
                    for name in spells.get(0, [])
                    if name.casefold() in spells_by_name
                ]
                if base_spells:
                    item["learn_spells"] = base_spells
                    item["always_prepared"] = True
                    item["free_cast"] = True
            definitions.extend(lineage_features)
        size = next((t["desc"] for t in record["traits"] if t["name"] == "Size"), "Medium")
        speed = next((t["desc"] for t in record["traits"] if t["name"] == "Speed"), "30 feet")
        template: dict[str, Any] = {
            "type": "template",
            "id": species_id,
            "kind": "species",
            "name": record["name"],
            "icon": SPECIES_ICONS.get(species_id, "humanoid"),
            "order": species_order,
            "description": (
                f"{record['name']}. {size}. Speed {speed}. "
                + " ".join(t["name"] + "." for t in record["traits"])
            ),
            "features": trait_ids,
        }
        if species_id == "human":
            template["skill_picks"] = [
                {"id": "skillful", "count": 1, "table": "all_skills", "max_level": 2}
            ]
            template["feat_picks"] = [
                {
                    "id": "versatile",
                    "count": 1,
                    "tag": "origin",
                    "feature_type": "feat",
                    "max_rank": 1,
                }
            ]
        if species_id == "dwarf":
            template["stats"] = {"hp": 1}
        if species_id == "goliath":
            template["counters"] = {"giant_ancestry": 2}
        if species_id == "dragonborn":
            template["feat_picks"] = [
                {
                    "id": "ancestry",
                    "count": 1,
                    "tag": "lineage-dragonborn",
                    "feature_type": "trait",
                    "max_rank": 0,
                }
            ]
        if species_id in SPECIES_LINEAGE_FEATURES:
            template["feat_picks"] = [
                {
                    "id": "lineage",
                    "count": 1,
                    "tag": f"lineage-{species_id}",
                    "feature_type": "trait",
                    "max_rank": 0,
                }
            ]
            for item in definitions:
                if item.get("tag") != f"lineage-{species_id}":
                    continue
                spells = LINEAGE_SPELLS.get(item["id"], {})
                for level, names in spells.items():
                    if level == 0:
                        continue
                    for name in names:
                        spell_id = spells_by_name.get(name.casefold())
                        if spell_id is None:
                            continue
                        spell_name = next(
                            entry["name"] for entry in spell_definitions if entry["id"] == spell_id
                        )
                        feature_id = f"{item['id']}-spell-{level}-{spell_id}"
                        definitions.append(
                            feature(
                                feature_id,
                                f"{record['name']}: {item['name'].split(': ', 1)[-1]} - {spell_name}",
                                f"You always have {spell_name} prepared. You can cast it once "
                                "without a spell slot, regaining that use on a Long Rest.",
                                feature_type="trait",
                                tag="species",
                                min_level=level,
                                requires_feature=item["id"],
                                learn_spells=[spell_id],
                                always_prepared=True,
                                free_cast=True,
                            )
                        )
                        template["features"].append(feature_id)
        definitions.append(template)
        files[f"species/{species_id}.json"] = definitions[start:]
    write_files(files)


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


BACKGROUND_EQUIPMENT_NAMES = {
    "Calligrapher's Supplies": "Calligrapher's Supplies (10 GP)",
    "Book": "Book",
    "Holy Symbol": "Holy Symbol",
    "Parchment": "Parchment",
    "Robe": "Robe",
    "Dagger": "Dagger",
    "Daggers": "Dagger",
    "Thieves' Tools": "Thieves' Tools",
    "Crowbar": "Crowbar",
    "Pouch": "Pouch",
    "Pouches": "Pouch",
    "Traveler's Clothes": "Clothes, Traveler's",
    "Quarterstaff": "Quarterstaff",
    "Spear": "Spear",
    "Shortbow": "Shortbow",
    "Arrows": "Arrows (20)",
    "Gaming Set": "Gaming Set, Dice",
    "Healer's Kit": "Healer's Kit",
    "Quiver": "Quiver",
}


def background_equipment_grant(equipment: str) -> dict[str, Any]:
    """The background's A/B starting package, parsed into catalogue names and silver."""
    if not equipment:
        return {}
    match = re.search(r"\(A\)\s*(.*?);?\s*or\s*\(B\)\s*(\d+)\s*GP", equipment)
    if match is None:
        return {}
    package, gold = match.groups()
    items: list[str] = []
    silver = 0
    for part in package.split(","):
        part = part.strip()
        if not part:
            continue
        money = re.fullmatch(r"(\d+)\s*GP", part)
        if money:
            silver += int(money.group(1)) * 10
            continue
        count = 1
        quantity = re.match(r"^(\d+)\s+(.*)$", part)
        if quantity:
            count, part = int(quantity.group(1)), quantity.group(2)
        name = re.sub(r"\s*\([^)]*\)$", "", part).strip()
        catalogue = BACKGROUND_EQUIPMENT_NAMES.get(name)
        if catalogue is None:
            raise ValueError(f"background equipment {name!r} is not mapped to the catalogue")
        # Arrows (20) is already one bundle in the catalogue, so "20 Arrows" is one item.
        if name == "Arrows":
            count = 1
        items.extend([catalogue] * count)
    return {
        "items": items,
        "silver": silver,
        "options": [
            {
                "id": "a",
                "label": "A: background kit",
                "items": items,
                "silver": silver,
            },
            {"id": "b", "label": f"B: {gold} GP", "silver": int(gold) * 10},
        ],
    }


def build_backgrounds() -> None:
    records = load("backgrounds")
    files: dict[str, list[dict[str, Any]]] = {}
    for background_order, record in enumerate(records):
        ident = key_id(record["key"])
        abilities = background_abilities(record)
        skills = background_skills(record)
        feat_id = background_feat(record)
        asi_id = f"background-asi-{ident}"
        entries: list[dict[str, Any]] = []
        tool = next(
            (
                benefit.get("desc", "")
                for benefit in record.get("benefits", [])
                if benefit.get("type") == "tool_proficiency"
            ),
            "",
        )
        if tool.casefold().startswith("choose one kind of "):
            tool = tool[len("choose one kind of ") :]
        entries.append(
            feature(
                asi_id,
                f"{record['name']}: Ability Scores",
                (
                    "Increase one of these abilities by 2 and another by 1, or each of them by 1: "
                    + ", ".join(ABILITY_NAMES[ability] for ability in abilities)
                    + "."
                ),
                feature_type="trait",
                tag="background",
                tool_training=[tool] if tool else [],
                picks=[
                    {
                        "id": "scores",
                        "kind": "stat",
                        "label": "Abilities",
                        "grant": "score",
                        "amount": 1,
                        "count": 3,
                        "max_value": 20,
                        "choices": abilities,
                        "patterns": [[1, 1, 1], [2, 1]],
                    }
                ],
            )
        )
        features = [asi_id]
        if feat_id:
            features.append(feat_id)
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
        entries.append(
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
                "order": background_order,
                "description": description,
                "skills": {skill: 1 for skill in skills},
                "features": features,
                "grant": background_equipment_grant(equipment),
            }
        )
        files[f"backgrounds/{ident}.json"] = entries
    write_files(files)


CLASS_ALWAYS_PREPARED = {
    "druid": {1: ["Speak with Animals"]},
    "paladin": {2: ["Divine Smite"], 5: ["Find Steed"]},
    "ranger": {1: ["Hunter's Mark"]},
}

CLASS_FEATURE_USE = {
    "Rage": ("rage", "bonus_action"),
    "Action Surge": ("action_surge", ""),
    "Indomitable": ("indomitable", ""),
    "Arcane Recovery": ("arcane_recovery", ""),
    "Second Wind": ("second_wind", "bonus_action"),
    "Bardic Inspiration": ("bardic_inspiration", "bonus_action"),
    "Channel Divinity": ("channel_divinity", "action"),
    "Wild Shape": ("wild_shape", "action"),
    "Lay On Hands": ("lay_on_hands", "action"),
    "Flurry of Blows": ("focus_points", "bonus_action"),
    "Patient Defense": ("focus_points", "bonus_action"),
    "Step of the Wind": ("focus_points", "bonus_action"),
    "Monk's Focus": ("focus_points", "bonus_action"),
    "Stunning Strike": ("focus_points", ""),
    "Deflect Attacks": ("", "reaction"),
    "Deflect Energy": ("", "reaction"),
    "Preserve Life": ("channel_divinity", "action"),
    "Sacred Weapon": ("channel_divinity", ""),
    "Abjure Foes": ("channel_divinity", "action"),
    "Cutting Words": ("bardic_inspiration", "reaction"),
    "Land's Aid": ("wild_shape", "action"),
}


CLASS_MASTERY_COUNTS = {
    "barbarian": {1: 2, 4: 3, 10: 4},
    "fighter": {1: 3, 4: 4, 10: 5, 16: 6},
    "paladin": {1: 2},
    "ranger": {1: 2},
    "rogue": {1: 2},
}
# The weapon groups each class's Weapon Mastery feature may choose from. A Barbarian chooses
# Simple or Martial Melee weapons; a Rogue chooses weapons with which the class is proficient
# (simple weapons and martial weapons with the finesse or light property). Paladins, Rangers,
# and Fighters are proficient with every simple and martial weapon, so they need no filter.
CLASS_MASTERY_GROUPS = {
    "barbarian": ["melee"],
    "rogue": ["simple", "finesse", "light"],
}

CLASS_SKIP_FEATURES = {
    "Ability Score Improvement",
    "Epic Boon",
    "Fighting Style",
    "Proficiency Bonus",
    "Cantrips Known",
}
CLASS_FEATURE_NAMES = {
    "Magic Cunning": "Magical Cunning",
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
    levels = [
        int(entry["level"]) for entry in record.get("gained_at", []) if int(entry["level"]) > 0
    ]
    if levels:
        return sorted(set(levels))
    match = re.search(r"level (\d+)", record.get("desc", ""), re.IGNORECASE)
    if match and int(match.group(1)) > 0:
        return [int(match.group(1))]
    return [default] if not record.get("gained_at") else []


def class_feature_id(class_id: str, record: dict[str, Any]) -> str:
    key = key_id(record["key"])
    prefix = f"{class_id}_"
    key = key.removeprefix(prefix)
    return f"class-{class_id}-{slug(key)}"


def subclass_spell_list(
    record: dict[str, Any], spells_by_name: dict[str, str]
) -> tuple[dict[str, list[str]], bool]:
    """A subclass's always-prepared spells by class level, parsed from its Spells table."""
    if record["name"] == "Circle of the Land":
        return {}, False
    by_level: dict[str, list[str]] = {}
    for entry in record.get("features", []):
        name = entry.get("name", "")
        text = entry.get("desc", "")
        if not name.endswith(" Spells") or "Table:" not in text:
            continue
        for level, names in re.findall(r"^\|(\d+)\|([^|]+)\|", text, flags=re.MULTILINE):
            spell_ids = [
                spells_by_name[spell.strip().casefold()]
                for spell in names.split(",")
                if spell.strip().casefold() in spells_by_name
            ]
            if spell_ids:
                by_level[level] = spell_ids
    return by_level, bool(by_level)


def subclass_land_spells(
    record: dict[str, Any], spells_by_name: dict[str, str]
) -> dict[str, dict[str, list[str]]]:
    """The four Circle of the Land tables, keyed by land and then class level."""
    if record["name"] != "Circle of the Land":
        return {}
    lands: dict[str, dict[str, list[str]]] = {}
    for entry in record.get("features", []):
        if entry.get("name") != "Circle of the Land Spells":
            continue
        text = entry.get("desc", "")
        for match in re.finditer(r"Table: (\w+) Land(.*?)(?=\nTable:|\Z)", text, flags=re.DOTALL):
            land = match.group(1).casefold()
            rows: dict[str, list[str]] = {}
            for level, names in re.findall(
                r"^\|(\d+)\|([^|]+)\|", match.group(2), flags=re.MULTILINE
            ):
                spell_ids = [
                    spells_by_name[spell.strip().casefold()]
                    for spell in names.split(",")
                    if spell.strip().casefold() in spells_by_name
                ]
                if spell_ids:
                    rows[level] = spell_ids
            if rows:
                lands[land] = rows
    return lands


def inferred_turn_resource(text: str) -> str:
    """The turn resource a feature's text says it takes, when it says so plainly."""
    lower = text.casefold()
    if "as a bonus action" in lower or "take a bonus action" in lower:
        return "bonus_action"
    if "as a reaction" in lower or "take a reaction" in lower:
        return "reaction"
    if "as an action" in lower or "take the magic action" in lower:
        return "action"
    return ""


def feature_choice_picks(name: str, class_id: str = "") -> list[dict[str, Any]]:
    if name == "Weapon Mastery":
        count = CLASS_MASTERY_COUNTS.get(class_id, {}).get(1, 2)
        pick: dict[str, Any] = {
            "id": "weapons",
            "kind": "feature",
            "label": "Weapons",
            "feature_type": "item",
            "tags": ["weapon"],
            "count": count,
        }
        groups = CLASS_MASTERY_GROUPS.get(class_id)
        if groups:
            pick["groups"] = groups
        return [pick]
    if name == "Pact Magic":
        # The class's cantrips and prepared spells are chosen on the level-up page; there is no
        # free-text choice to make here.
        return []
    if name == "Deft Explorer":
        return [
            {
                "id": "expertise",
                "kind": "skill",
                "label": "Expertise",
                "grant": "expertise",
                "choices": list(SKILLS),
            },
            {"id": "languages", "kind": "text", "label": "Languages", "count": 2},
        ]
    if name == "Scholar":
        return [
            {
                "id": "expertise",
                "kind": "skill",
                "label": "Expertise",
                "grant": "expertise",
                "choices": ["arcana", "history", "investigation", "medicine", "nature", "religion"],
            }
        ]
    if name == "Bonus Proficiencies":
        return [
            {
                "id": "skills",
                "kind": "skill",
                "label": "Skills",
                "grant": "proficiency",
                "count": 3,
            }
        ]
    if name == "Evocation Savant":
        return [
            {
                "id": "spells",
                "kind": "spell",
                "label": "Evocation spells",
                "grant": "learn",
                "count": 2,
                "min_rank": 1,
                "max_rank": 2,
                "tags": ["evocation"],
                "groups": ["wizard"],
            }
        ]
    if name == "Hunter's Prey":
        return [
            {
                "id": "prey",
                "kind": "feature",
                "label": "Prey",
                "grant": "learn",
                "choices": ["hunter-prey-colossus-slayer", "hunter-prey-horde-breaker"],
            }
        ]
    return []


def markdown_options(text: str) -> list[tuple[str, str]]:
    """Split an option list written with `### Name` headings into (name, body) pairs."""
    # The source leaves one option's heading as an indented line without ###; give it a
    # heading so every option is read the same way.
    text = re.sub(r"\n\s+([A-Z][^\n]+)\n\n\*Cost:", r"\n### \1\n\n*Cost:", text)
    options: list[tuple[str, str]] = []
    parts = re.split(r"^\s*###\s+", text, flags=re.MULTILINE)[1:]
    for part in parts:
        lines = part.splitlines()
        if not lines:
            continue
        name = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        if name:
            options.append((name, body))
    return options


def option_features(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Metamagic and Eldritch Invocation options, plus the Divine and Primal Orders. Each
    definition goes in the file of the class or subclass that offers it."""
    definitions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        for entry in record.get("features", []):
            if entry.get("feature_type") != "CLASS_FEATURE_OPTION_LIST":
                continue
            if record["name"] == "Sorcerer":
                tag = "metamagic"
                prefix = "metamagic"
                rank = 0
                path = "classes/sorcerer/sorcerer.json"
            elif record["name"] == "Warlock":
                tag = "invocation"
                prefix = "invocation"
                rank = 0
                path = "classes/warlock/warlock.json"
            else:
                continue
            for name, body in markdown_options(entry.get("desc", "")):
                level = 0
                match = re.search(r"Prerequisite:.*?Level (\d+)\+", body)
                if match:
                    level = int(match.group(1))
                definitions[path].append(
                    feature(
                        f"{prefix}-{slug(name)}",
                        name,
                        body or name,
                        feature_type="feat",
                        tag=tag,
                        rank=level or rank,
                        repeatable="**Repeatable.**" in body,
                    )
                )
    definitions["classes/cleric/cleric.json"] += [
        feature(
            "divine-order-protector",
            "Divine Order: Protector",
            "Trained for battle, you gain proficiency with Martial weapons and training with "
            "Heavy armor.",
            feature_type="feat",
            tag="divine-order",
            rank=1,
            armor_training=["heavy"],
            weapon_training=["martial"],
        ),
        feature(
            "divine-order-thaumaturge",
            "Divine Order: Thaumaturge",
            "You know one extra cantrip from the Cleric spell list. In addition, your mystical "
            "connection to the divine gives you a bonus to your Intelligence (Arcana or Religion) "
            "checks. The bonus equals your Wisdom modifier (minimum of +1).",
            feature_type="feat",
            tag="divine-order",
            rank=1,
        ),
    ]
    definitions["classes/druid/druid.json"] += [
        feature(
            "primal-order-magician",
            "Primal Order: Magician",
            "You know one extra cantrip from the Druid spell list. In addition, your mystical "
            "connection to nature gives you a bonus to your Intelligence (Arcana or Nature) "
            "checks. The bonus equals your Wisdom modifier (minimum bonus of +1).",
            feature_type="feat",
            tag="primal-order",
            rank=1,
        ),
        feature(
            "primal-order-warden",
            "Primal Order: Warden",
            "Trained for battle, you gain proficiency with Martial weapons and training with "
            "Medium armor.",
            feature_type="feat",
            tag="primal-order",
            rank=1,
            armor_training=["medium"],
            weapon_training=["martial"],
        ),
    ]
    definitions["classes/ranger/hunter.json"] += [
        feature(
            "hunter-prey-colossus-slayer",
            "Colossus Slayer",
            "Your tenacity can wear down the most potent foes. When you hit a creature with "
            "a weapon, the creature takes an extra 1d8 damage if it is below its hit point "
            "maximum. You can deal this extra damage only once per turn.",
            feature_type="trait",
            tag="hunter-prey",
            rank=1,
        ),
        feature(
            "hunter-prey-horde-breaker",
            "Horde Breaker",
            "Once on each of your turns when you make a weapon attack, you can make another "
            "attack with the same weapon against a different creature within 5 feet of the "
            "original target and within range.",
            feature_type="trait",
            tag="hunter-prey",
            rank=1,
        ),
    ]
    return dict(definitions)


def class_table_values(record: dict[str, Any], name: str, default: int = 0) -> list[int]:
    """A 20-level array from one CLASS_TABLE_DATA column, carrying each value forward."""
    values = [default] * 20
    entry = next(
        (
            item
            for item in record.get("features", [])
            if item.get("feature_type") == "CLASS_TABLE_DATA" and item.get("name") == name
        ),
        None,
    )
    if entry is None:
        return values
    rows = sorted(entry.get("data_for_class_table") or [], key=lambda row: int(row["level"]))
    current = default
    by_level: dict[int, int] = {}
    for row in rows:
        raw = str(row["column_value"]).strip()
        try:
            current = int(raw)
        except ValueError:
            if raw.casefold() == "unlimited":
                current = 99
            else:
                continue
        by_level[int(row["level"])] = current
    current = default
    for level in range(1, 21):
        if level in by_level:
            current = by_level[level]
        values[level - 1] = current
    return values


def class_resources(record: dict[str, Any], class_id: str) -> dict[str, list[int]]:
    """The class resource pools, one 20-level array each."""
    resources: dict[str, list[int]] = {}
    tables = {
        "rage": "Rages",
        "bardic_inspiration": "Bardic Inspiration",
        "channel_divinity": "Channel Divinity",
        "wild_shape": "Wild Shape",
        "second_wind": "Second Wind",
        "focus_points": "Focus Points",
        "sorcery_points": "Sorcery Points",
        "favored_enemy": "Favored Enemy",
    }
    for key, table in tables.items():
        values = class_table_values(record, table)
        if any(values):
            resources[key] = values
    if class_id == "druid":
        resources["wild_shape"] = [0, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4]
    if class_id == "fighter":
        resources["action_surge"] = [0] + [1] * 15 + [2] * 4
        resources["indomitable"] = [0] * 8 + [1] * 4 + [2] * 4 + [3] * 4
    if class_id == "paladin":
        resources["lay_on_hands"] = [5 * level for level in range(1, 21)]
    if class_id == "ranger":
        resources["tireless"] = [0] * 9 + [1] * 11
    if class_id == "wizard":
        resources["arcane_recovery"] = [1] * 20
    if class_id == "warlock":
        resources["mystic_arcanum"] = [0] * 10 + [1] * 2 + [2] * 2 + [3] * 2 + [4] * 4
    if class_id == "bard":
        resources["bardic_inspiration"] = [0] * 20  # filled from Charisma by the rules module
    return resources


def build_classes(spell_lists: dict[str, dict[int, list[str]]]) -> None:
    records = load("classes")
    spell_definitions = json.loads((OUT / "spells.json").read_text(encoding="utf-8"))
    spells_by_name = {
        str(entry["name"]).casefold(): str(entry["id"]) for entry in spell_definitions
    }
    base = {key_id(record["key"]): record for record in records if not record.get("subclass_of")}
    subclass_records = [record for record in records if record.get("subclass_of")]
    definitions: list[dict[str, Any]] = []
    files: dict[str, list[dict[str, Any]]] = {}
    progression_classes: dict[str, Any] = {}
    for class_order, (class_id, record) in enumerate(base.items()):
        start = len(definitions)
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
                # The 2024 Bard's second Expertise is at level 9; Open5e's class table omits it.
                if class_id == "bard":
                    expertise_levels.add(9)
                continue
            if "Subclass" in name:
                # Every 2024 class chooses its subclass at level 3.
                subclass_level = 3
                continue
            if name in CLASS_SKIP_FEATURES:
                continue
            ident = class_feature_id(class_id, entry)
            name = CLASS_FEATURE_NAMES.get(name, name)
            rank = min(levels)
            # The 2024 Monk features moved between levels in the SRD table.
            if class_id == "monk" and name == "Deflect Attacks":
                levels = [3]
                rank = 3
            elif class_id == "monk" and name == "Stunning Strike":
                levels = [5]
                rank = 5
            details = "; ".join(
                f"Level {item['level']}: {item['detail']}"
                for item in entry.get("gained_at", [])
                if item.get("detail")
            )
            text = entry.get("desc", "").strip()
            if details:
                text = f"{text}\n\n{details}".strip()
            extra: dict[str, Any] = {}
            use = CLASS_FEATURE_USE.get(name)
            if use is not None:
                extra["resource_counter"], extra["turn_resource"] = use
            defense = FEATURE_DEFENSE.get((class_id, name))
            if defense is not None:
                extra["defense_floor"] = defense
            if not extra.get("turn_resource"):
                extra["turn_resource"] = inferred_turn_resource(text)
            if ident == "class-barbarian-rage":
                extra["active_mark"] = "raging"
                extra["damage_bonus_levels"] = {1: 2, 9: 3, 16: 4}
                extra["weapon_kind"] = "melee"
                extra["resistances"] = ["bludgeoning", "piercing", "slashing"]
            if ident == "subclass-champion-improved-critical":
                extra["critical_min"] = 19
            if ident == "class-rogue-sneak-attack":
                extra["damage_dice_levels"] = {
                    1: "1d6",
                    3: "2d6",
                    5: "3d6",
                    7: "4d6",
                    9: "5d6",
                    11: "6d6",
                    13: "7d6",
                    15: "8d6",
                    17: "9d6",
                    19: "10d6",
                }
            definitions.append(
                feature(
                    ident,
                    name,
                    text or name,
                    feature_type="trait",
                    tag=f"class:{class_id}",
                    rank=rank,
                    picks=feature_choice_picks(name, class_id),
                    **extra,
                )
            )
            for level in levels:
                level_features[level].append(ident)
        # A class's always-prepared spells are learned as class features, so the sheet lists
        # them and they are castable without taking a prepared-spell slot.
        for level, spell_names in CLASS_ALWAYS_PREPARED.get(class_id, {}).items():
            for spell_name in spell_names:
                spell_id = spells_by_name.get(spell_name.casefold())
                if spell_id is not None:
                    level_features[level].append(spell_id)
        # Weapon Mastery grants more weapon kinds at later class levels.
        for level in CLASS_MASTERY_COUNTS.get(class_id, {}):
            if level == 1:
                continue
            mastery_id = f"class-{class_id}-weapon-mastery-{level}"
            definitions.append(
                feature(
                    mastery_id,
                    "Weapon Mastery: Additional Kind",
                    f"Your training lets you use the mastery property of one more weapon kind "
                    f"(chosen at level {level}).",
                    feature_type="trait",
                    tag=f"class:{class_id}",
                    rank=level,
                    picks=[{**feature_choice_picks("Weapon Mastery", class_id)[0], "count": 1}],
                )
            )
            level_features[level].append(mastery_id)
        if epic_levels:
            asi_levels.update(epic_levels)
        if class_id == "rogue":
            expertise_levels.discard(1)
        armor_training = CLASS_ARMOR_TRAINING.get(class_id, [])
        weapon_training = CLASS_WEAPON_TRAINING.get(class_id, [])
        tool_training = CLASS_TOOLS.get(class_id, [])
        if armor_training or weapon_training or tool_training:
            armor_training_id = f"class-{class_id}-armor-training"
            training_name = "Armor Training" if armor_training else "Weapon Training"
            pieces = []
            if armor_training:
                pieces.append("training with " + ", ".join(armor_training) + " armor")
            if tool_training:
                pieces.append("proficiency with " + ", ".join(tool_training))
            if weapon_training:
                labels = []
                for entry in weapon_training:
                    category, _, prop = entry.partition(":")
                    labels.append(
                        category + " weapons" + (f" with the {prop} property" if prop else "")
                    )
                pieces.append("proficiency with " + " and ".join(labels))
            definitions.append(
                feature(
                    armor_training_id,
                    training_name,
                    "You gain " + ", ".join(pieces) + ".",
                    feature_type="trait",
                    tag=f"class:{class_id}",
                    rank=1,
                    armor_training=armor_training,
                    weapon_training=weapon_training,
                    tool_training=tool_training,
                )
            )
            level_features[1].insert(0, armor_training_id)
        multiclass_armor = MULTICLASS_ARMOR.get(class_id, [])
        multiclass_weapon = MULTICLASS_WEAPON_TRAINING.get(class_id, [])
        multiclass_skills = MULTICLASS_SKILLS.get(class_id, 0)
        multiclass_tools = MULTICLASS_TOOLS.get(class_id, [])
        if multiclass_armor or multiclass_weapon or multiclass_tools or multiclass_skills:
            pieces = []
            if multiclass_armor:
                pieces.append("training with " + ", ".join(multiclass_armor) + " armor")
            if multiclass_weapon:
                pieces.append("proficiency with " + ", ".join(multiclass_weapon) + " weapons")
            if multiclass_tools:
                pieces.append("proficiency with " + ", ".join(multiclass_tools))
            if multiclass_skills:
                pieces.append("one skill of your choice from the class list")
            definitions.append(
                feature(
                    f"class-{class_id}-multiclass-training",
                    "Multiclass Training",
                    "You gain " + ", ".join(pieces) + ".",
                    feature_type="trait",
                    tag=f"class:{class_id}",
                    rank=1,
                    armor_training=multiclass_armor,
                    weapon_training=multiclass_weapon,
                    tool_training=multiclass_tools,
                )
            )
        kits = CLASS_KITS[class_id]
        resources = class_resources(record, class_id)
        counters: dict[str, int] = {"hit_dice": 1}
        for resource, values in resources.items():
            counters[resource] = values[0]
        if class_id == "bard":
            counters["bardic_inspiration"] = 1
        if class_id == "warlock":
            counters["invocation_picks"] = 1
        if CLASS_SPELLS[class_id][0]:
            counters["slot_1"] = 1 if class_id == "warlock" else 2
        if CLASS_CANTRIPS[class_id][0]:
            counters["cantrip_picks"] = CLASS_CANTRIPS[class_id][0]
        if CLASS_SPELLS[class_id][0]:
            # 2024 wizards begin with six spells in their spellbook, two more per level.
            counters["spell_picks"] = 6 if class_id == "wizard" else CLASS_SPELLS[class_id][0]
        if class_id == "rogue":
            counters["expertise_picks"] = 2
        template: dict[str, Any] = {
            "type": "template",
            "id": class_id,
            "kind": "class",
            "name": record["name"],
            "icon": CLASS_ICONS[class_id],
            "order": class_order,
            "description": (
                f"{CLASS_ROLES[class_id]} Hit Point Die: d{record['hit_dice'][1:]}. "
                "Saving Throw Proficiencies: "
                + ", ".join(
                    ABILITY_NAMES[ability]
                    for ability in CLASS_SAVES.get(
                        class_id,
                        tuple(
                            ABILITIES[save["name"].casefold()] for save in record["saving_throws"]
                        ),
                    )
                )
                + "."
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
                "options": [
                    {
                        "id": "a",
                        "label": "A: class kit",
                        "items": kits["items"],
                        "equip": kits["equip"],
                        "silver": kits["gold"] * 10,
                    },
                    *CLASS_KIT_OPTIONS.get(class_id, []),
                ],
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
        if class_id == "cleric":
            template["feat_picks"] = [
                {
                    "id": "divine_order",
                    "count": 1,
                    "tag": "divine-order",
                    "feature_type": "feat",
                    "max_rank": 1,
                }
            ]
        if class_id == "druid":
            template["feat_picks"] = [
                {
                    "id": "primal_order",
                    "count": 1,
                    "tag": "primal-order",
                    "feature_type": "feat",
                    "max_rank": 1,
                }
            ]
        if counters:
            template["counters"] = counters
        definitions.append(template)
        files[f"classes/{class_id}/{class_id}.json"] = definitions[start:]
        progression_classes[class_id] = {
            "hit_die": int(record["hit_dice"][1:]),
            "saves": list(
                CLASS_SAVES.get(
                    class_id,
                    tuple(ABILITIES[save["name"].casefold()] for save in record["saving_throws"]),
                )
            ),
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
            "resources": resources,
            "metamagic_levels": [2, 10, 17] if class_id == "sorcerer" else [],
            "metamagic_counts": [2, 2, 2] if class_id == "sorcerer" else [],
            "invocations": (
                class_table_values(record, "Eldritch Invocations")
                if class_id == "warlock"
                else [0] * 20
            ),
            "primary": CLASS_PRIMARY[class_id],
            "primary_any": class_id in CLASS_PRIMARY_ANY,
            "multiclass_armor": MULTICLASS_ARMOR.get(class_id, []),
            "multiclass_skills": MULTICLASS_SKILLS.get(class_id, 0),
            "multiclass_tools": MULTICLASS_TOOLS.get(class_id, []),
            "features": {str(level): ids for level, ids in sorted(level_features.items())},
            "always_prepared": {
                str(level): [
                    spells_by_name[name.casefold()]
                    for name in names
                    if name.casefold() in spells_by_name
                ]
                for level, names in CLASS_ALWAYS_PREPARED.get(class_id, {}).items()
            },
        }
    progression_subclasses: dict[str, Any] = {}
    for record in subclass_records:
        start = len(definitions)
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
            if subclass_id == "draconic-sorcery" and name == "Dragon Companion":
                feature_levels = [18]
            ident = f"subclass-{subclass_id}-{slug(key_id(entry['key']).split('_')[-1])}"
            extra: dict[str, Any] = {}
            if subclass_id == "draconic-sorcery" and name == "Draconic Resilience":
                extra["defense_floor"] = {
                    "base": 10,
                    "modifiers": ["cha"],
                    "requires_no_armor": True,
                }
                extra["level_hp_bonus"] = 1
                extra["flat_hp_bonus"] = 3
            use = CLASS_FEATURE_USE.get(name)
            if use is not None:
                extra["resource_counter"], extra["turn_resource"] = use
            extra.setdefault("turn_resource", inferred_turn_resource(entry.get("desc", "") or name))
            if ident == "subclass-champion-improved-critical":
                extra["critical_min"] = 19
            definitions.append(
                feature(
                    ident,
                    name,
                    (entry.get("desc", "") or name).strip(),
                    feature_type="trait",
                    tag=f"subclass:{subclass_id}",
                    rank=min(feature_levels),
                    picks=feature_choice_picks(name),
                    **extra,
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
        subclass_spells, always_prepared = subclass_spell_list(record, spells_by_name)
        files[f"classes/{parent}/{subclass_id}.json"] = definitions[start:]
        progression_subclasses[subclass_id] = {
            "class": parent,
            "features": {str(level): ids for level, ids in sorted(levels.items())},
            "spells": {
                level: ids
                for level, ids in sorted(subclass_spells.items(), key=lambda item: int(item[0]))
            },
            "always_prepared": always_prepared,
            "land_spells": subclass_land_spells(record, spells_by_name),
        }
    for relative, entries in option_features(records).items():
        files[relative] = [*files.get(relative, []), *entries]
    write_files(files)
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
        "slots": {"none": [[0] * 9 for _ in range(20)], "full": FULL_SLOTS, "half": HALF_SLOTS},
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
                "apply_level_class": "apply_level_up_class",
                "summary_class": "level_up_summary_class",
                "picks": [
                    {
                        "id": "multiclass_skills",
                        "label": "Multiclass Skill",
                        "kind": "skill",
                        "counter": "multiclass_skill_picks",
                        "costs": {"2": 1},
                        "minimum_levels": {"2": 2},
                        "options": "multiclass_skill_options",
                        "apply": "apply_skill_pick",
                    },
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
                        "id": "metamagic",
                        "label": "Metamagic",
                        "kind": "feature",
                        "counter": "metamagic_picks",
                        "feature_type": "feat",
                        "tags": ["metamagic"],
                        "options": "metamagic_options",
                        "apply": "apply_feat_pick",
                    },
                    {
                        "id": "invocations",
                        "label": "Eldritch Invocations",
                        "kind": "feature",
                        "counter": "invocation_picks",
                        "feature_type": "feat",
                        "tags": ["invocation"],
                        "options": "invocation_options",
                        "apply": "apply_feat_pick",
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
    parts = [f"{int(count or 1)}d{int(die.lstrip('Dd'))}"]
    extra_count = attack.get("extra_damage_die_count")
    extra_die = attack.get("extra_damage_die_type")
    if extra_count and extra_die:
        parts.append(f"{int(extra_count)}d{int(extra_die.lstrip('Dd'))}")
    dice = "+".join(parts)
    return f"{dice}{'+' if bonus >= 0 else '-'}{abs(bonus)}" if bonus else dice


def monster_usage(action: dict[str, Any]) -> str:
    """A short recharge or per-day note for an action."""
    usage = action.get("usage_limits") or {}
    kind = usage.get("type")
    param = usage.get("param")
    if kind == "RECHARGE_ON_ROLL" and param is not None:
        return f"Recharge {int(param)}-6"
    if kind == "PER_DAY" and param is not None:
        return f"{int(param)}/Day"
    if kind:
        return str(kind).replace("_", " ").title()
    return ""


def monster_actions(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        action
        for action in record.get("actions", [])
        if action.get("action_type") in ("ACTION", "BONUS_ACTION", "REACTION", "LEGENDARY_ACTION")
    ]


NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
}


def multiattack_count(record: dict[str, Any]) -> int:
    """The number of attacks a Multiattack action makes. The first sentence lists the attacks;
    later sentences are options such as replacing an attack, which do not add another."""
    for action in record.get("actions", []):
        if action.get("name", "").casefold() != "multiattack":
            continue
        text = action.get("desc", "")
        first_sentence = re.split(r"\.\s", text, maxsplit=1)[0]
        if "as many attacks as it has heads" in first_sentence.casefold():
            return 5
        counts = [
            NUMBER_WORDS.get(word.casefold(), int(word) if word.isdigit() else 0)
            for word in re.findall(
                r"\b(one|two|three|four|five|six|\d+)\b[^.;]*?\battacks?\b",
                first_sentence,
                re.IGNORECASE,
            )
        ]
        if counts:
            return max(1, sum(counts))
        match = re.search(r"\bmakes (\d+) attacks?\b", first_sentence, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 1


def monster_description(record: dict[str, Any]) -> str:
    size = (record.get("size") or {}).get("name", "Medium")
    kind = (record.get("type") or {}).get("name", "creature")
    alignment = record.get("alignment") or "unaligned"
    speed = record.get("speed") or {}
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
        f"Speed {(speeds or '30 ft.').rstrip('.')}.",
    ]
    saves = record.get("saving_throws") or {}
    proficient_saves = []
    for name in ABILITY_NAMES.values():
        bonus = saves.get(name.casefold())
        score = int(record["ability_scores"].get(name.casefold()) or 10)
        if bonus is not None and int(bonus) != (score - 10) // 2:
            proficient_saves.append(f"{name} {int(bonus):+d}")
    if proficient_saves:
        lines.append(f"Saving Throws: {', '.join(proficient_saves)}.")
    skills = record.get("skill_bonuses") or {}
    if skills:
        lines.append(
            "Skills: "
            + ", ".join(
                f"{name.replace('_', ' ').title()} {int(bonus):+d}"
                for name, bonus in sorted(skills.items())
            )
            + "."
        )
    if record.get("languages"):
        spoken = str(record["languages"].get("as_string", "")).rstrip(".")
        if spoken:
            lines.append(f"Languages: {spoken}.")
    if record.get("challenge_rating") is not None:
        lines.append(f"Challenge Rating {record['challenge_rating']}.")
    return " ".join(lines)


def monster_sense_and_defense_abilities(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The statblock's senses, damage defenses, and condition immunities as passive rules."""
    abilities: list[dict[str, Any]] = []
    senses = [
        (label, record.get(key))
        for label, key in (
            ("Darkvision", "darkvision_range"),
            ("Blindsight", "blindsight_range"),
            ("Tremorsense", "tremorsense_range"),
            ("Truesight", "truesight_range"),
        )
        if record.get(key)
    ]
    passive = record.get("passive_perception")
    if senses or passive:
        text = ", ".join(f"{label} {int(value)} ft." for label, value in senses)
        if passive:
            text = f"{text}, " if text else ""
            text += f"Passive Perception {int(passive)}"
        abilities.append({"kind": "passive", "name": "Senses", "description": text})
    defenses = record.get("resistances_and_immunities") or {}
    for key, label in (
        ("damage_resistances_display", "Damage Resistances"),
        ("damage_immunities_display", "Damage Immunities"),
        ("damage_vulnerabilities_display", "Damage Vulnerabilities"),
        ("condition_immunities_display", "Condition Immunities"),
    ):
        text = str(defenses.get(key) or "").strip()
        if text:
            abilities.append({"kind": "passive", "name": label, "description": text})
    return abilities


def monster_abilities(record: dict[str, Any]) -> list[dict[str, Any]]:
    abilities = monster_sense_and_defense_abilities(record)
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
        text = action.get("desc", "") or action.get("name", "")
        usage = monster_usage(action)
        if usage:
            text += f" ({usage})"
        abilities.append(
            {
                "kind": "action",
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
        saves = record.get("saving_throws") or {}
        for ident, name in ABILITY_NAMES.items():
            bonus = saves.get(name.casefold())
            if bonus is None:
                bonus = (int(record["ability_scores"].get(name.casefold()) or 10) - 10) // 2
            # Keep every statblock score non-negative; the rules module reads the offset.
            stats[f"save_{ident}"] = max(0, int(bonus) + 11)
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
            "skills": dict(record.get("skill_bonuses") or {}),
            "challenge_rating": float(record.get("challenge_rating") or 0),
            "experience_points": int(record.get("experience_points") or 0),
            "initiative_bonus": int(record.get("initiative_bonus") or 0),
            "abilities": abilities,
        }
        definitions.append(definition)
    write("monsters.json", definitions)


DND_RULES = [
    (
        "d20-tests",
        (
            "The d20 Test. Whenever a creature tries something that could fail, roll a d20, add "
            "the relevant ability modifier, and add the proficiency bonus when the creature is "
            "proficient. Compare the total to a Difficulty Class (DC): the check succeeds when the "
            "total equals or exceeds the DC. A natural 20 is not an automatic success outside an "
            "attack roll or death save, and a natural 1 is not an automatic failure."
        ),
        True,
    ),
    (
        "advantage",
        (
            "Advantage and Disadvantage. When a rule gives advantage, roll two d20s and use the "
            "higher; when it gives disadvantage, roll two and use the lower. Advantage and "
            "disadvantage cancel each other, however many of each apply. Pass advantage or "
            "disadvantage to the roll as the roll_mode argument."
        ),
        True,
    ),
    (
        "proficiency",
        (
            "Proficiency Bonus. A character's proficiency bonus is +2 at levels 1-4, +3 at 5-8, +4 "
            "at 9-12, +5 at 13-16, and +6 at 17-20. It is added to attack rolls, ability checks, "
            "and saving throws the character is proficient in. Expertise doubles it for a chosen "
            "skill."
        ),
        True,
    ),
    (
        "armor-class",
        (
            "Armor Class. An attack hits when the attack roll total equals or exceeds the target's "
            "Armor Class. Unarmored AC is 10 + Dexterity modifier. Body armor sets a base AC and "
            "may cap the Dexterity added; a shield adds +2."
        ),
        True,
    ),
    (
        "death-saves",
        (
            "Death Saving Throws. A player character at 0 hit points is unconscious and makes a "
            "death save at the start of each of their turns: 10 or higher is a success, 9 or lower "
            "a failure, a natural 1 counts as two failures, and a natural 20 restores 1 hit point. "
            "Three successes stabilize the character; three failures kill them. Use the death_save "
            "tool, never a normal ability save."
        ),
        True,
    ),
    (
        "spellcasting",
        (
            "Spellcasting. A spellcaster knows cantrips and leveled spells. Casting a leveled spell "
            "spends a slot of its level or higher; a cantrip costs no slot. Use cast_spell with the "
            "slot level when upcasting. A spell that requires concentration ends an earlier "
            "concentration spell. Long rests restore all slots."
        ),
        True,
    ),
    (
        "rests",
        (
            "Rests. A short rest takes 1 hour; a character may spend Hit Point Dice to regain hit "
            "points. A long rest takes 8 hours and restores all hit points, Hit Point Dice, and "
            "spell slots. Use the rest tool with length short or long, then spend_hit_die for each "
            "die a character spends."
        ),
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
        definitions.append({"type": "rule", "id": ident, "text": text, "important": important})
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
                "text": "D&D combat: an attack hits when d20 plus the attack bonus and ability modifier equals or exceeds the target's Armor Class. Use player_attack for a player character and attack for an NPC; the engine rolls the d20, adds the proficiency bonus and the weapon's ability modifier, and rolls the weapon's damage. A natural 20 is a critical hit: the weapon dice are rolled twice and the flat modifier is added once. A weapon's reach is 5 feet unless it has Reach (10 feet); a ranged weapon has a normal and a long band, and a shot beyond long range is refused. Pass roll_mode for advantage or disadvantage. On a hit, subtract the damage from hit points. At 0 hit points a player character falls unconscious and begins death saves; an NPC falls. A nonlethal strike leaves the target unconscious instead of dead. Extra Attack raises the engine's attack-action budget; Action Surge can be narrated as an additional action when the resource is spent.",
            },
            {
                "type": "prompt",
                "id": "spellcasting",
                "step": "state",
                "text": "Spellcasters prepare leveled spells. After a long rest, call prepare_spells with the player's chosen list: a wizard chooses from the spellbook, a cleric, druid, paladin, or ranger chooses from the class list, and a bard, sorcerer, or warlock chooses from the spells they know. The Prepared Spells number for the class and level is the limit; cantrips are always available and do not count. Use cast_spell only for a spell that is known and, when leveled, prepared. A cantrip costs no slot; a leveled spell spends a slot of its level or higher. Pass slot_level when the player upcasts; the spell's higher-level text applies. A prepared spell with the Ritual tag can be cast with ritual true: it takes 10 minutes and spends no slot; a wizard's Ritual Adept can cast an unprepared ritual from the spellbook. A concentration spell sets concentration and ends the earlier concentration spell; a long rest clears it, and end_concentration ends it when the player chooses. The result gives the spell save DC and spell attack bonus. For a spell attack call spell_attack; then apply the spell's damage with harm_character and its conditions with add_condition. Use roll_save or player_save for saving throws. A spellcaster learns new spells when the level-up page offers them, and a feature such as Magic Initiate may teach one with learn_spell. Never let a character cast a leveled spell they have not prepared. A Circle of the Land druid calls choose_land after each long rest; the chosen land's Circle spells become always prepared.",
            },
            {
                "type": "prompt",
                "id": "rests",
                "step": "state",
                "text": "A short rest takes 1 hour: call rest with length short, then spend_hit_die once for each Hit Point Die a character spends. A long rest takes 8 hours and restores all hit points, Hit Point Dice, class resource uses, and spell slots; call rest with length long. The engine refills slots, Hit Point Dice, and class resources for you. After a long rest, call prepare_spells for each spellcaster so the player can change their prepared list. Do not add advance_time for the same period, and do not restore hit points by narration alone.",
            },
            {
                "type": "prompt",
                "id": "death_and_dying",
                "step": "state",
                "text": "A player character at 0 hit points is unconscious and dying. At the start of each of their turns call death_save so the player rolls: 10 or more succeeds, 9 or less fails, a natural 1 is two failures, and a natural 20 restores 1 hit point and ends the dying state. Three successes stabilize the character; three failures kill them. A stabilized character remains unconscious until healing raises them above 0 hit points; healing then clears the defeated state automatically. The stabilize tool or a DC 10 Wisdom (Medicine) check can also stabilize a character at 0 hit points. An NPC at 0 hit points dies unless it is important, in which case it is unconscious.",
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
                "text": "Armor sets a base Armor Class and caps the Dexterity modifier it allows: light armor lets the full Dexterity through, medium armor caps it at 2, and heavy armor allows none. A shield adds 2. Equip armor and shields with equip_armor and ready other gear with ready_item so the engine records the change; never adjust Armor Class by narration alone. Buying and selling uses buy_item and the marketplace's silver prices.",
            },
            {
                "type": "prompt",
                "id": "level_up_choices",
                "step": "state",
                "text": "Award experience with award_xp for a defeated encounter, a completed quest, or a milestone. When a character has enough experience, the player takes the level on the advancement page. Hit points, the proficiency bonus, spell slots, and class features arrive automatically; the page then offers the level's spells, Ability Score Improvement or feat, fighting style, subclass, and expertise. Never apply a level or choose a player's feat, spell, or subclass for them; the player makes those choices.",
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
                "text": "The player rolls their own character's attacks, saves, checks, and spell attacks. Call player_attack for a weapon attack, spell_attack for a spell attack, player_save for a save, and roll_check for an ability check, so the player presses the roll button; never roll their dice yourself. The player rolls only the d20; the engine adds the ability modifier, proficiency bonus, and any situational modifier. For an NPC, use attack and roll_save, which roll the dice themselves.",
            },
            {
                "type": "prompt",
                "id": "feature_text",
                "text": "Before you resolve a spell, class feature, species trait, or feat, read it with lookup_feature. The statblock names the character's features and prints their short notes; the full text is the rule. Never invent an effect a feature does not state, and never add a bonus the feature does not give.",
            },
            {
                "type": "prompt",
                "id": "class_resources",
                "text": "Class and species resources are tracked as counters: Rage, Bardic Inspiration, Channel Divinity, Wild Shape, Second Wind, Action Surge, Indomitable, Focus Points, the Lay On Hands pool, Sorcery Points, Arcane Recovery, Favored Enemy, Tireless, Mystic Arcanum, Dark One's Luck, and Giant Ancestry. When a character uses a feature that spends one, call use_feature with the feature name; it spends the resource and the feature's Action, Bonus Action, or Reaction automatically. Use use_class_resource only for a pool amount with no single feature, such as a Lay On Hands point total or sorcery points. Never spend a use by narration alone. A Reaction that changes a hit or a damage roll, such as Uncanny Dodge, Stone's Endurance, or Cutting Words, is spent with use_feature before the attack or hazard is resolved, so it is recorded even when the blow would end the encounter; roll its die with roll_dice and apply the adjusted damage with harm_character or heal_character. Roll Bardic Inspiration, a monster recharge, and any other die the rules call for with roll_dice instead of inventing it. A short rest returns one use of Rage, Channel Divinity, Wild Shape, and Second Wind and all Action Surge and Focus Points; a long rest refills every pool and Giant Ancestry. The character sheet shows what is left.",
            },
            {
                "type": "prompt",
                "id": "attunement",
                "text": "A magic item whose description says it requires attunement works only for a character attuned to it. Attunement takes a short rest and a character can be attuned to at most three items; call attune_item when a player attunes and unattune_item when they end it. The spellcasting prompt already covers concentration, which is separate from attunement.",
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
                "character_param": "character",
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
                "character_param": "name",
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
                    "name": {"type": "string", "description": "The dying player character."},
                },
                "required": ["prompt", "name"],
            },
            "player_input": {
                "param": "roll",
                "dice": "1d20",
                "details": "Roll a death save: 10 or more succeeds.",
                "character_param": "name",
            },
        },
        {
            "type": "tool_call",
            "id": "spend_hit_die",
            "python_module": "dnd_srd_2024",
            "function": "spend_hit_die",
            "description": "Spend one of a character's Hit Point Dice on a short rest to regain its roll plus the Constitution modifier. Call it once per die spent, after the rest. Pass the die type when the character has Hit Point Dice of more than one kind.",
            "parameters": {
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The character spending a Hit Point Die.",
                    },
                    "die": {
                        "type": "string",
                        "description": "The die type to spend, such as d8 or d10, for a multiclass character with more than one kind. Leave it out to spend a die of the character's first class.",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "type": "tool_call",
            "id": "cast_spell",
            "python_module": "dnd_srd_2024",
            "function": "cast_spell",
            "description": "Cast a prepared spell, spending a slot of its level or higher. Cantrips cost no slot and need no preparation. Pass slot_level when upcasting. Pass ritual true for a prepared spell with the Ritual tag: it takes 10 minutes and spends no slot. The result gives the spell save DC, spell attack bonus, concentration, and the spell's text for its effect.",
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
                    "ritual": {
                        "type": "boolean",
                        "default": False,
                        "description": "Cast the ritual version: 10 minutes, no slot, no upcasting. The spell must have the Ritual tag.",
                    },
                },
                "required": ["name", "spell"],
            },
        },
        {
            "type": "tool_call",
            "id": "prepare_spells",
            "python_module": "dnd_srd_2024",
            "function": "prepare_spells",
            "description": "Prepare the leveled spells a caster can cast today after a long rest. A wizard prepares from the spellbook; a cleric, druid, paladin, or ranger prepares from the class list; a bard, sorcerer, or warlock prepares from the spells they know. A multiclass caster names the class whose list this call prepares. Cantrips are always available and take no slot in the list. The number prepared is the Prepared Spells number for the class and level.",
            "parameters": {
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "The caster preparing spells.",
                    },
                    "class_name": {
                        "type": "string",
                        "description": "The class whose list is prepared, such as wizard, for a multiclass caster. Leave it out for a single-class character.",
                    },
                    "spells": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": 25,
                        "items": {"type": "string"},
                        "description": "The leveled spell names or ids to prepare; the list replaces the old one.",
                    },
                },
                "required": ["character", "spells"],
            },
        },
        {
            "type": "tool_call",
            "id": "choose_land",
            "python_module": "dnd_srd_2024",
            "function": "choose_land",
            "description": "Choose the land for a Circle of the Land druid after a long rest. The land's Circle spells for the druid's level become always prepared. The lands are arid, polar, temperate, and tropical.",
            "parameters": {
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "The Circle of the Land druid.",
                    },
                    "land": {
                        "type": "string",
                        "description": "The land: arid, polar, temperate, or tropical.",
                    },
                },
                "required": ["character", "land"],
            },
        },
        {
            "type": "tool_call",
            "id": "attune_item",
            "python_module": "dnd_srd_2024",
            "function": "attune_item",
            "description": "Attune to a carried magic item that requires attunement, during a short rest. A character can be attuned to at most three items.",
            "parameters": {
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "The character attuning.",
                    },
                    "item": {
                        "type": "string",
                        "description": "The carried magic item's name or id.",
                    },
                },
                "required": ["character", "item"],
            },
        },
        {
            "type": "tool_call",
            "id": "unattune_item",
            "python_module": "dnd_srd_2024",
            "function": "unattune_item",
            "description": "End an attunement to a magic item.",
            "parameters": {
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "The character.",
                    },
                    "item": {
                        "type": "string",
                        "description": "The magic item's name or id.",
                    },
                },
                "required": ["character", "item"],
            },
        },
        {
            "type": "tool_call",
            "id": "use_class_resource",
            "python_module": "dnd_srd_2024",
            "function": "use_class_resource",
            "description": "Spend a use of a class resource such as Rage, Bardic Inspiration, Channel Divinity, Wild Shape, Second Wind, Action Surge, Indomitable, Focus Points, the Lay On Hands pool, Sorcery Points, Arcane Recovery, Favored Enemy, Tireless, Mystic Arcanum, or Dark One's Luck. The engine records the spending and returns what is left; rests refill the appropriate pools.",
            "parameters": {
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "The character spending the resource.",
                    },
                    "resource": {
                        "type": "string",
                        "description": "The resource counter id, such as rage or sorcery_points.",
                    },
                    "amount": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 1,
                        "description": "How many uses or pool points to spend.",
                    },
                    "cause": {
                        "type": "string",
                        "description": "What the resource is spent on.",
                    },
                },
                "required": ["character", "resource"],
            },
        },
        {
            "type": "tool_call",
            "id": "use_feature",
            "python_module": "dnd_srd_2024",
            "function": "use_feature",
            "description": "Use a class or species feature such as Rage, Second Wind, Channel Divinity, Wild Shape, Bardic Inspiration, Lay On Hands, Flurry of Blows, or a Goliath ancestry boon. This spends the feature's resource and its Action, Bonus Action, or Reaction automatically.",
            "parameters": {
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "The character using the feature.",
                    },
                    "feature": {
                        "type": "string",
                        "description": "The feature's name or id, such as Rage or class-barbarian-rage.",
                    },
                    "cause": {
                        "type": "string",
                        "description": "What the feature is being used for.",
                    },
                },
                "required": ["character", "feature"],
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
                    "name": {
                        "type": "string",
                        "description": "The character learning the feature.",
                    },
                    "feature": {"type": "string", "description": "The feature's name or id."},
                    "cause": {"type": "string", "description": "Why the character learns it."},
                },
                "required": ["name", "feature"],
            },
        },
        {
            "type": "tool_call",
            "id": "roll_initiative",
            "python_module": "dnd_srd_2024",
            "function": "roll_initiative",
            "description": "Roll initiative for every combatant once all participants have joined: 1d20 plus Dexterity, highest first. Call it before the first turn; the engine records the order and the rolls.",
        },
        {
            "type": "tool_call",
            "id": "award_xp",
            "python_module": "dnd_srd_2024",
            "function": "award_xp",
            "description": "Award experience points for a defeated encounter, a completed quest, or a milestone. The player takes the level on the advancement page when the total reaches the next threshold.",
            "parameters": {
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "The player character gaining experience.",
                    },
                    "amount": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000000,
                        "description": "How many experience points to award.",
                    },
                    "cause": {
                        "type": "string",
                        "description": "What earned the experience, in a few words.",
                    },
                },
                "required": ["character", "amount"],
            },
        },
        {
            "type": "tool_call",
            "id": "spell_attack",
            "python_module": "dnd_srd_2024",
            "function": "spell_attack",
            "description": "Ask the player to make a spell attack against a target's Armor Class: d20 plus proficiency bonus and spellcasting ability. Use it for Fire Bolt, Guiding Bolt, Eldritch Blast, and any spell that says spell attack. Apply the spell's damage only after a hit.",
            "parameters": {
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the GM says to invite the spell attack.",
                    },
                    "name": {"type": "string", "description": "The caster."},
                    "spell": {"type": "string", "description": "The spell's name or id."},
                    "target": {"type": "string", "description": "The creature being attacked."},
                    "roll_mode": {
                        "type": "string",
                        "enum": ["normal", "advantage", "disadvantage"],
                        "default": "normal",
                        "description": "Advantage or disadvantage on the attack roll.",
                    },
                },
                "required": ["prompt", "name", "spell", "target"],
            },
            "player_input": {
                "param": "roll",
                "dice": "1d20",
                "details": "The engine adds your spell attack bonus.",
                "character_param": "name",
            },
        },
        {
            "type": "tool_call",
            "id": "resolve_hazard",
            "python_module": "dnd_srd_2024",
            "function": "resolve_hazard",
            "description": "Resolve a D&D trap, breath weapon, fall, or area effect against NPCs: each rolls d20 plus its saving-throw bonus against the DC. Player characters roll their own save, so name only NPCs here. On a failure apply damage and the named condition; half_on_save halves damage on a success.",
            "parameters": {
                "properties": {
                    "hazard": {"type": "string", "description": "What the hazard is."},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The NPCs caught in the effect.",
                    },
                    "save": {
                        "type": "string",
                        "enum": ["str", "dex", "con", "int", "wis", "cha"],
                        "description": "The ability save the effect calls for.",
                    },
                    "dc": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 40,
                        "description": "The saving-throw DC.",
                    },
                    "damage": {
                        "type": "string",
                        "description": "Damage dice such as 4d6, or a fixed number.",
                    },
                    "half_on_save": {
                        "type": "boolean",
                        "default": False,
                        "description": "True when a successful save halves the damage.",
                    },
                    "condition": {
                        "type": "string",
                        "description": "A condition a failed save applies.",
                    },
                    "stat": {
                        "type": "string",
                        "description": "Leave it out; D&D hazards take hit points.",
                    },
                    "cause": {"type": "string", "description": "What caused the hazard."},
                    "save_modifier": {
                        "type": "integer",
                        "minimum": -20,
                        "maximum": 20,
                        "default": 0,
                        "description": "A situational bonus or penalty to the save.",
                    },
                },
                "required": ["hazard", "targets"],
            },
        },
        {
            "type": "tool_call",
            "id": "end_concentration",
            "python_module": "dnd_srd_2024",
            "function": "end_concentration",
            "description": "End the concentration spell a character is maintaining, such as when the player stops concentrating or fails a Constitution save against damage.",
            "parameters": {
                "properties": {
                    "name": {"type": "string", "description": "The concentrating character."},
                },
                "required": ["name"],
            },
        },
        {
            "type": "tool_call",
            "id": "ready_item",
            "description": "Ready a carried item for use, or stow it. A readied weapon can attack. Readying armor or a shield does not equip it; use equip_armor to wear or wield it.",
            "parameters": {
                "properties": {
                    "name": {"type": "string", "description": "The character carrying the item."},
                    "item": {"type": "string", "description": "The carried item's name."},
                    "ready": {
                        "type": "boolean",
                        "default": True,
                        "description": "True to ready it, false to stow it.",
                    },
                },
                "required": ["name", "item"],
            },
        },
        {
            "type": "tool_call",
            "id": "equip_armor",
            "description": "Wear or remove a carried, readied suit of armor, or wield or stop wielding a carried, readied shield. Only one suit and one shield can be equipped; equipping another replaces that slot. Armor Class updates automatically.",
            "parameters": {
                "properties": {
                    "name": {"type": "string", "description": "The character carrying the item."},
                    "item": {
                        "type": "string",
                        "description": "The carried armor or shield's catalogue name.",
                    },
                    "equipped": {
                        "type": "boolean",
                        "default": True,
                        "description": "True to wear or wield it, false to remove it.",
                    },
                },
                "required": ["name", "item"],
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
