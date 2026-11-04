"""Forge the Worlds Without Number bestiary from original creature pools.

The creatures in the free book are the author's own inventions and are not part
of the CC0 SRD, so the rule set needs a bestiary of its own. Authors write each
creature's shape and prose in `scripts/forge_pools/bestiary_*.json`; this script
fills in the mechanical stat block for the creature's Hit Dice from
`monster_stats.json`, which is derived from the stat lines the loader already
accepts.

A pool entry carries:

    id                  snake_case id
    name                display name
    hd                  one of the Hit Dice in monster_stats.json
    description         prose ending in the stat line, e.g.
                        "... (HD 3; MV 30 ft; Inst 5; Skill +1; 1 attack per action.)"
    armor               ascending AC
    attacks             {"name": {"dice": "1d8", "shock": 2, "shock_ac": 13}}
    attacks_per_action  whole number, and it must match the stat line
    large_group         boolean
    rules               list of rule ids, normally empty
    abilities           list of structured abilities, normally empty

Usage:
    uv run python scripts/forge_bestiary.py --check
    uv run python scripts/forge_bestiary.py --write
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

RULE_SET = pathlib.Path("src/cygnusforge/data/rulesets/worldswithoutnumber")
POOLS = pathlib.Path("scripts/forge_pools")
STATS = POOLS / "monster_stats.json"
TARGET = RULE_SET / "monsters.json"
KEYS = {
    "id",
    "name",
    "hd",
    "description",
    "armor",
    "attacks",
    "attacks_per_action",
    "large_group",
    "rules",
    "abilities",
}
ABILITY_KEYS = {
    "kind",
    "name",
    "trigger",
    "save",
    "condition",
    "damage",
    "description",
}
ABILITY_KINDS = {"save_or_condition", "action", "immunity", "passive"}
STAT_LINE = re.compile(r"HD (\d+);")
ATTACK_PHRASE = re.compile(r"(\d+)\s+attacks?\s+per\s+action", re.IGNORECASE)


def load_pool() -> list[dict]:
    creatures: list[dict] = []
    for path in sorted(POOLS.glob("bestiary_*.json")):
        document = json.loads(path.read_text())
        if not isinstance(document, list):
            raise SystemExit(f"{path}: a bestiary pool must be a list of creatures")
        creatures.extend(document)
    return creatures


def problem(creature: dict, stats: dict[str, dict]) -> str | None:
    unknown = set(creature) - KEYS
    if unknown:
        return f"unknown keys {sorted(unknown)}"
    missing = KEYS - set(creature) - {"abilities"}
    if missing:
        return f"missing keys {sorted(missing)}"
    if str(creature["hd"]) not in stats:
        return f"hd {creature['hd']} has no stat block; use one of {sorted(stats, key=int)}"
    description = creature["description"]
    match = STAT_LINE.search(description)
    if match and int(match.group(1)) != creature["hd"]:
        return f"description says HD {match.group(1)} but hd is {creature['hd']}"
    if not match:
        return "description has no '(HD N; ...)' stat line"
    phrase = ATTACK_PHRASE.search(description)
    if phrase and int(phrase.group(1)) != creature["attacks_per_action"]:
        return (
            f"description says {phrase.group(1)} attacks per action but "
            f"attacks_per_action is {creature['attacks_per_action']}"
        )
    if not creature["attacks"]:
        return "has no attacks"
    for ability in creature.get("abilities", []):
        extra = set(ability) - ABILITY_KEYS
        if extra:
            return f"unknown ability keys {sorted(extra)}"
        if ability.get("kind") not in ABILITY_KINDS:
            return f"ability kind {ability.get('kind')!r} is not one of {sorted(ABILITY_KINDS)}"
        if not ability.get("name") or not ability.get("description"):
            return "an ability needs a name and a description"
    return None


def check(creatures: list[dict], stats: dict[str, dict]) -> int:
    problems = 0
    seen: set[str] = set()
    for creature in creatures:
        creature_id = creature.get("id", "<no id>")
        if creature_id in seen:
            print(f"duplicate id: {creature_id}")
            problems += 1
        seen.add(creature_id)
        found = problem(creature, stats)
        if found:
            print(f"{creature_id}: {found}")
            problems += 1
    if not creatures:
        print("no creatures found")
        problems += 1
    if not problems:
        print(f"bestiary pool is sound: {len(creatures)} creatures")
    return problems


def write(creatures: list[dict], stats: dict[str, dict]) -> None:
    monsters = []
    for creature in creatures:
        monster = {
            "type": "monster",
            "id": creature["id"],
            "name": creature["name"],
            "description": creature["description"],
            "stats": stats[str(creature["hd"])],
            "armor": creature["armor"],
            "attacks": creature["attacks"],
            "attacks_per_action": creature["attacks_per_action"],
            "large_group": creature["large_group"],
            "rules": creature["rules"],
            "abilities": creature.get("abilities", []),
        }
        monsters.append(monster)
    TARGET.write_text(json.dumps(monsters, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {TARGET} ({len(monsters)} monsters)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.write and not args.check:
        parser.error("choose --check or --write")
    stats = json.loads(STATS.read_text())
    creatures = load_pool()
    problems = check(creatures, stats)
    if args.write:
        if problems:
            print("refusing to write while the bestiary has problems", file=sys.stderr)
            return 1
        write(creatures, stats)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
