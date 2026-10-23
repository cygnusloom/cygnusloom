from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import _load_injury_entries
from cygnusforge.services.events import MaxChanged, current, record
from cygnusforge.services.injuries import EffectContext, run_effects
from tests.test_combat import CAIRN, PC, Rolls, _character, _fight, _foe, _hit
from tests.test_world import _db


def _entries(count: int) -> list[dict[str, Any]]:
    return [
        {"roll": roll, "name": f"Injury {roll}", "text": "An injury.", "effects": []}
        for roll in range(1, count + 1)
    ]


def _swapped() -> list[dict[str, Any]]:
    entries = _entries(12)
    entries[0], entries[1] = entries[1], entries[0]
    return entries


def _run(app, campaign_id: int, ops: list[dict[str, Any]], *dice: int) -> None:
    """Run effect ops on Mara with the given dice, in order."""
    with _db(app) as db:
        cx = EffectContext(rng=Rolls(*dice), combat=CAIRN.combat)
        run_effects(db, campaign_id, PC, ops, cx, "a test", plus=CAIRN.plus)


def _set_max(app, campaign_id: int, stat: str, value: int) -> None:
    with _db(app) as db:
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
    result = replace(tools, rng=Rolls(2, 1)).player_attack("Bo", "Sword")

    assert "injury" not in result


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


def test_a_scar_table_of_the_wrong_length_is_rejected():
    definition = Definition(type="injury_table", id="injuries", data={"entries": _entries(11)})
    with pytest.raises(ContentError, match="'entries' must list exactly 12 entries"):
        _load_injury_entries(definition, CAIRN.character, CAIRN.conditions, "cairn2e")


def test_a_scar_table_whose_entries_are_out_of_order_is_rejected():
    definition = Definition(type="injury_table", id="injuries", data={"entries": _swapped()})
    with pytest.raises(ContentError, match="'roll' must be 1"):
        _load_injury_entries(definition, CAIRN.character, CAIRN.conditions, "cairn2e")


def test_a_scar_naming_an_unknown_condition_is_rejected():
    entries = _entries(12)
    entries[0]["effects"] = [{"op": "condition", "id": "not_a_condition"}]
    definition = Definition(type="injury_table", id="injuries", data={"entries": entries})
    with pytest.raises(ContentError, match="names no stored condition"):
        _load_injury_entries(definition, CAIRN.character, CAIRN.conditions, "cairn2e")


def test_the_loaded_scar_table_has_twelve_entries_in_order():
    table = CAIRN.combat.injury_table
    assert table is not None
    assert table.stat == "hp"
    assert [entry.roll for entry in table.entries] == list(range(1, 13))
    assert [entry.name for entry in table.entries][:3] == [
        "Lasting Scar",
        "Rattling Blow",
        "Walloped",
    ]
    assert table.entries[-1].name == "Doomed"


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
