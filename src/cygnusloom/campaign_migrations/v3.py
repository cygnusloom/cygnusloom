"""Cygnus Loom 3 -> 4 campaign event rewrites.

This module only moves the log to the 4.x contract. Anything the 4.x contract changed
again is handled by the next module in the chain.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cygnusloom.content.library import ContentLibrary
from cygnusloom.db.models import Campaign, Event
from cygnusloom.services.events import EVENT_TYPES

FROM_MAJOR = 3
TO_MAJOR = 4

# Event kinds 3.x wrote that 4.x no longer writes, with the event that replaces them.
# Every replacement is a 4.x event, so the next migration step can read it without this
# module.
EVENT_REWRITES: Mapping[str, tuple[str, Mapping[str, Any]]] = {
    # The upgrade marker opened an out-of-character turn so the turn.begun entry that
    # follows could attach to it. turn.opening opens that turn under the 4.x contract.
    "campaign.upgraded": ("turn.opening", {"player_id": None}),
}


def rewrite(
    rows: list[Event],
    library: ContentLibrary,
    campaign: Campaign,
) -> str:
    """Rewrite every 3.x-only event row in place and return the newest 3.x version found."""
    source_version = ""
    for row in rows:
        rewrite = EVENT_REWRITES.get(row.kind)
        if rewrite is None:
            continue
        kind, template_payload = rewrite
        event_class = EVENT_TYPES.get(kind)
        if event_class is None:
            raise RuntimeError(
                f"v3 migration maps {row.kind!r} to {kind!r}, which is not a current event"
            )
        payload = dict(template_payload)
        if row.kind == "campaign.upgraded":
            try:
                upgrade = json.loads(row.payload)
            except ValueError:
                upgrade = None
            if isinstance(upgrade, dict) and isinstance(upgrade.get("to_version"), str):
                source_version = upgrade["to_version"]
        row.kind = kind
        row.payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        row.channel = event_class.channel.value
    return source_version
