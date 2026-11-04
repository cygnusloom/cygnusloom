from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cygnusforge.content.definitions import ContentError
from cygnusforge.content.library import ContentLibrary, RuleSet, SaveRules
from cygnusforge.services.events import (
    InjuryGained,
    MaxChanged,
    current,
    decode,
    events_since,
    record,
)
from cygnusforge.services.injuries import EffectContext, run_effects
from tests import factories
from tests.conftest import content_library
from tests.support import DEFAULT_RULE_SET, content_tree

CAIRN = content_library().rule_sets["cairn2e"]
PC = "Mara"
YARD = "Stonegate Yard"
MAP = "Yard Green"

_DEFAULT_INJURIES = {
    "table": "synthetic",
    "stat": "hp",
    "trigger": "depleted",
    "target_kinds": ["pc"],
}


def _entries(count: int) -> list[dict[str, Any]]:
    return [
        {"roll": roll, "name": f"Injury {roll}", "text": "An injury.", "effects": []}
        for roll in range(1, count + 1)
    ]


def _swapped() -> list[dict[str, Any]]:
    entries = _entries(12)
    entries[0], entries[1] = entries[1], entries[0]
    return entries


def _stat(ident: str, name: str) -> dict[str, Any]:
    return {"type": "stat", "id": ident, "name": name, "derived": {"base": 10}}


def _synthetic_rules(
    tmp_path: Path,
    *,
    entries: list[dict[str, Any]] | None = None,
    injuries: Any = _DEFAULT_INJURIES,
) -> RuleSet:
    """A rule set whose injury table and combat are written for the test and loaded publicly."""
    tree = content_tree(
        tmp_path,
        definitions=[
            _stat("hp", "HP"),
            _stat("dex", "Dexterity"),
            {
                "type": "injury_table",
                "id": "synthetic",
                "name": "Synthetic Injuries",
                "entries": _entries(4) if entries is None else entries,
            },
            {
                "type": "combat",
                "id": "synthetic",
                "damage": [{"kind": "harm", "stat": "hp"}],
                "check_die": "1d20",
                "initiative_stat": "dex",
                "defeat": {"player_character": "down", "npc": "dead"},
                "armor_max": 0,
                "injuries": injuries,
            },
        ],
    )
    return ContentLibrary.load(tree).rule_sets[DEFAULT_RULE_SET]


def _injury_combat(tmp_path: Path, injuries: Any):
    combat = _synthetic_rules(tmp_path, entries=_entries(4), injuries=injuries).combat
    assert combat is not None
    return combat


def _fight(client_factory):
    """A Cairn campaign with Mara in the yard, the yard mapped, and an encounter under way."""
    client = factories.signed_in_client(client_factory)
    campaign_id = factories.create_campaign(client, world_template="twin-lakes", rule_set=CAIRN.id)
    factories.choose_campaign_options(client, campaign_id, CAIRN.id)
    created = factories.create_character(
        client,
        campaign_id,
        name=PC,
        fields=factories.creation_fields(client, campaign_id),
    )
    assert created.status_code == 303, created.text
    tools = factories.tools_for(client.app, campaign_id, rule_set=CAIRN.id)
    tools.give_item(PC, "Sword", damage="1d8")
    tools.create_location(YARD, "Cobbles around a dry well.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(MAP, YARD, 10, 10, "Open cobbles.")
    tools.start_encounter(MAP)
    tools.add_participant(PC)
    tools.player_initiative(1)
    return client.app, campaign_id, tools


def _foe(tools, name: str, hp: int = 6, armor: int = 0, claw: str = "1d6") -> None:
    tools.create_monster(
        name,
        YARD,
        description="A foe.",
        stats={"hp": hp, "str": 10, "dex": 10, "wil": 10},
        armor=armor,
        attacks=[{"name": "Claw", "dice": claw}],
    )
    tools.add_participant(name)


def _hit(tools, attacker: str, target: str, *dice: int) -> dict[str, Any]:
    """`attacker` swings its Claw at `target`, with the given dice."""
    return replace(tools, rng=factories.Rolls(*dice)).attack(attacker, "Claw", target)


def _character(app, campaign_id: int, name: str):
    return factories.character_named(app, campaign_id, name)


def _run(app, campaign_id: int, ops: list[dict[str, Any]], *dice: int, combat=CAIRN.combat) -> None:
    """Run effect ops on Mara with the given dice, in order."""
    with factories.db(app) as db:
        cx = EffectContext(rng=factories.Rolls(*dice), combat=combat)
        run_effects(db, campaign_id, PC, ops, cx, "a test", plus=CAIRN.plus)


def _set_max(app, campaign_id: int, stat: str, value: int) -> None:
    with factories.db(app) as db:
        before = _character(app, campaign_id, PC).stats[stat]
        record(
            db,
            campaign_id,
            MaxChanged(name=PC, stat=stat, before=before, after=value, cause="a test"),
        )
        db.commit()


def test_a_hit_to_exactly_zero_hp_gives_the_scar_for_the_harm_taken(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d20")
    _set_max(app, campaign_id, "hp", 10)
    _hit(tools, "Grave Thing", PC, 7)
    result = _hit(tools, "Grave Thing", PC, 3)

    assert result["injury"] == "Walloped"
    character = _character(app, campaign_id, PC)
    assert character.injuries == ["Walloped"]
    assert character.conditions == ["walloped"]
    assert current(character, "hp") == 0


def test_the_scar_roll_is_capped_at_the_last_entry(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d20")
    _set_max(app, campaign_id, "hp", 20)
    result = _hit(tools, "Grave Thing", PC, 20)

    assert result["injury"] == "Doomed"
    assert _character(app, campaign_id, PC).conditions == ["doomed"]


def test_a_hit_that_leaves_hp_above_zero_gives_no_scar(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d20")
    _set_max(app, campaign_id, "hp", 10)
    result = _hit(tools, "Grave Thing", PC, 1)

    assert "injury" not in result
    assert _character(app, campaign_id, PC).injuries == []


def test_a_hit_that_takes_hp_to_zero_and_overflows_gains_a_scar_and_owes_a_save(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d20")
    _set_max(app, campaign_id, "hp", 3)
    result = _hit(tools, "Grave Thing", PC, 5)

    assert result["harm"] == {"hp": 3, "str": 2}
    assert result["save_due"] == "str"
    assert result["injury"] == "Walloped"
    assert _character(app, campaign_id, PC).injuries == ["Walloped"]


def test_a_foe_brought_to_zero_gains_no_scar(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo", hp=2)
    result = replace(tools, rng=factories.Rolls(2, 1)).player_attack("Bo", "Sword")

    assert "injury" not in result


def test_damage_trigger_can_injure_an_npc_without_depleting_the_selected_stat(
    client_factory, tmp_path: Path
):
    app, campaign_id, tools = _fight(client_factory)
    injuries = {
        "table": "synthetic",
        "stat": "hp",
        "trigger": "damage",
        "target_kinds": ["npc"],
    }
    tools = replace(tools, combat=_injury_combat(tmp_path, injuries))
    _foe(tools, "Bo", hp=10)

    result = replace(tools, rng=factories.Rolls(1)).player_attack("Bo", "Sword")

    assert result["harm"] == {"hp": 1}
    assert result["injury"] == "Injury 1"
    npc = _character(app, campaign_id, "Bo")
    assert current(npc, "hp") == 9
    assert npc.injuries == ["Injury 1"]
    with factories.db(app) as db:
        [event] = [
            decode(row) for row in events_since(db, campaign_id) if row.kind == InjuryGained.kind
        ]
    assert event.render() == "Bo gains the injury Injury 1 (Injuries table 1). An injury."


def test_the_statblock_names_the_conditions_and_scars_a_character_has(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d20")
    lines = tools.get_statblock(PC)["statblock"]
    assert "Conditions: none" in lines
    assert not any(line.startswith("Injuries:") for line in lines)

    _set_max(app, campaign_id, "hp", 10)
    _hit(tools, "Grave Thing", PC, 7)
    _hit(tools, "Grave Thing", PC, 3)

    lines = tools.get_statblock(PC)["statblock"]
    assert "Conditions: Walloped" in lines
    assert "Injuries: Walloped" in lines


def test_a_set_max_if_higher_keeps_the_larger_of_the_two(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    before = _character(app, campaign_id, PC).stats["hp"]
    _run(
        app,
        campaign_id,
        [{"op": "set_max", "stat": "hp", "dice": "3d6", "mode": "if_higher"}],
        6,
        6,
        6,
    )
    assert _character(app, campaign_id, PC).stats["hp"] == max(before, 18)


def test_a_set_max_if_higher_keeps_the_maximum_when_the_roll_is_lower(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    before = _character(app, campaign_id, PC).stats["hp"]
    op = [{"op": "set_max", "stat": "hp", "dice": "1d6", "mode": "if_higher"}]
    _run(app, campaign_id, op, 1)
    assert _character(app, campaign_id, PC).stats["hp"] == before


def test_a_set_max_add_adds_the_roll(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    before = _character(app, campaign_id, PC).stats["hp"]
    _run(app, campaign_id, [{"op": "set_max", "stat": "hp", "dice": "1d6", "mode": "add"}], 4)
    assert _character(app, campaign_id, PC).stats["hp"] == before + 4


def test_a_set_max_always_takes_the_roll_even_when_it_is_lower(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    op = [{"op": "set_max", "stat": "hp", "dice": "2d6", "mode": "always"}]
    _run(app, campaign_id, op, 1, 1)
    assert _character(app, campaign_id, PC).stats["hp"] == 2


def test_a_set_max_if_above_current_takes_the_roll_only_when_it_is_above_the_current_score(
    client_factory,
):
    app, campaign_id, tools = _fight(client_factory)
    tools.harm_character(PC, "wil", 2, "a fall")
    wil_max = _character(app, campaign_id, PC).stats["wil"]
    current_wil = wil_max - 2
    op = [{"op": "set_max", "stat": "wil", "dice": "1d20", "mode": "if_above_current"}]

    _run(app, campaign_id, op, current_wil)
    assert _character(app, campaign_id, PC).stats["wil"] == wil_max

    _run(app, campaign_id, op, current_wil + 1)
    assert _character(app, campaign_id, PC).stats["wil"] == current_wil + 1


@pytest.mark.parametrize(
    ("roll", "stat"),
    [(1, "str"), (2, "str"), (3, "dex"), (4, "dex"), (5, "wil"), (6, "wil")],
)
def test_pick_stat_splits_the_faces_evenly_and_a_later_op_uses_the_pick(client_factory, roll, stat):
    app, campaign_id, _ = _fight(client_factory)
    before = _character(app, campaign_id, PC).stats[stat]
    ops = [
        {"op": "pick_stat", "options": ["str", "dex", "wil"], "dice": "1d6"},
        {"op": "set_max", "stat": "$picked", "dice": "1d6", "mode": "add"},
    ]
    _run(app, campaign_id, ops, roll, 2)
    assert _character(app, campaign_id, PC).stats[stat] == before + 2


def test_a_save_that_passes_runs_its_pass_ops(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    ops = [{"op": "save", "stat": "wil", "pass": [{"op": "condition", "id": "deafened"}]}]
    _run(app, campaign_id, ops, 1)
    assert _character(app, campaign_id, PC).conditions == ["deafened"]


def test_a_save_that_fails_runs_its_fail_ops(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    ops = [
        {
            "op": "save",
            "stat": "wil",
            "pass": [{"op": "condition", "id": "deafened"}],
            "fail": [{"op": "condition", "id": "diseased"}],
        }
    ]
    _run(app, campaign_id, ops, 20)
    assert _character(app, campaign_id, PC).conditions == ["diseased"]


@pytest.mark.parametrize(("save_roll", "condition"), [(1, "diseased"), (20, "deafened")])
def test_a_save_op_follows_a_declarative_roll_over_rule(
    client_factory, save_roll: int, condition: str
):
    app, campaign_id, _ = _fight(client_factory)
    combat = replace(CAIRN.combat, save=SaveRules(mode="roll_over"))
    ops = [
        {
            "op": "save",
            "stat": "wil",
            "pass": [{"op": "condition", "id": "deafened"}],
            "fail": [{"op": "condition", "id": "diseased"}],
        }
    ]

    _run(app, campaign_id, ops, save_roll, combat=combat)

    assert _character(app, campaign_id, PC).conditions == [condition]


def test_a_condition_already_held_is_not_added_twice(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    ops = [{"op": "condition", "id": "walloped"}]
    _run(app, campaign_id, ops)
    _run(app, campaign_id, ops)
    assert _character(app, campaign_id, PC).conditions == ["walloped"]


def test_a_note_changes_no_stat_or_condition(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    before = _character(app, campaign_id, PC)
    snapshot = (dict(before.stats), list(before.conditions))
    _run(app, campaign_id, [{"op": "note", "text": "Roll for where the injury is."}])
    after = _character(app, campaign_id, PC)
    assert (dict(after.stats), list(after.conditions)) == snapshot


def test_a_retire_op_retires_the_character(client_factory):
    app, campaign_id, _ = _fight(client_factory)

    _run(app, campaign_id, [{"op": "retire"}])

    assert _character(app, campaign_id, PC).retired


def test_a_non_12_scar_table_loads_contiguous_rolls(tmp_path: Path):
    combat = _synthetic_rules(tmp_path, entries=_entries(7)).combat
    assert combat is not None
    table = combat.injury_table
    assert table is not None
    assert [entry.roll for entry in table.entries] == list(range(1, 8))


def test_an_empty_scar_table_is_rejected(tmp_path: Path):
    with pytest.raises(ContentError, match="'entries' must be a non-empty list"):
        _synthetic_rules(tmp_path, entries=[])


def test_a_scar_table_whose_entries_are_out_of_order_is_rejected(tmp_path: Path):
    with pytest.raises(ContentError, match="'roll' must be 1"):
        _synthetic_rules(tmp_path, entries=_swapped())


def test_a_scar_naming_an_unknown_condition_is_rejected(tmp_path: Path):
    entries = _entries(12)
    entries[0]["effects"] = [{"op": "condition", "id": "not_a_condition"}]
    with pytest.raises(ContentError, match="names no stored condition"):
        _synthetic_rules(tmp_path, entries=entries)


def test_the_loaded_scar_table_has_twelve_entries_in_order():
    table = CAIRN.combat.injury_table
    assert table is not None
    assert table.stat == "hp"
    assert table.trigger == "depleted"
    assert table.target_kinds == ("pc",)
    assert [entry.roll for entry in table.entries] == list(range(1, 13))
    assert [entry.name for entry in table.entries][:3] == [
        "Lasting Scar",
        "Rattling Blow",
        "Walloped",
    ]
    assert table.entries[-1].name == "Doomed"


@pytest.mark.parametrize(
    ("injuries", "message"),
    [
        ("not an object", "'injuries' must be an object"),
        (
            {"stat": "hp", "trigger": "depleted", "target_kinds": ["pc"]},
            "'table' must be a non-empty string",
        ),
        (
            {"table": "missing", "stat": "hp", "trigger": "depleted", "target_kinds": ["pc"]},
            "names no injury table",
        ),
        (
            {
                "table": "synthetic",
                "stat": "luck",
                "trigger": "depleted",
                "target_kinds": ["pc"],
            },
            "names no stat",
        ),
        (
            {"table": "synthetic", "stat": "hp", "target_kinds": ["pc"]},
            "'trigger' must be one of",
        ),
        (
            {
                "table": "synthetic",
                "stat": "hp",
                "trigger": "always",
                "target_kinds": ["pc"],
            },
            "'trigger' must be one of",
        ),
        (
            {
                "table": "synthetic",
                "stat": "hp",
                "trigger": "damage",
                "target_kinds": [],
            },
            "'target_kinds' must be a non-empty list",
        ),
        (
            {
                "table": "synthetic",
                "stat": "hp",
                "trigger": "damage",
                "target_kinds": ["monster"],
            },
            "names no character kind",
        ),
        (
            {
                "table": "synthetic",
                "stat": "hp",
                "trigger": "damage",
                "target_kinds": ["npc", "npc"],
            },
            "must not list a kind twice",
        ),
        (
            {
                "table": "synthetic",
                "stat": "hp",
                "trigger": "damage",
                "target_kinds": ["npc"],
                "effect": "unsupported",
            },
            "unexpected field",
        ),
    ],
)
def test_malformed_injury_trigger_configuration_is_rejected(tmp_path: Path, injuries, message):
    with pytest.raises(ContentError, match=message):
        _injury_combat(tmp_path, injuries)


def test_the_scar_text_is_the_books_text():
    table = CAIRN.combat.injury_table
    assert table is not None
    assert table.entries[0].text == (
        "Roll 1d6. 1: Neck, 2: Hands, 3: Eye, 4: Chest, 5: Legs, 6: Ear. "
        "Roll 1d6. If the total is higher than your max HP, take the new result."
    )
    assert table.entries[5].text == (
        "Roll 1d6. 1-2: STR, 3-4: DEX, 5-6: WIL. Roll 3d6. "
        "If the total is higher than your current attribute, take the new result."
    )
    assert table.entries[-1].text.startswith("Death seemed ever so close")
