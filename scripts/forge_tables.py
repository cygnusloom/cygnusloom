"""Forge the Worlds Without Number tables from original content pools.

Every table in `dice_tables.json` and `worldgen_tables.json` is keyed by id. A
pool file supplies more candidate rows than the table has rolls, and this script
draws a random subset for each table and shuffles the order. That way the rows
that reach the book are genuinely a random sample of the material written for
them, rather than whichever entries happened to be written first.

The tables in the free book are the game author's GM tools and are not part of
the CC0 SRD. The pools in `scripts/forge_pools/` are original text written for
this project, in a plain fantasy register.

Usage:
    uv run python scripts/forge_tables.py --check     # report short or missing pools
    uv run python scripts/forge_tables.py --write     # rewrite the table JSON

Pools are JSON objects mapping a table id to a list of candidate results. A
creative table is instead an object with `name`, `parts`, and `patterns`; the
script composes its entries from the parts so a 1d100 table has 100 distinct
results built from a much larger space of combinations.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import pathlib
import random
import sys

RULE_SET = pathlib.Path("src/cygnusforge/data/rulesets/worldswithoutnumber")
POOLS = pathlib.Path("scripts/forge_pools")
TABLE_FILES = ("dice_tables.json", "worldgen_tables.json")
CREATIVE_POOL = POOLS / "creative.json"
CREATIVE_FILE = RULE_SET / "creative_tables.json"
BESTIARY_STATS = "monster_stats.json"
BESTIARY_PREFIX = "bestiary_"
DEFAULT_SEED = "cygnusforge-tables-v1"
# Share of extra candidates beyond the number of rolls, so the draw has a choice.
EXTRA_SHARE = 0.6
EXTRA_MINIMUM = 6


def wanted(dice: str, rows: int) -> int:
    """How many candidate results a pool must offer for a table.

    A 2d6 reaction table is an ordered scale from hostile to friendly, so its
    five bands are written in order and never sampled or shuffled.
    """
    if dice == "2d6":
        return rows
    return rows + max(EXTRA_MINIMUM, round(rows * EXTRA_SHARE))


def labels(dice: str, rows: int) -> list[str]:
    """The `roll` label of each row, matching the dice formula.

    A 2d6 table has five bands. A 1d100 table is written as fifty two-number
    ranges, which is how the book presents a hundred-entry table in a column.
    Every other table gives one roll per row.
    """
    if dice == "2d6":
        if rows != 5:
            raise ValueError("a 2d6 table needs five bands")
        return ["2", "3-5", "6-8", "9-11", "12"]
    if dice == "1d100" and rows == 50:
        return [f"{2 * index + 1}-{2 * index + 2}" for index in range(50)]
    if rows != int(dice.removeprefix("1d")):
        raise ValueError(f"{dice} cannot fill {rows} rows")
    return [str(index) for index in range(1, rows + 1)]


def seeded(seed: str, table_id: str) -> random.Random:
    """A random source that depends on the seed and the table, so one table's
    pool can grow without reshuffling every other table."""
    digest = hashlib.sha256(f"{seed}:{table_id}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def load_pools() -> dict[str, list[str]]:
    pools: dict[str, list[str]] = {}
    for path in sorted(POOLS.glob("*.json")):
        # The creative spec has its own shape, and the bestiary and stat
        # reference belong to `forge_bestiary.py`.
        if path == CREATIVE_POOL or path.name == BESTIARY_STATS:
            continue
        if path.name.startswith(BESTIARY_PREFIX):
            continue
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            raise SystemExit(f"{path}: a pool file must be an object of table id to entries")
        for table_id, entries in document.items():
            if table_id in pools:
                raise SystemExit(f"{path}: {table_id} is defined twice")
            if not isinstance(entries, list) or not all(
                isinstance(entry, str) and entry.strip() for entry in entries
            ):
                raise SystemExit(f"{path}: {table_id} must be a list of non-empty strings")
            pools[table_id] = entries
    return pools


def read_tables() -> list[tuple[pathlib.Path, list[dict]]]:
    return [(RULE_SET / name, json.loads((RULE_SET / name).read_text())) for name in TABLE_FILES]


def check(pools: dict[str, list[str]]) -> int:
    problems = 0
    for path, tables in read_tables():
        for table in tables:
            entries = pools.get(table["id"], [])
            need = wanted(table["dice"], len(table["rows"]))
            if not entries:
                print(f"missing pool: {table['id']} ({table['dice']}, wants {need})")
                problems += 1
            elif len(entries) < need:
                print(
                    f"short pool: {table['id']} has {len(entries)}, wants {need} "
                    f"({len(table['rows'])} rolls + {need - len(table['rows'])} spare)"
                )
                problems += 1
            duplicates = len(entries) - len(set(entries))
            if duplicates:
                print(f"duplicate entries: {table['id']} repeats {duplicates}")
                problems += 1
    known = {table["id"] for _, tables in read_tables() for table in tables}
    for table_id in sorted(set(pools) - known):
        print(f"unused pool: {table_id} is not a table in the rule set")
        problems += 1
    if not problems:
        print("every pool is long enough")
    return problems


def build_rows(table: dict, entries: list[str], seed: str) -> list[dict[str, str]]:
    rows = len(table["rows"])
    if table["dice"] == "2d6":
        if len(entries) != rows:
            raise SystemExit(
                f"{table['id']}: a 2d6 reaction table needs exactly {rows} ordered bands"
            )
        return [
            {"roll": roll, "result": result} for roll, result in zip(labels("2d6", rows), entries)
        ]
    rng = seeded(seed, table["id"])
    chosen = rng.sample(entries, rows)
    rng.shuffle(chosen)
    return [
        {"roll": roll, "result": result}
        for roll, result in zip(labels(table["dice"], rows), chosen)
    ]


def build_creative(spec: dict, table_id: str, seed: str) -> dict:
    patterns = spec["patterns"]
    parts = spec["parts"]
    names = list(parts)
    combinations = [tuple(pair) for pair in itertools.product(*(parts[name] for name in names))]
    count = spec.get("entries", 100)
    if len(combinations) < count:
        raise SystemExit(
            f"{table_id}: {len(combinations)} combinations cannot fill {count} entries"
        )
    rng = seeded(seed, table_id)
    chosen = rng.sample(combinations, count)
    entries = []
    for values in chosen:
        text = patterns[rng.randrange(len(patterns))].format(**dict(zip(names, values)))
        entries.append(text[:1].upper() + text[1:])
    rng.shuffle(entries)
    return {
        "type": "creative_table",
        "id": table_id,
        "name": spec["name"],
        "source": spec["source"],
        "entries": entries,
    }


def write(pools: dict[str, list[str]], seed: str) -> None:
    for path, tables in read_tables():
        for table in tables:
            table["rows"] = build_rows(table, pools[table["id"]], seed)
        path.write_text(json.dumps(tables, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path} ({len(tables)} tables)")

    creative_spec = json.loads(CREATIVE_POOL.read_text())
    creative = [build_creative(spec, table_id, seed) for table_id, spec in creative_spec.items()]
    CREATIVE_FILE.write_text(json.dumps(creative, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {CREATIVE_FILE} ({len(creative)} tables)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the table files")
    parser.add_argument("--check", action="store_true", help="report pool problems")
    parser.add_argument("--seed", default=DEFAULT_SEED)
    args = parser.parse_args()
    if not args.write and not args.check:
        parser.error("choose --check or --write")
    pools = load_pools()
    problems = check(pools)
    if args.write:
        if problems:
            print("refusing to write while pools are missing or short", file=sys.stderr)
            return 1
        write(pools, args.seed)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
