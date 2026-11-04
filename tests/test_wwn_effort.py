"""Effort pools belong only to characters whose class or tradition grants them.

A Warrior or Expert has no Effort at all. A Mage has the pool of the tradition they chose, a
partial Mage has the pool of their partial tradition, a Healer has Healer Effort, and a Vowed has
Vowed Effort. Commitment still refuses a tradition the character does not have, and an eligible
character can still commit and recover Effort.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.character import derived_stats, resolve
from cygnusloom.rules import wwn, wwn_magic
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import load_state
from cygnusloom.services.views import scene_view
from tests.test_wwn_advancement import (
    NAME,
    WWN,
    _campaign,
    _ctx,
    _pc,
    _world_tools,
)

EFFORT_STATS = (
    "effort_high",
    "effort_necromancer",
    "effort_elemental",
    "effort_healer",
    "effort_vowed",
)


def _effort_pools(stats: Mapping[str, object]) -> set[str]:
    """The Effort pools a sheet actually holds."""
    return {stat for stat in EFFORT_STATS if stat in stats}


@pytest.mark.parametrize(
    ("class_id", "tradition", "tradition2", "expected"),
    [
        ("warrior", None, None, set()),
        ("expert", None, None, set()),
        ("mage", "high_mage", None, {"effort_high"}),
        ("mage", "elementalist", None, {"effort_elemental"}),
        ("adventurer_expert_mage", "high_mage_partial", None, {"effort_high"}),
        ("healer", None, None, {"effort_healer"}),
        ("vowed", None, None, {"effort_vowed"}),
        (
            "adventurer_mage_mage",
            "high_mage_partial",
            "necromancer_partial_2",
            {"effort_high", "effort_necromancer"},
        ),
    ],
)
def test_a_character_has_only_the_effort_pools_their_choices_grant(
    client_factory: Callable[[], TestClient],
    class_id: str,
    tradition: str | None,
    tradition2: str | None,
    expected: set[str],
) -> None:
    app, campaign_id = _campaign(
        client_factory, class_id=class_id, tradition=tradition, tradition2=tradition2
    )

    assert _effort_pools(_pc(app, campaign_id).stats) == expected


def test_a_healer_with_a_partial_mage_tradition_has_both_pools(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(
        client_factory,
        class_id="healer",
        tradition="high_mage_partial",
        options={"template-class2": "mage_partial_2"},
    )

    assert _effort_pools(_pc(app, campaign_id).stats) == {"effort_healer", "effort_high"}


def test_a_second_partial_class_grants_its_effort_pool(
    client_factory: Callable[[], TestClient],
) -> None:
    healer_app, healer_campaign = _campaign(
        client_factory, class_id="healer", class2="vowed_partial_2"
    )
    assert _effort_pools(_pc(healer_app, healer_campaign).stats) == {
        "effort_healer",
        "effort_vowed",
    }

    vowed_app, vowed_campaign = _campaign(
        client_factory, class_id="vowed", class2="healer_partial_2"
    )
    assert _effort_pools(_pc(vowed_app, vowed_campaign).stats) == {
        "effort_healer",
        "effort_vowed",
    }


def test_a_healer_and_second_partial_vowed_recovers_both_effort_pools(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="healer", class2="vowed_partial_2")
    ctx = _ctx(_world_tools(app, campaign_id))

    wwn_magic.commit_effort(NAME, "healer", 1, "scene", ctx)
    wwn_magic.commit_effort(NAME, "vowed", 1, "scene", ctx)
    rested = wwn.recover_after_rest(NAME, ctx)

    assert rested["restored"]["effort_healer"] == 1
    assert rested["restored"]["effort_vowed"] == 1


def test_a_warrior_sheet_shows_no_effort_lines(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="warrior")

    with Session(app.state.engine) as db:
        sheet = scene_view(load_state(db, campaign_id), WWN.character).sheet

    assert not any("Effort" in line for line in sheet)


def test_commitment_refuses_a_character_without_a_matching_pool(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    ctx = _ctx(_world_tools(app, campaign_id))

    with pytest.raises(InvalidInput, match="cannot commit high Effort"):
        wwn_magic.commit_effort(NAME, "high", 1, "scene", ctx)

    character = _pc(app, campaign_id)
    assert wwn.EFFORT_MARK not in character.marks
    assert not any(key.startswith("effort_") for key in character.harm)


def test_commitment_and_recovery_still_work_for_an_eligible_character(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    ctx = _ctx(_world_tools(app, campaign_id))
    maximum = _pc(app, campaign_id).stats["effort_high"]

    committed = wwn_magic.commit_effort(NAME, "high", 1, "scene", ctx)
    assert committed["maximum"] == maximum
    assert _pc(app, campaign_id).harm["effort_high"] == 1

    recovered = wwn_magic.recover_effort(NAME, "high", "scene", ctx)
    assert recovered["recovered"] == 1
    assert wwn.EFFORT_MARK not in _pc(app, campaign_id).marks
    assert _pc(app, campaign_id).harm.get("effort_high", 0) == 0


def test_derived_effort_pools_need_their_template() -> None:
    spec = WWN.character
    stats = {"str": 10, "dex": 10, "con": 10, "int": 14, "wis": 10, "cha": 10}

    # With no chosen templates, no pool is granted.
    assert not _effort_pools(derived_stats(spec, stats, {"level": 1}, {"magic": 1}))

    values = derived_stats(
        spec,
        stats,
        {"level": 1},
        {"magic": 1},
        {"class": "mage", "tradition": "elementalist"},
    )
    assert values["effort_elemental"] == 3
    assert not {"effort_high", "effort_necromancer"} & set(values)


def test_resolve_leaves_effort_off_a_warrior_sheet() -> None:
    sheet = resolve(
        WWN.character,
        {"templates": {"class": "warrior", "tradition": "no_tradition"}},
        random.Random(7),
        plus=WWN.plus,
    )

    assert not _effort_pools(sheet.stats)
