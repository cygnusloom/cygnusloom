"""Forge the background Growth and Learning tables.

The CC0 SRD grants two example backgrounds in full, Artisan and Barbarian, and
leaves the rest to the designer. The rule set keeps twenty backgrounds so the
creation form and the existing characters still work, but the eighteen beyond
the SRD's examples get tables generated here from original skill pools.

Each background's Learning table offers its free skill plus related skills drawn
from the pool, and its Growth table offers the standard attribute growths plus
two picks seeded by the background's own work. Drawing is seeded per background,
so the tables are stable but were not written in a fixed order.

Usage:
    uv run python scripts/forge_characters.py --check
    uv run python scripts/forge_characters.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random

RULE_SET = pathlib.Path("src/cygnusforge/data/rulesets/worldswithoutnumber")
TABLES = RULE_SET / "tables.json"
BACKGROUNDS = RULE_SET / "backgrounds.json"
SEED = "cygnusforge-backgrounds-v1"
# The SRD grants these two in full, so their tables are left exactly as printed.
SRD_BACKGROUNDS = {"artisan", "barbarian"}
LEARNING_SIZE = 8
GROWTH_BASE = ("+1 Any Stat", "+2 Physical", "+2 Mental")

# The skills each background could plausibly have picked up, beyond its free
# skill. Written for this rule set rather than taken from any book.
POOLS: dict[str, tuple[str, ...]] = {
    "carter": (
        "ride",
        "connect",
        "trade",
        "notice",
        "survive",
        "exert",
        "administer",
        "craft",
        "sneak",
    ),
    "courtesan": (
        "perform",
        "convince",
        "connect",
        "notice",
        "know",
        "sneak",
        "administer",
        "lead",
    ),
    "criminal": (
        "sneak",
        "connect",
        "convince",
        "notice",
        "exert",
        "administer",
        "trade",
        "punch",
    ),
    "hunter": (
        "shoot",
        "survive",
        "sneak",
        "notice",
        "exert",
        "craft",
        "ride",
        "stab",
    ),
    "laborer": (
        "work",
        "exert",
        "craft",
        "connect",
        "notice",
        "administer",
        "survive",
        "trade",
    ),
    "merchant": (
        "trade",
        "administer",
        "connect",
        "convince",
        "know",
        "sail",
        "ride",
        "notice",
    ),
    "noble": (
        "lead",
        "convince",
        "administer",
        "know",
        "ride",
        "connect",
        "perform",
        "notice",
    ),
    "nomad": (
        "ride",
        "survive",
        "notice",
        "shoot",
        "exert",
        "connect",
        "craft",
        "sneak",
    ),
    "peasant": (
        "exert",
        "survive",
        "work",
        "craft",
        "notice",
        "connect",
        "sneak",
        "ride",
    ),
    "performer": (
        "perform",
        "convince",
        "connect",
        "notice",
        "sneak",
        "know",
        "lead",
        "administer",
    ),
    "physician": (
        "heal",
        "know",
        "notice",
        "craft",
        "connect",
        "administer",
        "convince",
        "survive",
    ),
    "priest": (
        "pray",
        "know",
        "convince",
        "connect",
        "administer",
        "lead",
        "heal",
        "notice",
    ),
    "sailor": (
        "sail",
        "exert",
        "notice",
        "survive",
        "craft",
        "connect",
        "trade",
        "punch",
    ),
    "scholar": (
        "know",
        "administer",
        "notice",
        "craft",
        "convince",
        "heal",
        "pray",
        "connect",
    ),
    "slave": (
        "sneak",
        "exert",
        "survive",
        "notice",
        "work",
        "connect",
        "craft",
        "perform",
    ),
    "soldier": (
        "stab",
        "shoot",
        "punch",
        "exert",
        "notice",
        "survive",
        "lead",
        "administer",
    ),
    "thug": (
        "punch",
        "exert",
        "sneak",
        "connect",
        "convince",
        "notice",
        "stab",
        "survive",
    ),
    "wanderer": (
        "survive",
        "notice",
        "connect",
        "sneak",
        "ride",
        "exert",
        "craft",
        "trade",
    ),
}


def seeded(name: str) -> random.Random:
    digest = hashlib.sha256(f"{SEED}:{name}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def learning(free: str, pool: tuple[str, ...], rng: random.Random) -> list[str]:
    """The free skill, then a random spread of related skills, in a random order."""
    candidates = [skill for skill in pool if skill != free]
    chosen = [free] + rng.sample(candidates, LEARNING_SIZE - 1)
    rng.shuffle(chosen)
    return chosen


def growth(free: str, picks: list[str], rng: random.Random) -> list[str]:
    """Attribute growth plus two skill picks seeded by the background's own work."""
    extras = rng.sample([free, *picks, "Any Skill"], 2)
    options = [*GROWTH_BASE, *dict.fromkeys(extras)]
    rng.shuffle(options)
    return options


def build(tables: list[dict], backgrounds: list[dict]) -> None:
    by_id = {table["id"]: table for table in tables}
    index = by_id["background_index"]
    descriptions = []
    for background in backgrounds:
        background_id = background["id"]
        name = background["name"]
        free = next(iter(background["skills"]))
        if background_id in SRD_BACKGROUNDS:
            options = by_id[f"learning_{background_id}"]["options"]
            growth_options = by_id[f"growth_{background_id}"]["options"]
        else:
            rng = seeded(background_id)
            options = learning(free, POOLS[background_id], rng)
            growth_options = growth(free, options[:3], rng)
            by_id[f"learning_{background_id}"]["options"] = options
            by_id[f"growth_{background_id}"]["options"] = growth_options
        quick = ", ".join(skill for skill in options if skill != free)
        descriptions.append(
            {
                "name": name,
                "description": f"Free skill: {free}. Quick skills: {quick}.",
            }
        )
    index["options"] = descriptions


def check(tables: list[dict], backgrounds: list[dict]) -> int:
    by_id = {table["id"]: table for table in tables}
    problems = 0
    for background in backgrounds:
        for prefix in ("growth", "learning"):
            table = by_id.get(f"{prefix}_{background['id']}")
            if table is None:
                print(f"missing table {prefix}_{background['id']}")
                problems += 1
                continue
            if len(set(table["options"])) != len(table["options"]):
                print(f"duplicate options in {prefix}_{background['id']}")
                problems += 1
            if not table["options"]:
                print(f"empty table {prefix}_{background['id']}")
                problems += 1
    if problems == 0:
        print(f"background tables are sound: {len(backgrounds)} backgrounds")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.write and not args.check:
        parser.error("choose --check or --write")
    tables = json.loads(TABLES.read_text())
    backgrounds = json.loads(BACKGROUNDS.read_text())
    if args.check:
        return 1 if check(tables, backgrounds) else 0
    build(tables, backgrounds)
    TABLES.write_text(json.dumps(tables, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {TABLES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
