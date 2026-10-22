from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import DATA_DIR, ContentLibrary, _load_dice_table
from cygnusforge.db.models import Event
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.dice_tables import roll_tables
from cygnusforge.services.events import decode, events_since
from tests.test_combat import CAIRN, _fight

TABLES_ROLLED = "dice.tables_rolled"
MONSTER_TABLES = [
    "monster_appearance_physique",
    "monster_appearance_feature",
    "monster_traits_quirks",
    "monster_traits_weakness",
    "monster_attacks_type",
    "monster_attacks_critical_damage",
    "monster_abilities_ability",
    "monster_abilities_target",
]


def _definition(rows: object, dice: object = "2d6", **extra: object) -> Definition:
    data: dict[str, object] = {"type": "dice_table", "id": "test", "name": "Test", "dice": dice}
    data["rows"] = rows
    data.update(extra)
    return Definition(type="dice_table", id="test", data=data)


def _event_count(db: Session, campaign_id: int) -> int:
    return db.exec(
        select(func.count()).select_from(Event).where(Event.campaign_id == campaign_id)
    ).one()


def test_the_real_cairn_rule_set_loads_its_dice_tables():
    tables = ContentLibrary.load(DATA_DIR).rule_sets["cairn2e"].dice_tables
    assert {"reaction", "die_of_fate", *MONSTER_TABLES} <= set(tables)
    assert tables["reaction"].dice == "2d6"


def test_cairn_reaction_table_maps_rolls_to_results():
    reaction = ContentLibrary.load(DATA_DIR).rule_sets["cairn2e"].dice_tables["reaction"]
    assert reaction.row_for(2).result == "Hostile"
    assert reaction.row_for(7).result == "Curious"
    assert reaction.row_for(12).result == "Helpful"


@pytest.mark.parametrize("table_id", MONSTER_TABLES)
def test_cairn_monster_tables_have_twenty_rows_covering_one_to_twenty(table_id):
    table = ContentLibrary.load(DATA_DIR).rule_sets["cairn2e"].dice_tables[table_id]
    assert table.dice == "1d20"
    assert len(table.rows) == 20
    assert [(row.low, row.high) for row in table.rows] == [(n, n) for n in range(1, 21)]


BAD_TABLES = {
    "dice-without-count": ({"dice": "d6", "rows": [{"roll": "1-6", "result": "x"}]}),
    "dice-with-trailing-text": ({"dice": "1d6x", "rows": [{"roll": "1-6", "result": "x"}]}),
    "gap-in-rows": ({"rows": [{"roll": "2", "result": "a"}, {"roll": "4-12", "result": "b"}]}),
    "overlapping-rows": (
        {"rows": [{"roll": "2-4", "result": "a"}, {"roll": "4-12", "result": "b"}]}
    ),
    "rows-stop-short-of-top": ({"rows": [{"roll": "2-11", "result": "a"}]}),
    "backwards-range": (
        {"rows": [{"roll": "5-3", "result": "a"}, {"roll": "6-12", "result": "b"}]}
    ),
    "extra-key-in-row": ({"rows": [{"roll": "2-12", "result": "a", "note": "extra"}]}),
    "empty-result": ({"rows": [{"roll": "2-12", "result": ""}]}),
    "whitespace-result": ({"rows": [{"roll": "2-12", "result": "   "}]}),
    "bad-roll-string": ({"rows": [{"roll": "two", "result": "a"}]}),
    "empty-rows-list": ({"rows": []}),
}


BAD_TABLE_MESSAGES = {
    "dice-without-count": "'dice' must be one dice formula",
    "dice-with-trailing-text": "'dice' must be one dice formula",
    "gap-in-rows": "without gaps or overlaps",
    "overlapping-rows": "without gaps or overlaps",
    "rows-stop-short-of-top": "must cover every roll up to 12",
    "backwards-range": "runs backwards",
    "extra-key-in-row": "needs 'roll' and 'result' and no other keys",
    "empty-result": "'result' must be non-empty text",
    "whitespace-result": "'result' must be non-empty text",
    "bad-roll-string": "'roll' must be a number or a range",
    "empty-rows-list": "'rows' must be a non-empty list",
}


@pytest.mark.parametrize("case", sorted(BAD_TABLES))
def test_a_malformed_dice_table_is_rejected(case):
    with pytest.raises(ContentError, match=BAD_TABLE_MESSAGES[case]):
        _load_dice_table(_definition(**BAD_TABLES[case]), "test")


def test_a_well_formed_dice_table_loads_from_a_definition():
    table = _load_dice_table(
        _definition(
            [
                {"roll": "2", "result": "a"},
                {"roll": "3-11", "result": "b"},
                {"roll": "12", "result": "c"},
            ]
        ),
        "test",
    )
    assert table.dice == "2d6"
    assert table.row_for(7).result == "b"
    assert table.row_for(12).result == "c"


def test_roll_tables_returns_one_roll_per_named_table_and_logs_one_event(client_factory):
    _, campaign_id, tools = _fight(client_factory)
    tables = CAIRN.dice_tables
    with Session(tools.engine) as db:
        last_id = max((row.id for row in events_since(db, campaign_id)), default=0)
        before = _event_count(db, campaign_id)
        out = roll_tables(db, campaign_id, ["reaction", "die_of_fate"], tables, random.Random(1))
        after = _event_count(db, campaign_id)
        new_events = events_since(db, campaign_id, after=last_id)

    rolls = out["rolls"]
    assert [roll["table"] for roll in rolls] == ["reaction", "die_of_fate"]
    assert set(rolls[0]) == {"table", "name", "roll", "result"}
    assert 2 <= rolls[0]["roll"] <= 12
    assert 1 <= rolls[1]["roll"] <= 6
    for roll in rolls:
        assert roll["result"] == tables[roll["table"]].row_for(roll["roll"]).result
    assert after == before + 1
    assert len(new_events) == 1

    (event,) = [row for row in new_events if row.kind == TABLES_ROLLED]
    rendered = decode(event).render()
    assert "Reaction" in rendered
    assert "Die of Fate" in rendered
    assert decode(event).rolls == rolls


def test_roll_tables_refuses_an_empty_list(client_factory):
    _, campaign_id, tools = _fight(client_factory)
    with Session(tools.engine) as db:
        before = _event_count(db, campaign_id)
        with pytest.raises(InvalidInput, match="at least one"):
            roll_tables(db, campaign_id, [], CAIRN.dice_tables, random.Random(1))
        assert _event_count(db, campaign_id) == before


def test_roll_tables_refuses_an_unknown_id_without_logging_or_rolling(client_factory):
    _, campaign_id, tools = _fight(client_factory)
    rng = random.Random(1)
    state_before = rng.getstate()
    with Session(tools.engine) as db:
        before = _event_count(db, campaign_id)
        with pytest.raises(InvalidInput, match="'nope'"):
            roll_tables(db, campaign_id, ["reaction", "nope"], CAIRN.dice_tables, rng)
        assert _event_count(db, campaign_id) == before
    assert rng.getstate() == state_before


def test_the_same_seed_gives_the_same_rolls(client_factory):
    _, campaign_id, tools = _fight(client_factory)
    with Session(tools.engine) as db:
        first = roll_tables(
            db, campaign_id, ["reaction", "die_of_fate"], CAIRN.dice_tables, random.Random(42)
        )
        second = roll_tables(
            db, campaign_id, ["reaction", "die_of_fate"], CAIRN.dice_tables, random.Random(42)
        )
    assert first["rolls"] == second["rolls"]


def test_world_tools_roll_tables_returns_the_rolls_shape(client_factory: Callable[[], TestClient]):
    _, _, tools = _fight(client_factory)
    live = replace(tools, dice_tables=CAIRN.dice_tables, rng=random.Random(1))
    out = live.roll_tables(["reaction"])
    assert list(out) == ["rolls"]
    (roll,) = out["rolls"]
    assert roll["table"] == "reaction"
    assert roll["name"] == "Reaction"
    assert CAIRN.dice_tables["reaction"].row_for(roll["roll"]).result == roll["result"]
