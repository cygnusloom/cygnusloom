"""Worlds Without Number magic: preparation, castings, Effort, System Strain, and first aid.

The campaign and character helpers come from `tests/test_wwn_advancement.py`; they create the
character through the same web form helpers as `tests/test_wwn.py`.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.character import derived_stats
from cygnusloom.rules import wwn, wwn_magic
from cygnusloom.rules.wwn_progression import require_wwn_progression
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import CheckMade, SpellCast, load_state
from tests.test_wwn_advancement import (
    LIBRARY,
    NAME,
    WWN,
    _campaign,
    _ctx,
    _events_of,
    _pc,
    _stat,
    _world_tools,
)

# Four level-1 High Magic spells, all castable by a level-1 mage.
HIGH_SPELLS = (
    "spell_apprehending_arcane_form",
    "spell_cognitive_supersession_inferior_orders",
    "spell_coruscating_coffin",
    "spell_damnation_of_the_sense",
)


def _mage(
    client_factory: Callable[[], TestClient],
    *,
    class_id: str = "mage",
    seed: int = 0,
):
    app, campaign_id = _campaign(client_factory, class_id=class_id, tradition="high_mage")
    tools = _world_tools(app, campaign_id, seed=seed)
    return app, campaign_id, tools, _ctx(tools)


def _learn(tools: Any, *spells: str) -> None:
    ctx = _ctx(tools)
    with Session(tools.engine) as db:
        known = set(load_state(db, tools.campaign_id).pc().features)
    for spell in spells:
        if spell not in known:
            wwn.learn_spell(NAME, spell, ctx)


def test_prepare_spells_enforces_the_prepared_limit_and_stores_the_mark(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, ctx = _mage(client_factory)
    _learn(tools, *HIGH_SPELLS)

    result = wwn_magic.prepare_spells(NAME, HIGH_SPELLS[:3], ctx)

    assert result["limit"] == 3  # the full mage's level-1 "prepared" value
    assert result["tradition"] == "high_mage"
    assert _pc(app, campaign_id).marks[wwn_magic.PREPARED_MARK] == list(HIGH_SPELLS[:3])

    with pytest.raises(InvalidInput, match="may prepare 3 spells at level 1, not 4"):
        wwn_magic.prepare_spells(NAME, HIGH_SPELLS, ctx)


def test_prepare_spells_refuses_unknown_duplicate_and_non_spell_features(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, _tools, ctx = _mage(client_factory)

    with pytest.raises(InvalidInput, match="does not know"):
        wwn_magic.prepare_spells(NAME, ["spell_apprehending_arcane_form"], ctx)
    with pytest.raises(InvalidInput, match="may not be listed twice"):
        wwn_magic.prepare_spells(NAME, [HIGH_SPELLS[0], HIGH_SPELLS[0]], ctx)
    with pytest.raises(InvalidInput, match="is not a spell"):
        wwn_magic.prepare_spells(NAME, ["focus_alert"], ctx)

    cleared = wwn_magic.prepare_spells(NAME, [], ctx)
    assert cleared["prepared"] == []
    assert wwn_magic.PREPARED_MARK not in _pc(app, campaign_id).marks


def test_cast_spell_requires_preparation_and_spends_one_casting(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, ctx = _mage(client_factory)
    _learn(tools, *HIGH_SPELLS[:2])
    wwn_magic.prepare_spells(NAME, HIGH_SPELLS[:1], ctx)

    result = wwn_magic.cast_spell(NAME, HIGH_SPELLS[0], ctx)

    assert result["castings_left"] == 0
    assert _pc(app, campaign_id).harm["castings"] == 1
    (cast,) = _events_of(app, campaign_id, SpellCast.kind)
    assert (cast.name, cast.spell) == (NAME, "Apprehending the Arcane Form")

    with pytest.raises(InvalidInput, match="has no spell castings left"):
        wwn_magic.cast_spell(NAME, HIGH_SPELLS[0], ctx)
    with pytest.raises(InvalidInput, match="has not prepared"):
        wwn_magic.cast_spell(NAME, HIGH_SPELLS[1], ctx)
    with pytest.raises(InvalidInput, match="there is no feature named 'spell_nonesuch'"):
        wwn_magic.cast_spell(NAME, "spell_nonesuch", ctx)
    with pytest.raises(InvalidInput, match="is not a spell"):
        wwn_magic.cast_spell(NAME, "focus_alert", ctx)


def test_restore_spell_slots_restores_the_castings(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, ctx = _mage(client_factory)
    _learn(tools, HIGH_SPELLS[0])
    wwn_magic.prepare_spells(NAME, HIGH_SPELLS[:1], ctx)
    wwn_magic.cast_spell(NAME, HIGH_SPELLS[0], ctx)
    assert _pc(app, campaign_id).harm["castings"] == 1

    result = wwn_magic.restore_spell_slots(NAME, ctx)

    assert result["restored"] == 1
    assert result["castings"] == 1
    assert _pc(app, campaign_id).harm.get("castings", 0) == 0
    # The slot is usable again.
    assert wwn_magic.cast_spell(NAME, HIGH_SPELLS[0], ctx)["castings_left"] == 0


def test_commit_effort_enforces_the_maximum_and_tracks_durations(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, _tools, ctx = _mage(client_factory)
    # Fixed stats: Int 18 (+2), Cha 8 (0), Magic 0, so Effort is 1 + 2 = 3.
    assert _stat(app, campaign_id, "effort_high") == 3

    scene = wwn_magic.commit_effort(NAME, "high", 1, "scene", ctx)
    assert (scene["maximum"], scene["committed"], scene["remaining"]) == (3, 1, 2)
    day = wwn_magic.commit_effort(NAME, "high", 1, "day", ctx)
    indefinite = wwn_magic.commit_effort(NAME, "high", 1, "indefinite", ctx)
    assert day["committed"] == 2
    assert indefinite["committed"] == 3
    assert _pc(app, campaign_id).marks[wwn.EFFORT_MARK] == {
        "scene": {"high": 1},
        "day": {"high": 1},
        "indefinite": {"high": 1},
    }
    assert _pc(app, campaign_id).harm["effort_high"] == 3

    with pytest.raises(InvalidInput, match="limited to 3"):
        wwn_magic.commit_effort(NAME, "high", 1, "scene", ctx)

    with pytest.raises(InvalidInput, match="no Effort pool"):
        wwn_magic.commit_effort(NAME, "wizard", 1, "scene", ctx)
    with pytest.raises(InvalidInput, match="no Effort pool"):
        wwn_magic.recover_effort(NAME, "wizard", "scene", ctx)
    with pytest.raises(InvalidInput, match="no Effort pool"):
        wwn_magic.commit_effort(NAME, "no_tradition", 1, "scene", ctx)
    with pytest.raises(InvalidInput, match="no Effort pool"):
        wwn_magic.recover_effort(NAME, "no_tradition", "scene", ctx)
    with pytest.raises(InvalidInput, match="duration must be one of"):
        wwn_magic.commit_effort(NAME, "high", 1, "week", ctx)


def test_recover_effort_refunds_only_the_matching_duration(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, _tools, ctx = _mage(client_factory)
    wwn_magic.commit_effort(NAME, "high", 1, "scene", ctx)
    wwn_magic.commit_effort(NAME, "high", 2, "day", ctx)

    result = wwn_magic.recover_effort(NAME, "high", "scene", ctx)

    assert result["recovered"] == 1
    assert result["committed"] == 2
    assert _pc(app, campaign_id).marks[wwn.EFFORT_MARK] == {"day": {"high": 2}}
    assert _pc(app, campaign_id).harm["effort_high"] == 2
    with pytest.raises(InvalidInput, match="no scene Effort committed"):
        wwn_magic.recover_effort(NAME, "high", "scene", ctx)

    day = wwn_magic.recover_effort(NAME, "high", "day", ctx)
    assert day["recovered"] == 2
    assert wwn.EFFORT_MARK not in _pc(app, campaign_id).marks
    assert _pc(app, campaign_id).harm.get("effort_high", 0) == 0


def test_a_partial_mage_has_one_less_effort_and_never_less_than_one(
    client_factory: Callable[[], TestClient],
) -> None:
    _app, _campaign_id, tools, ctx = _mage(client_factory, class_id="adventurer_expert_mage")

    # Fixed stats: Int 18 (+2), Cha 8 (0), Magic 0; a partial mage's derived pool is 2.
    wwn_magic.commit_effort(NAME, "high", 2, "scene", ctx)
    with pytest.raises(InvalidInput, match="limited to 2"):
        wwn_magic.commit_effort(NAME, "high", 1, "day", ctx)
    wwn_magic.recover_effort(NAME, "high", "scene", ctx)

    # With both mental modifiers at -2, the formula falls below one, so one is the maximum.
    tools.set_stat_max(NAME, "int", 3, cause="a test")
    tools.set_stat_max(NAME, "cha", 3, cause="a test")
    with pytest.raises(InvalidInput, match="limited to 1"):
        wwn_magic.commit_effort(NAME, "high", 2, "scene", ctx)
    assert wwn_magic.commit_effort(NAME, "high", 1, "scene", ctx)["maximum"] == 1


def test_every_arcane_class_gets_the_configured_partial_mage_effort_reduction() -> None:
    progression = require_wwn_progression(WWN)
    stats = {"str": 10, "dex": 10, "con": 10, "int": 14, "wis": 10, "cha": 10}
    for class_id, casting_table in progression.casting_class_tables.items():
        if casting_table == "full_mage":
            templates = {"class": class_id, "tradition": "high_mage"}
            pools = ("effort_high",)
        elif casting_table == "dual_mage":
            templates = {
                "class": class_id,
                "tradition": "high_mage_partial",
                "tradition2": "necromancer_partial_2",
            }
            pools = ("effort_high", "effort_necromancer")
        else:
            templates = {"class": class_id, "tradition": "high_mage_partial"}
            pools = ("effort_high",)
        values = derived_stats(WWN.character, stats, {"level": 1}, {"magic": 1}, templates)
        expected = 3 if casting_table == "full_mage" else 2
        for stat in pools:
            assert values[stat] == expected, (class_id, stat)
        # A character has only the pools their chosen traditions grant.
        assert "effort_elemental" not in values, class_id
        if casting_table != "dual_mage":
            assert "effort_necromancer" not in values, class_id


@pytest.mark.parametrize(
    ("class_id", "tradition", "other_pool"),
    [
        ("warrior", None, "high"),
        ("mage", "high_mage", "necromancer"),
        ("healer", None, "vowed"),
        ("vowed", None, "healer"),
    ],
)
def test_effort_cannot_be_committed_to_an_unavailable_tradition(
    client_factory: Callable[[], TestClient],
    class_id: str,
    tradition: str | None,
    other_pool: str,
) -> None:
    app, campaign_id = _campaign(client_factory, class_id=class_id, tradition=tradition)
    ctx = _ctx(_world_tools(app, campaign_id))

    with pytest.raises(InvalidInput, match="cannot commit .* Effort"):
        wwn_magic.commit_effort(NAME, other_pool, 1, "scene", ctx)

    character = _pc(app, campaign_id)
    assert wwn.EFFORT_MARK not in character.marks
    assert not any(key.startswith("effort_") for key in character.harm)


def test_a_dual_mage_can_commit_effort_to_either_tradition(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(
        client_factory,
        class_id="adventurer_mage_mage",
        tradition="high_mage_partial",
        tradition2="necromancer_partial_2",
    )
    ctx = _ctx(_world_tools(app, campaign_id))

    assert wwn_magic.commit_effort(NAME, "high", 1, "scene", ctx)["committed"] == 1
    assert wwn_magic.commit_effort(NAME, "necromancer", 1, "scene", ctx)["committed"] == 1


def test_add_strain_and_heal_strain_stay_within_the_strain_score(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, _tools, ctx = _mage(client_factory)
    assert _stat(app, campaign_id, "strain") == _stat(app, campaign_id, "con") == 10

    first = wwn_magic.add_strain(NAME, 4, "a hard march", ctx)
    assert (first["added"], first["strain"]) == (4, 6)
    second = wwn_magic.add_strain(NAME, 8, "a hard march", ctx)
    assert (second["added"], second["strain"]) == (6, 0)
    with pytest.raises(InvalidInput, match="already at its maximum"):
        wwn_magic.add_strain(NAME, 1, "a hard march", ctx)

    healed = wwn_magic.heal_strain(NAME, 1, "a night's rest", ctx)
    assert healed["restored"] == 1
    assert _stat(app, campaign_id, "strain") - _pc(app, campaign_id).harm["strain"] == 1
    wwn_magic.heal_strain(NAME, 100, "a night's rest", ctx)
    assert _pc(app, campaign_id).harm.get("strain", 0) == 0


@pytest.mark.parametrize(
    ("class_id", "tradition", "skill", "stat"),
    [
        ("healer", "healer", "heal", "effort_healer"),
        ("vowed", "vowed", "exert", "effort_vowed"),
    ],
)
def test_effort_uses_the_configured_derived_pool(
    client_factory: Callable[[], TestClient],
    class_id: str,
    tradition: str,
    skill: str,
    stat: str,
) -> None:
    app, campaign_id = _campaign(client_factory, class_id=class_id)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    tools.raise_skill(NAME, skill, 3, cause="a test")
    wwn._recompute_derived(ctx, NAME, WWN.character, cause="a test")

    result = wwn_magic.commit_effort(NAME, tradition, 2, "scene", ctx)

    assert result["maximum"] == _stat(app, campaign_id, stat)
    assert result["maximum"] >= 4
    assert result["committed"] == 2

    recovered = wwn_magic.recover_effort(NAME, tradition, "scene", ctx)
    assert recovered["recovered"] == 2
    assert wwn.EFFORT_MARK not in _pc(app, campaign_id).marks


@pytest.mark.parametrize("tool_name", ["commit_effort", "recover_effort"])
def test_effort_tool_advertises_every_configured_pool(tool_name: str) -> None:
    progression = require_wwn_progression(WWN)
    tradition = WWN.tool_calls[tool_name].parameters["properties"]["tradition"]
    assert set(tradition["enum"]) == set(progression.tradition_effort_stats)


def test_vowed_effort_tracks_the_order_skill_instead_of_exert(client_factory):
    app, campaign_id = _campaign(
        client_factory,
        class_id="vowed",
        options={
            "template-background": "artisan",
            "pick-skill-background_learning-0": "craft",
            "pick-skill-background_learning-1": "know",
            "pick-skill-order_skill-0": "trade",
        },
    )
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    assert _pc(app, campaign_id).fields["order_skill"] == "trade"
    assert _stat(app, campaign_id, "effort_vowed") == 2

    tools.raise_skill(NAME, "exert", 4, cause="a test")
    tools.adjust_counter(NAME, "skill_points", 2)
    wwn.spend_skill_point(NAME, "trade", ctx)
    result = wwn_magic.commit_effort(NAME, "vowed", 2, "scene", ctx)

    assert _stat(app, campaign_id, "effort_vowed") == 3
    assert result["maximum"] == 3


def test_first_aid_rolls_a_d6_plus_heal_heals_and_adds_strain(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, ctx = _mage(client_factory)
    if _pc(app, campaign_id).skills.get("heal", -1) < 1:
        tools.raise_skill(NAME, "heal", 1, cause="a test")
    tools.set_stat_max(NAME, "hp", 20, cause="a test")
    tools.set_stat_max(NAME, "strain", 10, cause="a test")
    tools.harm_character(NAME, "hp", 5, "a test")

    # random.Random(0) rolls 4 on the first d6, and Heal 1 adds one.
    result = wwn_magic.first_aid(NAME, NAME, ctx)

    assert (result["roll"], result["bonus"], result["total"]) == (4, 1, 5)
    assert result["healed"] == 5
    assert result["strain"] == 1
    character = _pc(app, campaign_id)
    assert character.harm.get("hp", 0) == 0
    assert character.harm["strain"] == 1
    (check,) = _events_of(app, campaign_id, CheckMade.kind)
    assert (check.check, check.skill, check.total) == ("First Aid", "heal", 5)


def test_recover_after_rest_restores_hp_strain_castings_and_effort(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, ctx = _mage(client_factory)
    _learn(tools, HIGH_SPELLS[0])
    wwn_magic.prepare_spells(NAME, HIGH_SPELLS[:1], ctx)
    wwn_magic.cast_spell(NAME, HIGH_SPELLS[0], ctx)
    tools.set_stat_max(NAME, "hp", 20, cause="a test")
    tools.set_stat_max(NAME, "strain", 10, cause="a test")
    tools.harm_character(NAME, "hp", 5, "a test")
    wwn_magic.add_strain(NAME, 2, "a test", ctx)
    wwn_magic.commit_effort(NAME, "high", 2, "scene", ctx)
    wwn_magic.commit_effort(NAME, "high", 1, "day", ctx)

    result = wwn.recover_after_rest(NAME, ctx)

    # A night restores hit points equal to the character's level, one Strain, every casting,
    # and the Effort committed for the scene and the day.
    assert result["restored"] == {
        "hp": 1,
        "strain": 1,
        "castings": 1,
        "effort_high": 3,
    }
    character = _pc(app, campaign_id)
    assert character.harm["hp"] == 4
    assert character.harm["strain"] == 1
    assert character.harm.get("castings", 0) == 0
    assert character.harm.get("effort_high", 0) == 0
    assert wwn.EFFORT_MARK not in character.marks


@pytest.mark.parametrize("tradition", ["unconfigured_tradition", "no_tradition"])
def test_rest_refuses_an_effort_mark_without_a_pool_and_keeps_the_mark(
    client_factory: Callable[[], TestClient],
    tradition: str,
) -> None:
    app, campaign_id, _tools, ctx = _mage(client_factory)
    malformed = {"scene": {tradition: 1}}
    ctx.world.with_hook_context(
        lambda hc: hc.set_mark(NAME, wwn.EFFORT_MARK, malformed, "a malformed test mark")
    )

    with pytest.raises(InvalidInput, match="no Effort pool for the tradition"):
        wwn.recover_after_rest(NAME, ctx)

    assert _pc(app, campaign_id).marks[wwn.EFFORT_MARK] == malformed


def _placeholder(schema: dict[str, Any]) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "array":
        return ["x"]
    return "x"


# WWN tools that cannot bind a WorldTools method directly: the wrapper translates a save name,
# injects the check id, validates a total, or fills a schema gap the method's signature has.
KEPT_WWN_WRAPPERS = {
    "player_attack",
    "player_roll_save",
}


def test_every_wwn_tool_loads_and_binds_its_declared_required_parameters() -> None:
    calls = [
        call
        for call in WWN.tool_calls.values()
        if call.function is not None
        and call.function.__module__ in {"cygnusloom.rules.wwn", "cygnusloom.rules.wwn_magic"}
    ]
    assert {call.id for call in calls} == {
        # The WorldTools methods that cannot take the schema's argument names or defaults.
        "player_roll_save",
        "player_attack",
        # Rule-set logic: advancement, focus/spell/art learning, morale, reaction, instinct, rest.
        "check_morale",
        "gain_xp",
        "gain_growth",
        "level_up",
        "spend_skill_point",
        "learn_focus",
        "learn_spell",
        "learn_art",
        "improve_attribute",
        "grant_package",
        "roll_starting_silver",
        "recover_after_rest",
        "roll_reaction",
        "instinct_check",
        "rest",
        "travel",
        # Worlds Without Number magic.
        "prepare_spells",
        "cast_spell",
        "commit_effort",
        "recover_effort",
        "add_strain",
        "heal_strain",
        "first_aid",
        "restore_spell_slots",
    }
    assert {call.id for call in calls} & KEPT_WWN_WRAPPERS == KEPT_WWN_WRAPPERS
    for call in calls:
        module = wwn if call.function.__module__ == "cygnusloom.rules.wwn" else wwn_magic
        assert getattr(module, call.name) is call.function
        assert call.world_method is None
        assert call.takes_context
        properties = call.parameters["properties"]
        required = call.parameters.get("required", [])
        consumed = {"prompt"} if call.player_input is not None else set()
        arguments = {
            name: _placeholder(properties[name]) for name in required if name not in consumed
        }
        if call.player_input is not None and call.player_input.param is not None:
            arguments[call.player_input.param] = 1
        arguments["ctx"] = object()
        inspect.signature(call.function).bind(**arguments)


def test_wwn_tools_bind_engine_operations_without_wrappers() -> None:
    core_ids = set(LIBRARY.core.tool_calls)
    bound = {
        call_id: call.world_method
        for call_id, call in WWN.tool_calls.items()
        if call.world_method is not None and call_id not in core_ids
    }
    assert bound == {
        "abandon_project": "abandon_project",
        "add_faction_asset": "add_faction_asset",
        "add_participant": "add_participant",
        "advance_project": "advance_project",
        "advance_time": "advance_time",
        "attack": "attack",
        "buy_item": "buy_item",
        "buy_service": "buy_service",
        "create_encounter_map": "create_encounter_map",
        "create_monster": "create_monster",
        "delete_encounter_map": "delete_encounter_map",
        "dismiss_hireling": "dismiss_hireling",
        "drop_item": "drop_item",
        "end_encounter": "end_encounter",
        "equip_armor": "equip_armor",
        "break_grapple": "break_grapple",
        "choose_feature_choice": "choose_feature_choice",
        "create_magic_item": "create_magic_item",
        "damage_faction_asset": "damage_faction_asset",
        "execute": "execute",
        "faction_asset_acted": "faction_asset_acted",
        "give_item": "give_item",
        "grapple": "grapple",
        "harm_character": "harm_character",
        "heal_character": "heal_character",
        "hire_hireling": "hire_hireling",
        "place_participant": "place_participant",
        "player_save": "player_save",
        "prepare_execution": "prepare_execution",
        "ready_item": "ready_item",
        "recover_character": "recover_character",
        "remove_faction_asset": "remove_faction_asset",
        "remove_participant": "remove_participant",
        "repair_faction_asset": "repair_faction_asset",
        "resolve_attacks": "resolve_attacks",
        "retreat": "retreat",
        "roll_check": "roll_check",
        "roll_initiative": "roll_side_initiative",
        "roll_save": "roll_save",
        "roll_tables": "roll_tables",
        "search_marketplace": "search_marketplace",
        "search_monster": "search_monster",
        "set_ac": "set_armor",
        "set_armor": "set_armor",
        "stabilize": "stabilize",
        "start_encounter": "start_encounter",
        "start_project": "start_project",
        "surprise": "surprise",
        "take_faction_turn": "take_faction_turn",
        "total_defense": "total_defense",
        "update_encounter_map": "update_encounter_map",
        "update_faction_asset": "update_faction_asset",
    }
    for call_id, method in bound.items():
        call = WWN.tool_calls[call_id]
        assert call.function is None
        assert call.takes_context is False
        assert call.world_method == method
    for call_id in KEPT_WWN_WRAPPERS:
        call = WWN.tool_calls[call_id]
        assert call.function is not None
        assert call.world_method is None


def test_first_aid_gives_no_benefit_at_maximum_system_strain(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools, ctx = _mage(client_factory)
    tools.set_stat_max(NAME, "hp", 20, cause="a test")
    tools.set_stat_max(NAME, "strain", 10, cause="a test")
    tools.harm_character(NAME, "hp", 5, "a test")
    wwn_magic.add_strain(NAME, 10, "a test", ctx)

    result = wwn_magic.first_aid(NAME, NAME, ctx)

    assert result["healed"] == 0
    assert result["roll"] is None
    assert _pc(app, campaign_id).harm["hp"] == 5
