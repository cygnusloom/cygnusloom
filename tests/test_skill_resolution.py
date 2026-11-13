"""Loose skill, stat, feature, check, and move ids resolve to canonical ids without guessing."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from cygnusloom.content.lookup import AmbiguousId, loose_key, resolve_id
from cygnusloom.rules import wwn, wwn_magic
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import CheckMade
from tests.factories import WWN_STATS
from tests.test_wwn_advancement import (
    NAME,
    WWN,
    _campaign,
    _ctx,
    _events_of,
    _give_pick,
    _pc,
    _world_tools,
)
from tests.test_wwn_combat import _roomy_pack, _wwn_campaign
from tests.test_wwn_magic import HIGH_SPELLS, _learn, _mage


def test_loose_key_ignores_case_and_punctuation() -> None:
    assert loose_key("Sneak") == loose_key("sneak") == loose_key("Sneak!") == "sneak"
    assert loose_key("Magic Missile") == "magicmissile"


def test_an_exact_id_wins_over_a_display_name() -> None:
    table = {
        "focus_alert": "first",
        "focus_alert_2": "second",
    }
    assert (
        resolve_id(table, "focus_alert", kind="feature", names=lambda value: ("Alert",))
        == "focus_alert"
    )


def test_an_ambiguous_name_lists_the_candidates() -> None:
    table = {"focus_alert": "first", "focus_alert_2": "second"}
    with pytest.raises(AmbiguousId, match="focus_alert_2"):
        resolve_id(table, "Alert", kind="feature", names=lambda value: ("Alert",))


def test_an_unknown_id_lists_the_available_ids() -> None:
    with pytest.raises(LookupError, match="sneak"):
        resolve_id({"sneak": "skill"}, "Wizardry", kind="skill")


def test_roll_check_resolves_a_capitalized_skill_stat_and_check_label(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    result = tools.roll_check("Skill Check", NAME, skill="Sneak", stat="DEX", difficulty=8)

    assert (result["skill"], result["stat"]) == ("sneak", "dex")
    (made,) = _events_of(app, campaign_id, CheckMade.kind)
    assert (made.check, made.skill, made.stat) == ("Skill Check", "sneak", "dex")


def test_a_player_reported_check_roll_shows_its_dice_in_the_log(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    tools.roll_check(
        "skill",
        NAME,
        skill="Sneak",
        stat="DEX",
        difficulty=8,
        roll=8,
        dice_values=[3, 5],
    )

    (made,) = _events_of(app, campaign_id, CheckMade.kind)
    assert made.rolls == [3, 5]
    assert "2d6 [3, 5]" in made.render()


def test_a_capitalized_skill_still_takes_the_armor_penalty(
    client_factory: Callable[[], TestClient],
) -> None:
    _, _, raw_tools = _wwn_campaign(client_factory)
    tools = _roomy_pack(raw_tools)
    tools.give_item(NAME, "Mail Shirt")
    tools.equip_armor(NAME, "Mail Shirt")

    result = tools.roll_check("skill", NAME, skill="Sneak", stat="DEX", difficulty=8)

    assert result["armor_penalty"] == -1


def test_spend_skill_point_accepts_a_capitalized_skill(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    _give_pick(tools, "skill_points", 3)
    before = _pc(app, campaign_id).skills.get("sneak", -1)

    result = wwn.spend_skill_point(NAME, "Sneak", _ctx(tools))

    assert result["skill"] == "sneak"
    assert _pc(app, campaign_id).skills["sneak"] == before + 1


def test_improve_attribute_accepts_a_capitalized_stat(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    _give_pick(tools, "skill_points", 20)
    tools.adjust_counter(NAME, "level", 1)

    result = wwn.improve_attribute(NAME, "STR", _ctx(tools))

    assert result["stat"] == "str"
    assert _pc(app, campaign_id).stats["str"] == WWN_STATS["str"] + 1


def test_prepare_and_cast_accept_uppercase_and_display_names(
    client_factory: Callable[[], TestClient],
) -> None:
    _, _, tools, ctx = _mage(client_factory)
    spell = HIGH_SPELLS[0]
    _learn(tools, spell)
    display = WWN.character.features[spell].name.upper()

    prepared = wwn_magic.prepare_spells(NAME, [display], ctx)

    assert prepared["prepared"] == [spell]
    cast = wwn_magic.cast_spell(NAME, display, ctx)
    assert cast["spell"] == spell


def test_roll_save_accepts_a_save_display_name(
    client_factory: Callable[[], TestClient],
) -> None:
    _, _, tools = _wwn_campaign(client_factory)

    result = tools.roll_save(NAME, "Physical", "a test", natural=10)

    assert result["stat"] == "save_physical"


def test_an_exact_feature_id_wins_over_a_shared_display_name(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    feature = tools.lookup_feature("focus_alert")

    assert feature["id"] == "focus_alert"


def test_an_ambiguous_feature_name_lists_both_candidate_ids(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    with pytest.raises(InvalidInput) as raised:
        tools.lookup_feature("Alert")

    message = str(raised.value)
    assert "focus_alert" in message
    assert "focus_alert_2" in message
    assert "pass the exact id" in message

    with pytest.raises(InvalidInput, match="focus_alert_2"):
        wwn.learn_focus(NAME, "Alert", _ctx(tools))


def test_an_unknown_skill_keeps_the_helpful_listing(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)

    with pytest.raises(InvalidInput, match="there is no skill named 'Wizardry'"):
        tools.roll_check("skill", NAME, skill="Wizardry", stat="DEX")
