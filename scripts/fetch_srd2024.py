#!/usr/bin/env python3
"""Cache the Open5e srd-2024 records used to build the D&D rule set.

Run it before `scripts/build_srd2024.py`:

    .venv/bin/python scripts/fetch_srd2024.py

The records are written to /tmp/srd2024_raw, which the build script reads. A cached endpoint is
not fetched again, so the script can resume after a failure.
"""

from __future__ import annotations

import json
import pathlib
import time
import urllib.request

BASE = "https://api.open5e.com/v2"
OUT = pathlib.Path("/tmp/srd2024_raw")
ENDPOINTS = (
    "species",
    "classes",
    "backgrounds",
    "feats",
    "spells",
    "creatures",
    "weapons",
    "armor",
    "items",
    "magicitems",
    "rules",
    "rulesets",
)


def get(url: str) -> dict:
    """Fetch one JSON page, retrying a transient network failure with a growing pause."""
    request = urllib.request.Request(url, headers={"User-Agent": "cygnusloom-srd-build/1.0"})
    last: Exception | None = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except Exception as error:  # noqa: BLE001 - a transient DNS or network failure
            last = error
            print(f"retry {attempt + 1}: {error}")
            time.sleep(2**attempt)
    raise RuntimeError(f"could not fetch {url}") from last


def fetch(name: str, query: str = "document__key__in=srd-2024", limit: int = 100) -> list[dict]:
    """Every record of one endpoint, read from its cache file when it exists."""
    OUT.mkdir(exist_ok=True)
    cached = OUT / f"{name}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    records: list[dict] = []
    url = f"{BASE}/{name}/?{query}&limit={limit}"
    while url:
        page = get(url)
        records.extend(page["results"])
        url = page.get("next")
        print(f"{name}: {len(records)}/{page['count']}")
    cached.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records


def main() -> None:
    """Fetch every endpoint, then keep only the 5e-2024 descriptions of the shared conditions."""
    for endpoint in ENDPOINTS:
        fetch(endpoint)
    conditions = fetch("conditions", query="document__key__in=core")
    for condition in conditions:
        condition["descriptions"] = [
            description
            for description in condition.get("descriptions", [])
            if description.get("gamesystem") == "5e-2024"
        ]
    (OUT / "conditions.json").write_text(json.dumps(conditions, indent=2), encoding="utf-8")
    print("conditions:", len(conditions))


if __name__ == "__main__":
    main()
