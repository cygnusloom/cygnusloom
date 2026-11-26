"""D&D SRD 5.2.1 Concentration: the damage save, incapacitation, and Rage."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.rules import dnd_srd_2024
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import MarkSet, record

from .factories import Rolls, character_named, tools_for
from .test_dnd_srd import _prepared_wizard
from .test_dnd_srd_play import BARBARIAN_FIELDS, _campaign


def _concentrating(app: FastAPI, campaign_id: int, name: str, spell: str = "Fog Cloud") -> None:
    """Give a character the concentration mark, as casting the spell would."""
    with Session(app.state.engine) as db:
        record(
            db,
            campaign_id,
            MarkSet(
                name=name,
                key="concentration",
                value={"id": "spell-fog-cloud", "name": spell},
                text=f"{name} concentrates on {spell}.",
            ),
        )
        db.commit()


def test_the_concentration_save_is_ten_or_half_the_damage_up_to_thirty():
    assert [dnd_srd_2024.concentration_save_dc(damage) for damage in (1, 19, 20, 21, 22)] == [
        10,
        10,
        10,
        10,
        11,
    ]
    assert dnd_srd_2024.concentration_save_dc(60) == 30
    assert dnd_srd_2024.concentration_save_dc(100) == 30


def test_damage_leaves_a_concentrating_player_character_a_save_to_roll(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.cast_spell("Wren", "fog-cloud", ctx)
    tools = tools_for(app, campaign_id, rule_set="dnd-srd-2024")

    result = tools.harm_character("Wren", "hp", 5, "a falling rock")

    expected = {
        "spell": "Fog Cloud",
        "save": "con",
        "dc": 10,
        "pending": True,
    }
    concentration = result["concentration"]
    assert {key: concentration[key] for key in expected} == expected
    assert concentration["cause"].startswith("concentration")
    marks = character_named(app, campaign_id, "Wren").marks
    assert marks["concentration"]["id"] == "spell-fog-cloud"
    assert marks["concentration"]["saves"][0]["dc"] == 10
    # The save is owed before the character casts again.
    with pytest.raises(InvalidInput, match="player_save"):
        dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx)
    panel = client.get(f"/campaigns/{campaign_id}").text.partition('id="panel-character"')[2]
    assert "Concentrating" in panel and "Fog Cloud" in panel
    assert "Concentration save due" in panel


def test_a_failed_concentration_save_ends_the_spell(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.cast_spell("Wren", "fog-cloud", ctx)
    pending = tools_for(app, campaign_id, rule_set="dnd-srd-2024").harm_character(
        "Wren", "hp", 5, "a falling rock"
    )["concentration"]
    result = dnd_srd_2024.roll_save("Wren", "con", pending["dc"], pending["cause"], ctx, roll=5)
    assert result["passed"] is False
    assert "concentration" not in character_named(app, campaign_id, "Wren").marks


def test_a_made_concentration_save_keeps_the_spell(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.cast_spell("Wren", "fog-cloud", ctx)
    pending = tools_for(app, campaign_id, rule_set="dnd-srd-2024").harm_character(
        "Wren", "hp", 5, "a falling rock"
    )["concentration"]
    result = dnd_srd_2024.roll_save("Wren", "con", pending["dc"], pending["cause"], ctx, roll=15)
    assert result["passed"] is True
    wren = character_named(app, campaign_id, "Wren")
    assert dnd_srd_2024.concentration_state(wren) == ("spell-fog-cloud", "Fog Cloud", [])
    # A spell without concentration leaves the maintained spell alone.
    dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx)
    wren = character_named(app, campaign_id, "Wren")
    assert dnd_srd_2024.concentration_state(wren)[0] == "spell-fog-cloud"


def test_an_unrelated_constitution_save_does_not_answer_the_concentration_save(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.cast_spell("Wren", "fog-cloud", ctx)
    pending = tools_for(app, campaign_id, rule_set="dnd-srd-2024").harm_character(
        "Wren", "hp", 5, "a falling rock"
    )["concentration"]

    dnd_srd_2024.roll_save("Wren", "con", 12, "poison", ctx, roll=20)
    saves = character_named(app, campaign_id, "Wren").marks["concentration"]["saves"]
    assert [entry["dc"] for entry in saves] == [pending["dc"]]
    with pytest.raises(InvalidInput, match="player_save"):
        dnd_srd_2024.cast_spell("Wren", "magic-missile", ctx)

    # A save the GM words in their own way still answers the one the engine recorded.
    dnd_srd_2024.roll_save(
        "Wren", "con", pending["dc"], "holding concentration after a hit", ctx, roll=20
    )
    wren = character_named(app, campaign_id, "Wren")
    assert dnd_srd_2024.concentration_state(wren) == ("spell-fog-cloud", "Fog Cloud", [])


def test_a_concentration_save_the_gm_asks_for_before_any_is_due_ends_the_spell(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.cast_spell("Wren", "fog-cloud", ctx)

    result = dnd_srd_2024.roll_save(
        "Wren", "con", 10, "concentrating through the blast", ctx, roll=5
    )

    assert result["passed"] is False
    assert "concentration" not in character_named(app, campaign_id, "Wren").marks


def test_incapacitating_conditions_end_concentration(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    tools = tools_for(app, campaign_id, rule_set="dnd-srd-2024")
    for condition in ("incapacitated", "paralyzed", "stunned", "petrified", "unconscious"):
        dnd_srd_2024.cast_spell("Wren", "detect-magic", ctx, ritual=True)
        tools.add_condition("Wren", condition, "a mind blast")
        assert "concentration" not in character_named(app, campaign_id, "Wren").marks, condition
        tools.end_condition("Wren", condition, "gm")


def test_a_condition_that_does_not_incapacitate_leaves_concentration_alone(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.cast_spell("Wren", "detect-magic", ctx, ritual=True)
    tools_for(app, campaign_id, rule_set="dnd-srd-2024").add_condition(
        "Wren", "frightened", "a shriek"
    )
    wren = character_named(app, campaign_id, "Wren")
    assert dnd_srd_2024.concentration_state(wren)[0] == "spell-detect-magic"


def test_falling_to_zero_hit_points_ends_concentration_without_a_save(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.cast_spell("Wren", "fog-cloud", ctx)
    tools = tools_for(app, campaign_id, rule_set="dnd-srd-2024")

    result = tools.harm_character("Wren", "hp", 8, "a falling rock")

    assert result["defeated"] is True
    assert result["concentration"]["ended"] is True
    assert result["concentration"]["reason"] == "unconscious"
    assert "concentration" not in character_named(app, campaign_id, "Wren").marks


def test_an_npc_rolls_its_own_concentration_save(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, _ctx = _prepared_wizard(app, client_factory)
    tools = tools_for(app, campaign_id, rule_set="dnd-srd-2024", rng=Rolls(5))
    tools.create_location("Cave", "A low cave.")
    tools.create_monster("Foe", "Cave", template="monster-ogre")
    _concentrating(app, campaign_id, "Foe")

    result = tools.harm_character("Foe", "hp", 25, "a boulder")

    concentration = result["concentration"]
    assert concentration["dc"] == 12
    assert concentration["passed"] is False
    assert concentration["ended"] is True
    assert "concentration" not in character_named(app, campaign_id, "Foe").marks


def test_an_npc_keeps_concentration_when_its_save_succeeds(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, _ctx = _prepared_wizard(app, client_factory)
    tools = tools_for(app, campaign_id, rule_set="dnd-srd-2024", rng=Rolls(20))
    tools.create_location("Cave", "A low cave.")
    tools.create_monster("Foe", "Cave", template="monster-ogre")
    _concentrating(app, campaign_id, "Foe")

    result = tools.harm_character("Foe", "hp", 25, "a boulder")

    concentration = result["concentration"]
    assert concentration["dc"] == 12
    assert concentration["passed"] is True
    assert concentration["ended"] is False
    assert "concentration" in character_named(app, campaign_id, "Foe").marks


def test_rage_ends_concentration_and_forbids_spells(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id, ctx = _campaign(client_factory, fields=BARBARIAN_FIELDS)
    _concentrating(app=client.app, campaign_id=campaign_id, name="Rook")

    dnd_srd_2024.use_feature("Rook", "Rage", ctx)

    rook = character_named(client.app, campaign_id, "Rook")
    assert "concentration" not in rook.marks
    assert rook.marks["raging"] is True
    with pytest.raises(InvalidInput, match="raging"):
        dnd_srd_2024.cast_spell("Rook", "fog-cloud", ctx)
    with pytest.raises(InvalidInput, match="raging"):
        dnd_srd_2024.spell_attack("Rook", "fog-cloud", "Foe", ctx)


def test_a_long_rest_ends_concentration(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    _client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    dnd_srd_2024.cast_spell("Wren", "fog-cloud", ctx)
    tools = tools_for(app, campaign_id, rule_set="dnd-srd-2024")

    tools.rest("long")

    assert "concentration" not in character_named(app, campaign_id, "Wren").marks
