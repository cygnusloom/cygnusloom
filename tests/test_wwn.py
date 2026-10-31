"""Smoke tests for the Worlds Without Number ruleset: content, creation, checks, combat."""

from __future__ import annotations

import re

from sqlmodel import Session

from cygnusforge.content.character import derived_stats
from cygnusforge.content.library import ContentLibrary
from cygnusforge.services.events import load_state
from tests.conftest import content_library
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_character_creation import _choices, _rolled_stats


def _wwn() -> ContentLibrary:
    return content_library()


def test_wwn_content_loads() -> None:
    rule_set = _wwn().rule_sets["worldswithoutnumber"]
    assert set(rule_set.character.stats) >= {
        "str",
        "dex",
        "con",
        "int",
        "wis",
        "cha",
        "hp",
        "save_physical",
        "save_evasion",
        "save_mental",
        "save_luck",
        "strain",
    }
    assert len(rule_set.character.skills) == 21
    assert rule_set.character.features
    assert rule_set.monsters
    assert rule_set.checks["skill"].dice == "2d6"
    assert rule_set.combat is not None
    assert rule_set.combat.hit_mode == "roll_vs_defense"
    assert rule_set.combat.shock_on_miss
    assert rule_set.combat.defeat_at_zero == ("hp",)


def test_wwn_strength_modifier_bands() -> None:
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    assert spec.modifier({"str": 3}, "str") == -2
    assert spec.modifier({"str": 7}, "str") == -1
    assert spec.modifier({"str": 10}, "str") == 0
    assert spec.modifier({"str": 14}, "str") == 1
    assert spec.modifier({"str": 18}, "str") == 2


def test_wwn_derived_saves_and_effort() -> None:
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    stats = {"str": 14, "dex": 8, "con": 18, "int": 12, "wis": 10, "cha": 3}
    values = derived_stats(spec, stats, {"level": 3}, {"magic": 1})
    assert values["save_physical"] == 16 - 3 + 2
    assert values["save_evasion"] == 16 - 3
    assert values["save_mental"] == 16 - 3
    assert values["save_luck"] == 16 - 3
    assert values["strain"] == 18
    assert values["effort_high"] == 1 + 1 + 0


def test_wwn_character_creation(client_factory) -> None:
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="latter-earth", rule_set="worldswithoutnumber")
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    created = _create_character(client, campaign_id, name="Wren", fields=fields)
    error = re.search(r'<p class="form-error"[^>]*>(.*?)</p>', created.text, re.DOTALL)
    assert created.status_code == 303, error.group(1) if error else "no error"
    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
    pc = state.pc()
    assert pc is not None
    assert (
        len([stat for stat in pc.stats if stat in {"str", "dex", "con", "int", "wis", "cha"}]) == 6
    )
    assert pc.skills
    assert "background" in pc.templates and "class" in pc.templates
    assert "tradition" not in pc.templates
    assert pc.stats["hp"] >= 1
    assert pc.stats["save_physical"] <= 16
