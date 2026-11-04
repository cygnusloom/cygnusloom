"""The generic-engine refactors the Worlds Without Number ruleset needed: inventory capacity from a
stat, side initiative, filtered tool-call enums, and shop armor that can set rather than stack."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import (
    ContentLibrary,
    InitiativeRules,
    Marketplace,
    ShopItem,
    _load_marketplace,
    _load_tool_call,
)
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.services import campaigns as campaigns_service
from cygnusforge.services import encounters, inventory, market
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    ArmorSet,
    ChatGM,
    CurrencyChanged,
    ItemRemoved,
    load_state,
    record,
)
from cygnusforge.services.views import scene_view, world_state_text
from cygnusforge.services.world import WorldTools
from tests.conftest import content_library
from tests.factories import WWN_STATS, pinned_stats
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_character_creation import (
    _cairn_campaign,
    _choices,
    _complete_form,
    _named_picks,
    _picks,
    _rolled_stats,
)
from tests.test_combat import CAIRN, PC, Rolls, _character, _fight_client, _state, _tools

LIBRARY: ContentLibrary = content_library()
WWN = LIBRARY.rule_sets["worldswithoutnumber"]
# The side initiative block the WWN combat JSON declares. The content worker owns that file, so the
# test supplies it here to exercise the engine support.
SIDE = replace(
    WWN.combat,
    initiative=InitiativeRules(die="1d8", bonus_stat="dex", side=True, tie="party"),
)
MAP = "Yard Green"
YARD = "Stonegate Yard"
WREN = "Wren"


def _wwn_campaign(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int]:
    """A Worlds Without Number campaign with its player character created."""
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    with pinned_stats(WWN_STATS):
        page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields.update(_picks(page))
    fields = _named_picks(fields)
    created = _create_character(client, campaign_id, name=WREN, fields=fields)
    assert created.status_code == 303, created.text
    return client, campaign_id


def _wwn_tools(app: FastAPI, campaign_id: int, rules=None) -> WorldTools:
    return WorldTools(
        engine=app.state.engine,
        campaign_id=campaign_id,
        tables=LIBRARY.core.tables,
        rng=random.Random(0),
        character=WWN.character,
        rule_data=WWN.rule_data,
        plus=WWN.plus,
        inventory=rules or WWN.inventory,
        item_sizes=WWN.item_sizes,
    )


def _wwn_pc(client: TestClient, campaign_id: int):
    with Session(client.app.state.engine) as db:
        return load_state(db, campaign_id).characters[WREN.casefold()]


def _empty_pack(client: TestClient, campaign_id: int) -> None:
    """Drop everything the character carries. Every character starts with the kit the creation
    form gives, so a test that measures what a pack can take empties it first."""
    with Session(client.app.state.engine) as db:
        for held in list(load_state(db, campaign_id).characters[WREN.casefold()].items):
            inventory.drop_item(db, campaign_id, WWN.inventory, WREN, held.name)
        db.commit()


# --- 1. Inventory capacity from a stat ------------------------------------------------------


def test_wwn_capacity_is_the_strength_score(client_factory: Callable[[], TestClient]):
    client, campaign_id = _wwn_campaign(client_factory)
    pc = _wwn_pc(client, campaign_id)
    rules = replace(
        WWN.inventory,
        pools=(),
        size_pools={},
        default_pool="",
        capacity_stat="str",
        capacity_multiplier=1,
    )
    assert rules.capacity_multiplier == 1
    assert inventory.capacity_of(pc, rules, WWN.character) == pc.stats["str"]


def test_a_capacity_multiplier_scales_the_score(client_factory: Callable[[], TestClient]):
    client, campaign_id = _wwn_campaign(client_factory)
    pc = _wwn_pc(client, campaign_id)
    rules = replace(
        WWN.inventory,
        pools=(),
        size_pools={},
        default_pool="",
        capacity_stat="str",
        capacity_multiplier=2,
    )
    assert inventory.capacity_of(pc, rules, WWN.character) == pc.stats["str"] * 2


def test_carrying_and_dropping_report_the_stat_based_capacity(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _wwn_campaign(client_factory)
    pc = _wwn_pc(client, campaign_id)
    single_pool = replace(
        WWN.inventory,
        pools=(),
        size_pools={},
        default_pool="",
        capacity_stat="str",
        capacity_multiplier=1,
    )
    tools = _wwn_tools(client.app, campaign_id, single_pool)
    carried = tools.give_item(WREN, "Rope")
    assert carried["capacity"] == pc.stats["str"]
    dropped = tools.drop_item(WREN, "Rope")
    assert dropped["capacity"] == pc.stats["str"]


def test_a_buy_is_measured_against_the_stat_based_capacity(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _wwn_campaign(client_factory)
    pc = _wwn_pc(client, campaign_id)
    rules = replace(
        WWN.inventory,
        pools=(),
        size_pools={},
        default_pool="",
        capacity_stat="str",
        capacity_multiplier=1,
    )
    marketplace = Marketplace(
        currency="silver",
        items=(
            ShopItem(
                id="rope",
                name="Rope",
                category="gear",
                price=2,
                size="normal",
                armor=0,
                damage="",
                uses=0,
                description="Fifty feet of hemp rope.",
            ),
        ),
        services=(),
        hirelings=(),
    )
    _empty_pack(client, campaign_id)
    with Session(client.app.state.engine) as db:
        record(
            db,
            campaign_id,
            CurrencyChanged(name=WREN, feature="silver", amount=10, cause="a test"),
        )
        db.commit()
        result = market.buy_item(db, campaign_id, marketplace, rules, WWN.item_sizes, WREN, "Rope")
    assert result["capacity"] == pc.stats["str"]
    assert result["currency"] == "silver"
    assert result["currency_left"] == 8


def test_cairn_keeps_its_fixed_capacity(client_factory: Callable[[], TestClient]):
    client, campaign_id, tools = _fight_client(client_factory)
    pc = _character(client.app, campaign_id, PC)
    assert CAIRN.inventory.capacity_stat == ""
    assert CAIRN.inventory.full_stat == "hp"
    assert inventory.capacity_of(pc, CAIRN.inventory, CAIRN.character) == CAIRN.inventory.capacity
    result = tools.give_item(PC, "Lantern")
    assert result["capacity"] == CAIRN.inventory.capacity


def test_taking_an_object_uses_stat_based_capacity(client_factory: Callable[[], TestClient]):
    client, campaign_id = _wwn_campaign(client_factory)
    rules = replace(
        WWN.inventory,
        # The test measures the stat-based capacity alone, so it drops the Readied and Stowed
        # pools, which the character's starting kit would otherwise fill.
        pools=(),
        size_pools={},
        default_pool="",
        capacity=0,
        capacity_stat="str",
        capacity_multiplier=100,
    )
    tools = _wwn_tools(client.app, campaign_id, rules)
    location = "Capacity Yard"
    tools.create_location(location, "A test yard.")
    tools.move_actor(WREN, location=location)

    tools.place_object(location, "a rope", False, "a test", item="Rope")
    result = tools.take_object("a rope", WREN, "a test")

    assert result["item"] == "Rope"
    assert any(item.name == "Rope" for item in _wwn_pc(client, campaign_id).items)


def test_world_and_pack_displays_use_stat_based_capacity(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _wwn_campaign(client_factory)
    rules = replace(WWN.inventory, capacity_stat="str", capacity_multiplier=1)
    state = _state(client.app, campaign_id)
    pc = state.pc()
    assert pc is not None
    capacity = inventory.capacity_of(pc, rules, WWN.character)
    used = inventory.used_slots(pc)

    world = world_state_text(state, WWN.campaign, inventory=rules)
    pack = scene_view(state, WWN.character, rules).pack

    expected_line = f"Inventory: {used} of {capacity} slots"
    if used > capacity:
        expected_line += " (over capacity)"
    assert expected_line in world
    assert pack is not None
    assert pack.capacity == capacity


def test_non_wwn_rule_set_can_draw_a_character_table(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _fight_client(client_factory)
    library = client.app.state.content
    rule_set = library.rule_sets["cairn2e"]
    context = campaigns_service._tool_context(
        client.app.state.engine,
        random.Random(0),
        FakeProvider(),
        "fake/scripted",
        campaign_id,
        rule_set,
        library.world_content("twin-lakes", rule_set.id),
    )
    table = next(iter(sorted(rule_set.tables)))
    expected = random.Random(0).choice(context.world.tables[table]).value

    assert context.world.draw_character_table(table) == expected


def test_a_full_inventory_drains_nothing_when_the_rule_set_names_no_full_stat(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _wwn_campaign(client_factory)
    items = [item.name for item in _wwn_pc(client, campaign_id).items]
    rules = replace(WWN.inventory, capacity=1, full_stat="")
    tools = _wwn_tools(client.app, campaign_id, rules)
    with Session(client.app.state.engine) as db:
        for item in items:
            record(db, campaign_id, ItemRemoved(name=WREN, item=item))
        db.commit()
    result = tools.give_item(WREN, "Rope")
    assert result["slots_used"] == 1
    assert "drained" not in result


# --- 2. Side initiative ---------------------------------------------------------------------


def _side_encounter(
    client_factory: Callable[[], TestClient], *foes: tuple[str, int]
) -> tuple[TestClient, int, WorldTools]:
    """A campaign with a map and a side-initiative encounter. The foes join before the player
    character, so no player turn opens before the side roll. Each foe is a name and a Dexterity
    score."""
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303, created.text
    app = client.app
    with Session(app.state.engine) as db:
        # Close the campaign's opening turn so an NPC's turn may begin.
        record(db, campaign_id, ChatGM(content="The story begins.", model="test"))
        db.commit()
    tools = _tools(app, campaign_id)
    tools.create_location(YARD, "Cobbles around a dry well.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(MAP, YARD, 10, 10, "Open cobbles.")
    with Session(app.state.engine) as db:
        result = encounters.start_encounter(
            db,
            campaign_id,
            MAP,
            rolls_initiative=True,
            combat=SIDE,
            character=WWN.character,
            rng=random.Random(0),
        )
    assert result["initiative"] == "sides"
    for name, dex in foes:
        tools.create_monster(
            name,
            YARD,
            description="A foe.",
            stats={"hp": 6, "str": 10, "dex": dex, "wil": 10},
            attacks=[{"name": "Claw", "dice": "1d6"}],
            force=True,
        )
        tools.add_participant(name)
    tools.add_participant(PC)
    return client, campaign_id, tools


def _side(rolls: list[dict[str, Any]], side: str) -> dict[str, Any]:
    return next(entry for entry in rolls if entry["side"] == side)


def test_side_initiative_asks_no_player_character_for_a_save(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _side_encounter(client_factory, ("Wolf", 10))
    state = _state(client.app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.rolls_initiative is False
    assert encounters.awaiting_initiative(state) == []
    assert encounters.initiative_open(state) is False
    with Session(client.app.state.engine) as db:
        # An NPC's turn may begin without waiting for initiative rolls.
        assert encounters.begin_turn(db, campaign_id, "Wolf").name == "Wolf"


def test_side_initiative_rolls_once_per_side(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _side_encounter(client_factory, ("Wolf", 10))
    pc = _character(client.app, campaign_id, PC)
    bonus = WWN.character.stats["dex"].modifier(pc.stats["dex"])
    with Session(client.app.state.engine) as db:
        result = encounters.roll_side_initiative(db, campaign_id, SIDE, WWN.character, Rolls(1, 8))
    party = _side(result["rolls"], "party")
    foes = _side(result["rolls"], "foes")
    assert len(result["rolls"]) == 2
    assert party["roll"] == 8 and party["bonus"] == bonus
    assert foes["roll"] == 1 and foes["bonus"] == 0
    assert result["order"] == [PC, "Wolf"]


def test_a_side_uses_the_best_modifier_among_its_members(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _side_encounter(client_factory, ("Wolf", 10), ("Quick", 18))
    with Session(client.app.state.engine) as db:
        result = encounters.roll_side_initiative(db, campaign_id, SIDE, WWN.character, Rolls(5, 2))
    foes = _side(result["rolls"], "foes")
    assert foes["bonus"] == 2
    assert foes["members"] == ["Wolf", "Quick"]


def test_a_tie_at_side_initiative_goes_to_the_party(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _side_encounter(client_factory, ("Wolf", 10))
    pc = _character(client.app, campaign_id, PC)
    bonus = WWN.character.stats["dex"].modifier(pc.stats["dex"])
    with Session(client.app.state.engine) as db:
        result = encounters.roll_side_initiative(
            db, campaign_id, SIDE, WWN.character, Rolls(6 + bonus, 6)
        )
    assert _side(result["rolls"], "party")["total"] == _side(result["rolls"], "foes")["total"]
    assert result["order"] == [PC, "Wolf"]


def test_a_tie_can_be_given_to_the_foes_by_the_rule_set(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _side_encounter(client_factory, ("Wolf", 10))
    pc = _character(client.app, campaign_id, PC)
    bonus = WWN.character.stats["dex"].modifier(pc.stats["dex"])
    assert SIDE.initiative is not None
    combat = replace(SIDE, initiative=replace(SIDE.initiative, tie="foes"))
    with Session(client.app.state.engine) as db:
        result = encounters.roll_side_initiative(
            db, campaign_id, combat, WWN.character, Rolls(6 + bonus, 6)
        )
    assert result["order"] == ["Wolf", PC]


def test_cairn_keeps_its_individual_initiative_saves(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _fight_client(client_factory, rolled=False)
    state = _state(client.app, campaign_id)
    assert CAIRN.combat is not None and CAIRN.combat.initiative is None
    assert encounters.side_initiative(CAIRN.combat) is None
    assert state.encounter is not None and state.encounter.rolls_initiative is True
    assert encounters.awaiting_initiative(state) == [PC]


def test_an_encounter_started_without_side_rules_opens_no_side_roll(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303, created.text
    tools = _tools(client.app, campaign_id)
    tools.create_location(YARD, "Cobbles.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(MAP, YARD, 10, 10, "Open.")
    with Session(client.app.state.engine) as db:
        result = encounters.start_encounter(
            db, campaign_id, MAP, rolls_initiative=True, combat=CAIRN.combat
        )
    assert "initiative" not in result
    state = _state(client.app, campaign_id)
    assert state.encounter is not None and state.encounter.rolls_initiative is True


def test_side_initiative_is_refused_when_the_rule_set_has_none(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id, _ = _side_encounter(client_factory, ("Wolf", 10))
    with Session(client.app.state.engine) as db, pytest.raises(InvalidInput):
        encounters.roll_side_initiative(db, campaign_id, CAIRN.combat)


# --- 3. Filtered enum_from ------------------------------------------------------------------


def _cast_definition(enum_source: object) -> Definition:
    return Definition(
        type="tool_call",
        id="cast_spell",
        data={
            "type": "tool_call",
            "id": "cast_spell",
            "name": "cast_spell",
            "python_module": "test_wwn_refactor",
            "function": "cast_spell",
            "description": "Cast a spell.",
            "parameters": {
                "type": "object",
                "properties": {
                    "spell": {"type": "string", "enum_from": enum_source},
                },
                "required": ["spell"],
            },
        },
    )


def cast_spell(spell: str, ctx: Any = None) -> dict[str, Any]:
    return {"spell": spell}


def _enum_sources() -> dict[str, list[str]]:
    return {
        "features": sorted(WWN.character.features),
        "skills": sorted(WWN.character.skills),
        "checks": sorted(WWN.checks),
    }


def _tool_enum(enum_source: object) -> list[str]:
    module = ModuleType("test_wwn_refactor")
    module.cast_spell = cast_spell
    call = _load_tool_call(
        _cast_definition(enum_source),
        {"test_wwn_refactor": module},
        WWN.character,
        "worldswithoutnumber",
        {},
        enum_sources=_enum_sources(),
    )
    return call.parameters["properties"]["spell"]["enum"]


def test_a_filtered_feature_enum_lists_only_matching_features():
    enum = _tool_enum({"source": "features", "feature_type": "spell", "tag": "high", "max_rank": 5})
    expected = sorted(
        feature.id
        for feature in WWN.character.features.values()
        if feature.feature_type == "spell" and feature.tag == "high" and feature.rank <= 5
    )
    assert enum == expected
    assert enum
    assert all(WWN.character.features[feature].feature_type == "spell" for feature in enum)


def test_a_filtered_feature_enum_can_limit_the_rank():
    all_high = _tool_enum({"source": "features", "tag": "high"})
    first_rank = _tool_enum({"source": "features", "tag": "high", "max_rank": 1})
    assert first_rank
    assert all(WWN.character.features[feature].rank <= 1 for feature in first_rank)
    assert len(first_rank) < len(all_high)


def test_skills_and_checks_are_enum_sources():
    assert _tool_enum({"source": "skills"}) == sorted(WWN.character.skills)
    assert _tool_enum({"source": "checks"}) == sorted(WWN.checks)


def test_the_string_form_of_enum_from_still_lists_every_feature():
    assert _tool_enum("features") == sorted(WWN.character.features)


def test_a_filter_needs_a_feature_source():
    with pytest.raises(ContentError):
        _tool_enum({"source": "skills", "tag": "focus"})


def test_an_unknown_enum_filter_is_refused():
    with pytest.raises(ContentError):
        _tool_enum({"source": "features", "rarity": "rare"})


def test_an_unknown_feature_type_is_refused():
    with pytest.raises(ContentError):
        _tool_enum({"source": "features", "feature_type": "ritual"})


def test_the_existing_rule_sets_still_fill_their_string_enums():
    hogwarts = LIBRARY.rule_sets["hogwarts-rpg"]
    move = hogwarts.tool_calls["roll_move"].parameters["properties"]["move"]
    assert move["enum"] == sorted(hogwarts.moves)
    assert move["enum"]
    assert LIBRARY.rule_sets["cairn2e"].tool_calls


# --- 4. Marketplace currency and armor -------------------------------------------------------


def test_a_marketplace_can_trade_in_any_currency_feature(client_factory: Callable[[], TestClient]):
    client, campaign_id = _wwn_campaign(client_factory)
    marketplace = Marketplace(
        currency="silver",
        items=(
            ShopItem(
                id="rope",
                name="Rope",
                category="gear",
                price=2,
                size="normal",
                armor=0,
                damage="",
                uses=0,
                description="Fifty feet of hemp rope.",
            ),
        ),
        services=(),
        hirelings=(),
    )
    _empty_pack(client, campaign_id)
    with Session(client.app.state.engine) as db:
        record(
            db,
            campaign_id,
            CurrencyChanged(name=WREN, feature="silver", amount=10, cause="a test"),
        )
        db.commit()
        result = market.buy_item(
            db, campaign_id, marketplace, WWN.inventory, WWN.item_sizes, WREN, "Rope"
        )
        assert load_state(db, campaign_id).characters[WREN.casefold()].features["silver"] == 8
    assert result["currency"] == "silver"
    assert result["currency_left"] == 8


def _armor_shop(mode: str, armor: int) -> Marketplace:
    return Marketplace(
        currency=CAIRN.marketplace.currency,
        items=(
            ShopItem(
                id="mail",
                name="Mail",
                category="armor",
                price=5,
                size="normal",
                armor=armor,
                damage="",
                uses=0,
                description="A coat of mail.",
                armor_mode=mode,
            ),
        ),
        services=(),
        hirelings=(),
    )


@pytest.mark.parametrize(
    ("mode", "listed", "before", "after"),
    [
        ("add", 2, 3, 5),
        ("set", 2, 3, 2),
        ("max", 2, 3, 3),
        ("max", 4, 3, 4),
    ],
)
def test_shop_armor_can_add_set_or_keep_the_best(
    client_factory: Callable[[], TestClient], mode: str, listed: int, before: int, after: int
):
    client, campaign_id, _ = _fight_client(client_factory)
    with Session(client.app.state.engine) as db:
        record(db, campaign_id, ArmorSet(name=PC, armor=before))
        record(
            db,
            campaign_id,
            CurrencyChanged(
                name=PC, feature=CAIRN.marketplace.currency, amount=100, cause="a test"
            ),
        )
        db.commit()
        result = market.buy_item(
            db,
            campaign_id,
            _armor_shop(mode, listed),
            CAIRN.inventory,
            CAIRN.item_sizes,
            PC,
            "Mail",
            armor_max=0,
        )
        assert result["armor"] == after
        assert load_state(db, campaign_id).characters[PC.casefold()].armor == after


def _shop_definitions(item: dict[str, Any]) -> list[Definition]:
    return [
        Definition(
            type="marketplace",
            id="shop",
            data={"type": "marketplace", "id": "shop", "currency": "gold"},
        ),
        Definition(
            type="shop_item",
            id="mail",
            data={
                "type": "shop_item",
                "id": "mail",
                "name": "Mail",
                "category": "armor",
                "price": 5,
                "size": "normal",
                "armor": 2,
                "uses": 0,
                **item,
            },
        ),
    ]


def test_a_shop_item_loads_its_armor_mode():
    shop = _load_marketplace(
        _shop_definitions({"armor_mode": "set"}), {"normal": 1}, CAIRN.character, "cairn2e"
    )
    assert shop is not None
    assert shop.items[0].armor_mode == "set"


def test_a_default_shop_item_still_adds_armor():
    shop = _load_marketplace(_shop_definitions({}), {"normal": 1}, CAIRN.character, "cairn2e")
    assert shop is not None
    assert shop.items[0].armor_mode == "add"
