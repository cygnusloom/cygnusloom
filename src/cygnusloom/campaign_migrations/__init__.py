"""Campaign contract migrations.

This package is the only place that knows how a campaign written for an older major
version is upgraded. Each module moves one major version forward:

* ``v3`` rewrites Cygnus Loom 3 event kinds and payloads into the Cygnus Loom 4 shape.
* ``v4`` rewrites Cygnus Loom 4 payloads and inventory into the Cygnus Loom 5 shape.

``convert_campaign`` applies every step from the campaign's major version to the major
version this server runs, in order. It then replays the fully converted log and resolves
every content reference before stamping the campaign with the current major. The event
registry and live services never need to understand an older major version.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import ModuleType
from typing import Any

from sqlalchemy import Engine, select

from cygnusloom import CAMPAIGN_MAJOR_VERSION, __version__
from cygnusloom.content.library import ContentLibrary
from cygnusloom.db.models import Campaign, Event
from cygnusloom.services import encounters
from cygnusloom.services.campaigns import _exclusive
from cygnusloom.services.common import InvalidInput, NotFound, session_scope
from cygnusloom.services.events import (
    EVENT_TYPES,
    CampaignConverted,
    CampaignState,
    load_state,
    record,
)

from . import v3, v4

STEPS: Mapping[int, ModuleType] = {
    v3.FROM_MAJOR: v3,
    v4.FROM_MAJOR: v4,
}


def _steps(major: int) -> tuple[ModuleType, ...]:
    steps: list[ModuleType] = []
    current = major
    while current < CAMPAIGN_MAJOR_VERSION:
        step = STEPS.get(current)
        if step is None or step.TO_MAJOR != current + 1:
            return ()
        steps.append(step)
        current = step.TO_MAJOR
    return tuple(steps)


def supports(major: int) -> bool:
    """Whether this server can upgrade a campaign of `major` to the contract it runs."""
    return bool(_steps(major))


def convert_campaign(
    engine: Engine,
    owner_id: int,
    campaign_id: int,
    library: ContentLibrary,
) -> None:
    """Upgrade one older-major campaign in place, or raise without changing anything.

    The campaign row, its id, its members, and its events stay in place. Each migration
    step rewrites the event rows in order, and the campaign is stamped with the current
    major only after the whole converted log replays and every content reference resolves.
    There are no partial upgrades.
    """
    with _exclusive(campaign_id), session_scope(engine) as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None or campaign.owner_id != owner_id:
            raise NotFound("campaign not found")
        if campaign.major_version == CAMPAIGN_MAJOR_VERSION:
            raise InvalidInput(
                f"this campaign already plays on Cygnus Loom {CAMPAIGN_MAJOR_VERSION}"
            )
        steps = _steps(campaign.major_version)
        if not steps:
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

        original_major = campaign.major_version
        source_version = ""
        for step in steps:
            found = step.rewrite(rows, library, campaign)
            if found:
                source_version = found

        for row in rows:
            if row.kind not in EVENT_TYPES:
                raise InvalidInput(
                    f"event {row.seq}: Cygnus Loom {CAMPAIGN_MAJOR_VERSION} has no conversion "
                    f"for event kind {row.kind!r}"
                )
        db.flush()

        try:
            state = load_state(db, campaign_id)
        except Exception as exc:
            raise InvalidInput(
                f"the campaign log could not be replayed under Cygnus Loom "
                f"{CAMPAIGN_MAJOR_VERSION}: {exc}"
            ) from exc

        problems = _content_problems(state, library, campaign)
        if state.turn is not None:
            problems.append(
                "the campaign ends in an open turn; finish or discard it on Cygnus Loom "
                f"{original_major} and export it again"
            )
        if problems:
            raise InvalidInput("; ".join(problems))

        # A converted log follows the 5.x turn contract: a player character whose turn is
        # next starts acting at once instead of waiting for an explicit Begin button.
        if state.turn is None:
            encounters.begin_player_turn_if_next(db, campaign_id)

        campaign.major_version = CAMPAIGN_MAJOR_VERSION
        record(
            db,
            campaign_id,
            CampaignConverted(
                from_major=original_major,
                to_major=CAMPAIGN_MAJOR_VERSION,
                from_version=source_version,
                to_version=__version__,
            ),
        )
        db.commit()


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


def payload(row: Event) -> dict[str, Any]:
    """The decoded payload of a stored event row."""
    raw = json.loads(row.payload)
    if not isinstance(raw, dict):
        raise InvalidInput(f"event {row.seq} has a payload that is not an object")
    return raw


def set_payload(row: Event, data: Mapping[str, Any]) -> None:
    """Store a rewritten payload on an event row without changing its id or sequence."""
    row.payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
