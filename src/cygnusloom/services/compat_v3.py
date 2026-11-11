"""One-way conversion of Cygnus Loom 3.x campaigns to the current campaign contract.

This module is the only place that knows which event kinds 3.x wrote that the current engine no
longer writes. It is imported lazily by the conversion route, never at application startup, and
it does not register anything in the live event registry. Everything the current engine still
knows is decoded and replayed by the live services, so the adapter does not become a second
engine or a fork of 3.x.

An upgrade is refused when an event kind has no rewrite. The log stays untouched, so a later
release can add a rewrite and try again. The replay gate runs the complete converted log and
resolves every content reference before the campaign is stamped with the target major version.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Engine, select

from cygnusloom import CAMPAIGN_MAJOR_VERSION, __version__
from cygnusloom.content.library import ContentLibrary
from cygnusloom.db.models import Campaign, Event
from cygnusloom.services.campaigns import _exclusive
from cygnusloom.services.common import InvalidInput, NotFound, session_scope
from cygnusloom.services.events import (
    EVENT_TYPES,
    CampaignConverted,
    CampaignState,
    load_state,
    record,
)

# The campaign contract this adapter reads and writes. A future major version gets its own
# adapter rather than widening this one; the UI asks `supports` before offering an upgrade.
FROM_MAJOR = 3
TO_MAJOR = 4

# Event kinds 3.x wrote that the current engine no longer writes, with the event that replaces
# them. Every replacement is a current event so the converted log replays without this module.
EVENT_REWRITES: Mapping[str, tuple[str, Mapping[str, Any]]] = {
    # The upgrade marker opened an out-of-character turn so the turn.begun entry that follows
    # could attach to it. turn.opening opens that turn under the current contract.
    "campaign.upgraded": ("turn.opening", {"player_id": None}),
}


def supports(major: int) -> bool:
    """Whether this server can upgrade a campaign of `major` to the contract it runs."""
    return major == FROM_MAJOR and TO_MAJOR == CAMPAIGN_MAJOR_VERSION


def convert_campaign(
    engine: Engine,
    owner_id: int,
    campaign_id: int,
    library: ContentLibrary,
) -> None:
    """Upgrade one 3.x campaign in place, or raise without changing anything.

    The campaign row, its id, its members, and its events stay in place. The events are
    rewritten only after every kind has a current replacement, and the campaign is stamped with
    the target major version only after the whole converted log replays and every content
    reference resolves. There are no partial upgrades.
    """
    if not supports(FROM_MAJOR):
        raise InvalidInput(
            f"this upgrader moves campaigns to Cygnus Loom {TO_MAJOR}, but this server runs "
            f"Cygnus Loom {CAMPAIGN_MAJOR_VERSION}"
        )
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.owner_id != owner_id:
            raise NotFound("campaign not found")
        if campaign.major_version == CAMPAIGN_MAJOR_VERSION:
            raise InvalidInput(
                f"this campaign already plays on Cygnus Loom {CAMPAIGN_MAJOR_VERSION}"
            )
        if campaign.major_version != FROM_MAJOR:
            raise InvalidInput(
                f"Cygnus Loom {CAMPAIGN_MAJOR_VERSION} has no converter for campaigns made "
                f"by Cygnus Loom {campaign.major_version}"
            )
        if campaign.rule_set not in library.rule_sets:
            raise InvalidInput(f"this server has no rule set named {campaign.rule_set!r}")
        if campaign.world_template not in library.world_templates:
            raise InvalidInput(
                f"this server has no world template named {campaign.world_template!r}"
            )

        rows = list(
            db.execute(
                select(Event).where(Event.campaign_id == campaign_id).order_by(Event.seq)
            ).scalars()
        )
        if not rows:
            raise InvalidInput("the campaign has no events to convert")

        rewrites, source_version = _rewrites(rows)
        for row in rows:
            rewrite = rewrites.get(row.id)
            if rewrite is None:
                if row.kind not in EVENT_TYPES:
                    raise InvalidInput(
                        f"event {row.seq}: Cygnus Loom {TO_MAJOR} has no conversion for "
                        f"event kind {row.kind!r}"
                    )
                continue
            kind, payload, channel = rewrite
            row.kind = kind
            row.payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            row.channel = channel
        db.flush()

        try:
            state = load_state(db, campaign_id)
        except Exception as exc:
            raise InvalidInput(
                f"the campaign log could not be replayed under Cygnus Loom {TO_MAJOR}: {exc}"
            ) from exc

        problems = _content_problems(state, library, campaign)
        if state.turn is not None:
            problems.append(
                "the campaign ends in an open turn; finish or discard it on Cygnus Loom 3 "
                "and export it again"
            )
        if problems:
            raise InvalidInput("; ".join(problems))

        campaign.major_version = TO_MAJOR
        record(
            db,
            campaign_id,
            CampaignConverted(
                from_major=FROM_MAJOR,
                to_major=TO_MAJOR,
                from_version=source_version,
                to_version=__version__,
            ),
        )
        db.commit()


def _rewrites(
    rows: list[Event],
) -> tuple[dict[int, tuple[str, dict[str, Any], str]], str]:
    """The event rewrites to apply, keyed by stored event id, and the newest 3.x version the
    log recorded in an upgrade marker."""
    rewrites: dict[int, tuple[str, dict[str, Any], str]] = {}
    source_version = ""
    for row in rows:
        rewrite = EVENT_REWRITES.get(row.kind)
        if rewrite is None:
            continue
        kind, payload = rewrite
        event_class = EVENT_TYPES.get(kind)
        if event_class is None:
            raise RuntimeError(
                f"compat_v3 maps {row.kind!r} to {kind!r}, which is not a current event"
            )
        rewrites[row.id or 0] = (kind, dict(payload), event_class.channel.value)
        if row.kind == "campaign.upgraded":
            try:
                upgrade = json.loads(row.payload)
            except ValueError:
                continue
            if isinstance(upgrade, dict) and isinstance(upgrade.get("to_version"), str):
                source_version = upgrade["to_version"]
    return rewrites, source_version


def _content_problems(
    state: CampaignState,
    library: ContentLibrary,
    campaign: Campaign,
) -> list[str]:
    """Every id the campaign needs that this server's content cannot resolve.

    A conversion either produces a campaign that works on this server or does not happen. A
    missing feature, template, condition, counter, or skill would make the campaign fail as
    soon as the sheet, compendium, or that subsystem reads it, so each one is an error, not a
    warning.
    """
    rule_set = library.rule_sets[campaign.rule_set]
    spec = rule_set.character
    missing_features: set[str] = set()
    missing_templates: set[str] = set()
    missing_conditions: set[str] = set()
    missing_counters: set[str] = set()
    missing_skills: set[str] = set()
    for character in state.characters.values():
        for feature_id in character.features:
            if feature_id not in spec.features and feature_id not in character.custom_features:
                missing_features.add(feature_id)
        for template_id in character.templates.values():
            if template_id and template_id not in spec.templates:
                missing_templates.add(template_id)
        for condition_id in character.conditions:
            if condition_id not in rule_set.conditions:
                missing_conditions.add(condition_id)
        for counter_id in character.counters:
            if counter_id not in spec.counters:
                missing_counters.add(counter_id)
        for skill_id in character.skills:
            if skill_id not in spec.skills:
                missing_skills.add(skill_id)

    problems: list[str] = []
    for label, ids in (
        ("features", missing_features),
        ("templates", missing_templates),
        ("conditions", missing_conditions),
        ("counters", missing_counters),
        ("skills", missing_skills),
    ):
        if ids:
            problems.append(
                f"this server's rule set {campaign.rule_set} no longer has {len(ids)} "
                f"{label} the campaign uses: {_listed(ids)}"
            )
    return problems


def _listed(ids: set[str]) -> str:
    names = sorted(ids)
    shown = ", ".join(names[:6])
    if len(names) > 6:
        shown += f", and {len(names) - 6} more"
    return shown
