"""The Worlds Without Number rule set carries no content reserved by the free book.

The CC0 SRD grants the mechanics, the class and spell names, the equipment, and
two example backgrounds. It reserves the setting, the creatures, the starting
packages, the nonhuman origins, and the GM tables of the free book. These tests
hold the line: no reserved name survives in the bundled content, the tables are
the project's own, and the GM has a way to write the invented setting down.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cygnusforge.content.library import ContentLibrary
from cygnusforge.services import views
from cygnusforge.services.common import InvalidInput
from tests import factories
from tests.conftest import content_library
from tests.test_wwn_advancement import NAME, _campaign, _world_tools

DATA = Path(__file__).resolve().parent.parent / "src" / "cygnusforge" / "data"
RULE_SET = DATA / "rulesets" / "worldswithoutnumber"
CORE = DATA / "rulesets" / "core"
WORLD = DATA / "worlds" / "unwritten-world"

# Words that belong to the setting, the peoples, and the institutions the SRD
# explicitly reserves. A hit means free-book material has crept back in.
RESERVED = re.compile(
    r"latter[\s-]earth|arratu|academ(?:y|ies)|"
    r"\banak\b|\bdrudge\b|\bhalfman\b|\bhouri\b|xenoformed|"
    r"black brass legion",
    re.IGNORECASE,
)

# Creature names that appear only in the free book's bestiary.
FREE_BOOK_CREATURES = {
    "ashcrawler",
    "ashcrawler_slave",
    "anak_courser",
    "betrayer_bird",
    "bull_pig",
    "dunecrawler",
    "fleshbeast",
    "horse_lizard",
    "judas_goat",
    "mountain_crab",
    "razorhorn",
    "salt_devil",
    "wailer_crane",
    "polop_hellsquid",
    "polop_hunter",
    "great_anak_warchief",
    "anak_king",
    "war_drudge",
    "houri_concubine",
    "revanant_king",
    "revenant_king",
    "brass_scytheman",
}

CREATIVE_TABLES = {
    "setting_premise",
    "region_identity",
    "faction",
    "conflict",
    "mystery",
    "adventure_hook",
}


def _wwn() -> ContentLibrary:
    return content_library()


def test_no_reserved_setting_text_survives() -> None:
    found: list[str] = []
    paths = sorted(RULE_SET.glob("*.json")) + sorted(WORLD.glob("*.json"))
    paths += sorted(CORE.glob("*.json"))
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in RESERVED.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{path.name}:{line}: {match.group(0)}")
    assert found == [], "reserved free-book text remains:\n" + "\n".join(found)


def test_bestiary_creatures_are_original() -> None:
    monsters = _wwn().rule_sets["worldswithoutnumber"].monsters
    assert monsters, "the bestiary must not be empty"
    assert FREE_BOOK_CREATURES.isdisjoint(monsters), "free-book creatures remain: " + ", ".join(
        sorted(FREE_BOOK_CREATURES & set(monsters))
    )
    stat_line = re.compile(r"\(HD (\d+);")
    for monster in monsters.values():
        match = stat_line.search(monster.description)
        assert match is not None, f"{monster.id} has no stat line"


def test_the_bestiary_spans_the_hit_dice_range() -> None:
    monsters = _wwn().rule_sets["worldswithoutnumber"].monsters
    hit_dice = {
        int(match.group(1))
        for monster in monsters.values()
        if (match := re.search(r"\(HD (\d+);", monster.description))
    }
    assert {1, 2, 3, 4, 5, 6}.issubset(hit_dice)
    assert max(hit_dice) >= 10


def test_creative_tables_roll_a_hundred_entries_each() -> None:
    tables = _wwn().rule_sets["worldswithoutnumber"].dice_tables
    creative = {table.id: table for table in tables.values() if table.creative}
    assert CREATIVE_TABLES.issubset(creative)
    for table_id in CREATIVE_TABLES:
        table = creative[table_id]
        assert table.dice == "1d100", f"{table_id} rolls {table.dice}"
        assert len(table.rows) == 100, f"{table_id} has {len(table.rows)} entries"
        assert len({row.result.casefold() for row in table.rows}) == 100, (
            f"{table_id} repeats an entry"
        )


def test_every_dice_table_is_filled_for_its_dice() -> None:
    rules = _wwn().rule_sets["worldswithoutnumber"]
    for table in rules.dice_tables.values():
        rolls = {roll for row in table.rows for roll in range(row.low, row.high + 1)}
        if table.dice == "2d6":
            expected = set(range(2, 13))
        else:
            expected = set(range(1, int(table.dice.removeprefix("1d")) + 1))
        assert rolls == expected, f"{table.id} does not cover {table.dice}"


def test_kits_arm_every_character_and_leave_none_unarmed() -> None:
    rules = _wwn().rule_sets["worldswithoutnumber"]
    kits = [t for t in rules.character.templates.values() if t.kind == "kit"]
    assert len(kits) == 6
    weapons = {item.name for item in rules.marketplace.items if item.category == "weapon"}
    for kit in kits:
        assert kit.grant is not None, f"{kit.id} grants nothing"
        assert weapons & set(kit.grant.items), f"{kit.id} grants no weapon"


def test_recording_a_setting_fact_reaches_the_world_state(
    client_factory,
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    first = tools.record_setting("The starting region", "A river valley of oath-bound clans.")
    assert first["recorded"] == "The starting region"
    assert first["facts"] == 1

    second = tools.record_setting("the starting region", "A highland of herders and old stone.")
    assert second["replaced"] == "A river valley of oath-bound clans."
    assert second["facts"] == 1

    state = factories.open_campaign(app, campaign_id)
    assert state.settings == {"The starting region": "A highland of herders and old stone."}

    rules = _wwn().rule_sets["worldswithoutnumber"]
    text = views.world_state_text(state, rules.campaign)
    assert "Setting:" in text
    assert "A highland of herders and old stone." in text


def test_recording_several_setting_facts_takes_one_call(client_factory) -> None:
    """The opening writes six or more facts; one round per fact is the difference between a
    three-minute opening and a long one."""
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    result = tools.record_settings(
        [
            {"topic": "The world", "detail": "A river kingdom."},
            {"topic": "Magic", "detail": "Learned from old books."},
            {"topic": "The world", "detail": "A river kingdom of oath-bound clans."},
        ]
    )

    assert result["recorded"] == ["The world", "Magic", "The world"]
    assert result["facts"] == 2
    state = factories.open_campaign(app, campaign_id)
    assert state.settings == {
        "The world": "A river kingdom of oath-bound clans.",
        "Magic": "Learned from old books.",
    }
    with pytest.raises(InvalidInput):
        tools.record_settings([])


def test_setting_facts_do_not_leak_between_campaigns(client_factory) -> None:
    client = factories.signed_in_client(client_factory)
    first_id = factories.create_campaign(
        client, label="First", world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    second_id = factories.create_campaign(
        client, label="Second", world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    tools = factories.tools_for(client.app, first_id, rule_set="worldswithoutnumber")
    tools.record_setting("Magic", "Sorcery is learned from old books and costs the reader sleep.")

    first = factories.open_campaign(client.app, first_id)
    second = factories.open_campaign(client.app, second_id)
    assert "Magic" in first.settings
    assert second.settings == {}


def test_the_blank_world_carries_no_setting_of_its_own() -> None:
    library = _wwn()
    world = library.world_templates["unwritten-world"]
    assert world.rule_set is None
    assert not world.locations
    assert not world.npcs
    assert not world.definitions
    document = json.loads((WORLD / "world.json").read_text(encoding="utf-8"))
    assert [entry["id"] for entry in document] == ["unwritten-world"]


def test_a_gm_turn_invents_the_setting_with_the_tools(client_factory) -> None:
    """The whole loop: the GM rolls the idea tables, writes the world down, and then sees it."""
    app, campaign_id = _campaign(client_factory)
    # A second client for the same signed-in player, so the campaign the helper built is reachable.
    client = factories.signed_in_client(client_factory)
    # The opening turn is pending until the player answers it, as in any new campaign.
    factories.answer(client, campaign_id, "continue")
    tools = _world_tools(app, campaign_id)
    tools.create_location(name="Crossroads", description="A road junction with an old well.")
    tools.move_actor(NAME, "Crossroads")

    script = "__FAKE__:" + json.dumps(
        {
            "turns": [
                {
                    "rounds": [
                        [
                            {"name": "roll_creative", "arguments": {}},
                            {
                                "name": "record_setting",
                                "arguments": {
                                    "topic": "The world",
                                    "detail": "A land of river kingdoms built on older stone.",
                                },
                            },
                        ]
                    ],
                    "text": "The world takes shape around the crossroads.",
                }
            ]
        }
    )
    response = factories.send_message(client, campaign_id, script)
    assert response.status_code in (200, 303), response.text

    state = factories.open_campaign(client.app, campaign_id)
    assert state.settings["The world"] == "A land of river kingdoms built on older stone."
    rules = _wwn().rule_sets["worldswithoutnumber"]
    text = views.world_state_text(state, rules.campaign)
    assert "A land of river kingdoms built on older stone." in text
