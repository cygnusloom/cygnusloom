"""Audit Worlds Without Number content against the CC0 SRD.

The SRD grants the game's mechanics and trivially-derived material. It does not
grant the setting, the individual creatures, or the GM tool text of the free
book. This script answers one question per string in the ruleset data: does it
come from the SRD, from the free book alone, or from neither?

Usage:
    uv run python scripts/audit_srd.py --srd /tmp/srd.txt --free /tmp/free.txt

Extract the text first, e.g.:

    uv run --with pypdf python -c "from pypdf import PdfReader; \
        print('\\n'.join(p.extract_text() or '' for p in PdfReader('srd.pdf').pages))" > /tmp/srd.txt

The report names content ids rather than printing book text, so it is safe to
commit and share.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

DATA = pathlib.Path("src/cygnusforge/data/rulesets/worldswithoutnumber")

# Strings shorter than this are too generic to attribute to a source. A name
# like "Guard" appears in both books and proves nothing.
MIN_PROBE = 24


def normalize(text: str) -> str:
    """Collapse whitespace and undo PDF extraction damage so line wraps do not hide matches.

    The extractor breaks long words across lines as "transfor - mation", and it splits
    some numbers as "1 3". Both are treated as the wrapped word or number, so a phrase
    that the source really contains is not reported as missing.
    """
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?<=[a-z]) ?- (?=[a-z])", "", text)
    text = re.sub(r"(?<=\d) (?=\d)", "", text)
    return text.lower()


def collect(node, path: str = ""):
    """Yield (path, string) for every prose-ish string in a loaded JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from collect(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from collect(value, f"{path}[{index}]")
    elif isinstance(node, str) and len(node) >= MIN_PROBE:
        yield path, node


def classify(probe: str, srd: str, free: str) -> str:
    normalized = normalize(probe)
    in_srd = normalized in srd
    in_free = normalized in free
    if in_srd and in_free:
        return "both"
    if in_srd:
        return "srd"
    if in_free:
        return "free"
    return "neither"


def audit(srd_path: pathlib.Path, free_path: pathlib.Path) -> int:
    srd = normalize(srd_path.read_text())
    free = normalize(free_path.read_text())

    totals = {"srd": 0, "both": 0, "free": 0, "neither": 0}
    copied: list[str] = []

    for data_file in sorted(DATA.glob("*.json")):
        document = json.loads(data_file.read_text())
        counts = {"srd": 0, "both": 0, "free": 0, "neither": 0}
        for path, probe in collect(document):
            verdict = classify(probe, srd, free)
            counts[verdict] += 1
            totals[verdict] += 1
            if verdict == "free":
                copied.append(f"{data_file.name}:{path}")
        print(
            f"{data_file.name:<24} srd={counts['srd']:<4} both={counts['both']:<4} "
            f"free-only={counts['free']:<4} unverified={counts['neither']:<4}"
        )

    print("\ntotals:", json.dumps(totals))

    if copied:
        print(f"\nverbatim free-book text found in {len(copied)} place(s):")
        for location in copied[:200]:
            print("  ", location)
        return 1

    print("\nno verbatim free-book text found")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--srd", type=pathlib.Path, required=True)
    parser.add_argument("--free", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not DATA.is_dir():
        print(f"no ruleset data at {DATA}", file=sys.stderr)
        return 2
    return audit(args.srd, args.free)


if __name__ == "__main__":
    raise SystemExit(main())
