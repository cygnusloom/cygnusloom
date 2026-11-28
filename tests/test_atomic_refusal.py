"""An impossible action is refused whole: nothing is spent, rolled, or applied.

The live failure behind these tests was a Sleep cast that caught one goblin, rolled saves for
both, marked both incapacitated, and then took the second one back. The engine now measures every
named target before the cast spends anything, and the GM has an explicit refusal that leaves no
part of the action done.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Event
from cygnusloom.llm.base import ToolRequest
from cygnusloom.rules import dnd_srd_2024
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    ActionRefused,
    ChatPlayer,
    StateStepMade,
    decode,
    load_state,
    record,
)
from cygnusloom.services.tools import ToolRun, run_tool
from cygnusloom.services.turns import _turn_log

from .factories import (
    Rolls,
    character_named,
    close_opening,
    rules_for,
    tools_for,
)
from .test_dnd_srd import _prepared_wizard


def _wizard_with_sleep(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> tuple[TestClient, int, object]:
    client, campaign_id, ctx = _prepared_wizard(app, client_factory)
    close_opening(client, campaign_id)
    dnd_srd_2024.learn_spell("Wren", "spell-sleep", ctx)
    dnd_srd_2024.prepare_spells("Wren", ["spell-sleep", "spell-magic-missile"], ctx)
    return client, campaign_id, ctx


def _sleep_fight(
    client: TestClient, campaign_id: int, *, columns: int = 12
) -> tuple[list[str], object]:
    """An encounter with Wren acting and two goblins ten feet apart in the briar blind."""
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.create_location("Cave", "A low cave.")
    tools.move_actor("Wren", "Cave")
    tools.create_encounter_map("Cave Yard", "Cave", columns, columns, "Open stone.")
    tools.start_encounter("Cave Yard")
    tools.create_monster("Grub", "Cave", template="monster-goblin-warrior")
    tools.create_monster("Nokk", "Cave", template="monster-goblin-warrior")
    for name in ("Wren", "Grub", "Nokk"):
        assert tools.add_participant(name)["added"] == name
    ctx = _ctx(client, campaign_id)
    ctx = replace(ctx, world=replace(ctx.world, rng=Rolls(20, 1, 1)))
    assert dnd_srd_2024.roll_initiative(ctx)["order"][0] == "Wren"
    tools.adjust_encounter_order(["Wren", "Grub", "Nokk"], current="Wren")
    tools.place_participant("Wren", 1, 1)
    tools.place_participant("Grub", 11, 4)
    tools.place_participant("Nokk", 11, 6)
    return ["Wren", "Grub", "Nokk"], ctx


def _ctx(client: TestClient, campaign_id: int) -> object:
    from cygnusloom.services import level_up as level_up_service

    rules = rules_for("dnd-srd-2024")
    world = client.app.state.content.world_content("testhollow", "dnd-srd-2024")
    return level_up_service.tool_context(client.app.state.engine, campaign_id, rules, world)


def _state(app: FastAPI, campaign_id: int) -> object:
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id)


def _counts(app: FastAPI, campaign_id: int) -> tuple[int, bool, int, int]:
    """Slots, concentration, actions used, and attacks used for the acting caster."""
    state = _state(app, campaign_id)
    wren = state.characters["wren"]
    encounter = state.encounter
    return (
        wren.counters["slot_1"],
        "concentration" in wren.marks,
        encounter.actions_used if encounter is not None else -1,
        encounter.attacked if encounter is not None else -1,
    )


def test_sleep_refuses_a_target_outside_its_area_and_spends_nothing(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """The sixth-level report: Nokk and Grub are ten feet apart, so a five-foot sphere centred
    on Nokk cannot catch Grub. The whole cast is refused before the slot or action is spent."""
    client, campaign_id, _ = _wizard_with_sleep(app, client_factory)
    _names, ctx = _sleep_fight(client, campaign_id)
    before = _counts(app, campaign_id)
    latest = _newest_event_id(client.app, campaign_id)

    with pytest.raises(InvalidInput, match="beyond the 5-foot radius of Sleep"):
        dnd_srd_2024.cast_spell("Wren", "spell-sleep", ctx, targets=["Nokk", "Grub"], center="Nokk")

    assert _counts(app, campaign_id) == before
    # No save, condition, or slot change reached the log for the refused cast.
    with Session(app.state.engine) as db:
        kinds = db.exec(
            select(Event.kind).where(Event.campaign_id == campaign_id, Event.id > latest)
        ).all()
    assert "character.saved" not in kinds
    assert "condition.added" not in kinds
    assert "counter.changed" not in kinds


def test_sleep_catches_both_when_the_centre_is_between_them(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """The same pair is a legal target set when the player centres the sphere between them."""
    client, campaign_id, _ = _wizard_with_sleep(app, client_factory)
    _names, ctx = _sleep_fight(client, campaign_id)
    before = _counts(app, campaign_id)

    result = dnd_srd_2024.cast_spell(
        "Wren", "spell-sleep", ctx, targets=["Grub", "Nokk"], center="column 11, row 5"
    )

    assert result["spell"] == "Sleep"
    after = _counts(app, campaign_id)
    assert after[0] == before[0] - 1  # one first-level slot
    assert after[1] is True  # concentration on Sleep
    assert after[2] == 1  # the cast is the turn's action


def test_a_target_beyond_the_spells_range_is_refused_before_anything_is_spent(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """Sleep reaches 60 feet. A target 130 feet away refuses the whole cast."""
    client, campaign_id, _ = _wizard_with_sleep(app, client_factory)
    _names, ctx = _sleep_fight(client, campaign_id, columns=30)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.place_participant("Grub", 27, 1)
    before = _counts(app, campaign_id)

    with pytest.raises(InvalidInput, match="beyond the 60-foot range of Sleep"):
        dnd_srd_2024.cast_spell("Wren", "spell-sleep", ctx, targets=["Grub"])

    assert _counts(app, campaign_id) == before


def test_a_spell_attack_beyond_range_is_refused_before_the_player_rolls(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """Fire Bolt reaches 120 feet; the precheck refuses a 130-foot target so no prompt appears."""
    client, campaign_id, _ = _wizard_with_sleep(app, client_factory)
    _names, ctx = _sleep_fight(client, campaign_id, columns=30)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    tools.place_participant("Grub", 27, 1)
    before = _counts(app, campaign_id)

    with pytest.raises(InvalidInput, match="beyond the 120-foot range of Fire Bolt"):
        tools.spell_attack_check("Wren", spell="fire-bolt", target="Grub")
    with pytest.raises(InvalidInput, match="beyond the 120-foot range of Fire Bolt"):
        dnd_srd_2024.spell_attack("Wren", "fire-bolt", "Grub", ctx, roll=15)

    assert _counts(app, campaign_id) == before
    with Session(app.state.engine) as db:
        kinds = db.exec(select(Event.kind).where(Event.campaign_id == campaign_id)).all()
    assert "combat.spell_attack" not in kinds


def test_an_unknown_target_refuses_the_whole_cast(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """A name that is not a character is a refusal, not a cast against nobody."""
    client, campaign_id, _ = _wizard_with_sleep(app, client_factory)
    _names, ctx = _sleep_fight(client, campaign_id)
    before = _counts(app, campaign_id)

    with pytest.raises(InvalidInput, match="there is no character named 'Nobody'"):
        dnd_srd_2024.cast_spell("Wren", "spell-sleep", ctx, targets=["Nobody"])

    assert _counts(app, campaign_id) == before


def test_refusing_an_action_records_a_log_line_and_changes_nothing(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """The GM's atomic refusal is a first-class log entry, and it applies no state change."""
    client, campaign_id, _ = _wizard_with_sleep(app, client_factory)
    _names, _fight_ctx = _sleep_fight(client, campaign_id)
    ctx = _ctx(client, campaign_id)
    before = character_named(app, campaign_id, "Wren")
    opener = _newest_event_id(client.app, campaign_id)

    request = ToolRequest(
        id="call-1",
        name="refuse_action",
        arguments=json.dumps(
            {
                "action": "Cast Sleep on both goblins",
                "reason": (
                    "Grub is ten feet from Nokk, outside the five-foot radius; "
                    "centre the spell between them, or cast it on Nokk alone"
                ),
            }
        ),
    )
    run = asyncio.run(run_tool(rules_for("dnd-srd-2024").tool_calls, request, None, ctx))
    assert isinstance(run, ToolRun)
    payload = json.loads(run.output)
    assert payload["refused"] == "Cast Sleep on both goblins"

    with Session(client.app.state.engine) as db:
        rows = db.exec(
            select(Event).where(Event.campaign_id == campaign_id, Event.kind == ActionRefused.kind)
        ).all()
    assert len(rows) == 1
    text = decode(rows[0]).render()
    assert text.startswith("Refused: Cast Sleep on both goblins did not happen.")
    assert "centre the spell between them" in text
    log = _turn_log(client.app.state.engine, campaign_id, opener)
    assert any("Cast Sleep on both goblins did not happen" in line for line in log)

    after = character_named(app, campaign_id, "Wren")
    assert after.counters == before.counters
    assert after.marks == before.marks


def test_a_refusal_after_a_change_is_refused(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """A refusal must come before the action is under way; undoing a done change is not a
    refusal, so the engine refuses the refusal and keeps the log honest."""
    client, campaign_id, _ = _wizard_with_sleep(app, client_factory)
    _names, _fight_ctx = _sleep_fight(client, campaign_id)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    with Session(client.app.state.engine) as db:
        record(
            db,
            campaign_id,
            ChatPlayer(content="Ilyana casts sleep on both goblins", speaker="Wren", player_id=1),
        )
        record(
            db,
            campaign_id,
            StateStepMade(
                requests=[],
                messages=[
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-0",
                                "type": "function",
                                "function": {
                                    "name": "place_participant",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                ],
                context=[],
                final=False,
            ),
        )
        db.commit()

    with pytest.raises(InvalidInput, match="already changed the world with place_participant"):
        tools.refuse_action("Cast Sleep on both goblins", "Grub is out of range")

    with Session(client.app.state.engine) as db:
        kinds = db.exec(select(Event.kind).where(Event.campaign_id == campaign_id)).all()
    assert ActionRefused.kind not in kinds


def test_no_change_tool_runs_after_a_refusal(
    app: FastAPI, client_factory: Callable[[], TestClient]
) -> None:
    """A refusal is atomic for the turn: a cast that would otherwise be legal is refused, so the
    player's action is not half done, and a lookup still works."""
    client, campaign_id, _ = _wizard_with_sleep(app, client_factory)
    _names, ctx = _sleep_fight(client, campaign_id)
    tools = tools_for(client.app, campaign_id, rule_set="dnd-srd-2024")
    with Session(client.app.state.engine) as db:
        record(
            db,
            campaign_id,
            ChatPlayer(content="Ilyana casts sleep on both goblins", speaker="Wren", player_id=1),
        )
        db.commit()
    tools.refuse_action("Cast Sleep on both goblins", "the goblins are fifteen feet apart")
    before = character_named(app, campaign_id, "Wren").counters["slot_1"]

    request = ToolRequest(
        id="call-2",
        name="cast_spell",
        arguments=json.dumps(
            {"name": "Wren", "spell": "Sleep", "targets": ["Grub"], "center": "Grub"}
        ),
    )
    run = asyncio.run(run_tool(rules_for("dnd-srd-2024").tool_calls, request, None, ctx))
    assert isinstance(run, ToolRun)
    assert "refused this turn" in json.loads(run.output)["error"]
    assert character_named(app, campaign_id, "Wren").counters["slot_1"] == before

    lookup = ToolRequest(id="call-3", name="get_statblock", arguments=json.dumps({"name": "Wren"}))
    run = asyncio.run(run_tool(rules_for("dnd-srd-2024").tool_calls, lookup, None, ctx))
    assert isinstance(run, ToolRun)
    assert json.loads(run.output)["name"] == "Wren"


def _newest_event_id(app: FastAPI, campaign_id: int) -> int:
    with Session(app.state.engine) as db:
        newest = db.exec(
            select(Event.id).where(Event.campaign_id == campaign_id).order_by(Event.id.desc())
        ).first()
    return int(newest or 0)
