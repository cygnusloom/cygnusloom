"""Worlds Without Number advancement: experience, levels, skills, Foci, spells, and AC.

The helpers here build a real campaign and player character through the web creation form, the
way `tests/test_wwn.py` does, and hand the rule set's tools a `WorldTools` for the campaign.
`tests/test_wwn_magic.py` reuses the same helpers.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from dataclasses import replace
from html import unescape
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.character import Feature, Granted
from cygnusloom.content.library import ContentLibrary, LocationTemplate, NpcTemplate
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.rules import wwn
from cygnusloom.services import campaigns as campaigns_service
from cygnusloom.services import world as world_service
from cygnusloom.services.clock import rest_target
from cygnusloom.services.common import InvalidInput, session_scope
from cygnusloom.services.events import (
    ArmorSet,
    CharacterDying,
    ConditionAdded,
    FeatureLearned,
    ItemAdded,
    NpcAdded,
    SkillChanged,
    Struck,
    decode,
    events_since,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.injuries import EffectContext, run_effects
from cygnusloom.services.tools import check_arguments
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library
from tests.factories import WWN_STATS, create_character_direct
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_character_creation import (
    _choices,
    _named_picks,
    _picks,
    _rolled_stats,
)

LIBRARY: ContentLibrary = content_library()
WWN = LIBRARY.rule_sets["worldswithoutnumber"]
NAME = "Wren"

# The creation form rolls from an unseeded generator. These tests pin the roll so the class,
# Constitution, Dexterity, and mental modifiers a level or a check sees are known. A Constitution
# of 10 gives no HP modifier, so the rolled 6 is also the submitted HP.


def _fixed_roll(spec: object, rng: object, *, plus: str) -> dict[str, int]:
    """The rolled stats, pinned so the tests know every modifier."""
    return dict(WWN_STATS)


# The Focus and art picks each class and tradition takes in the creation form, so the tests know
# exactly which features the character starts with instead of rolling them.
CLASS_PICKS: dict[str, dict[str, str]] = {
    "warrior": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_warrior-0": "focus_alert",
    },
    "expert": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_expert-0": "focus_connected",
    },
    "mage": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
    },
    "adventurer_expert_warrior": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_expert-0": "focus_connected",
        "pick-feature-class_focus_warrior-0": "focus_alert",
    },
    "adventurer_expert_mage": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_expert-0": "focus_connected",
    },
    "adventurer_warrior_mage": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
        "pick-feature-class_focus_warrior-0": "focus_alert",
    },
    "adventurer_mage_mage": {
        "pick-feature-class_focus_any-0": "focus_die_hard",
    },
    "healer": {
        "pick-feature-class_focus_any-0": "focus_unique_gift",
        "pick-feature-class_art_healer-0": "art_empowered_healer",
    },
    "vowed": {
        "pick-feature-class_focus_any-0": "focus_unique_gift",
        "pick-feature-class_art_vowed-0": "art_brutal_counter",
    },
}
TRADITION2_PICKS: dict[str, dict[str, str]] = {
    "high_mage_partial_2": {
        "pick-feature-tradition2_art_high-0": "art_arcane_lexicon",
        "pick-feature-tradition2_spells_high-0": "spell_long_amber_moment",
        "pick-feature-tradition2_spells_high-1": "spell_wardpact_invocation",
    },
    "necromancer_partial_2": {
        "pick-feature-tradition2_art_necromancer-0": "art_bonetalker",
        "pick-feature-tradition2_spells_necromancer-0": "spell_long_amber_moment",
        "pick-feature-tradition2_spells_necromancer-1": "spell_wardpact_invocation",
    },
    "elementalist_partial_2": {
        "pick-feature-tradition2_art_elementalist-0": "art_elemental_blast",
        "pick-feature-tradition2_spells_elementalist-0": "spell_long_amber_moment",
        "pick-feature-tradition2_spells_elementalist-1": "spell_wardpact_invocation",
    },
}
CLASS2_PICKS: dict[str, dict[str, str]] = {
    "warrior_partial_2": {
        "pick-feature-second_class_focus_warrior-0": "focus_alert",
    },
    "expert_partial_2": {
        "pick-feature-second_class_focus_expert-0": "focus_connected",
    },
    "mage_partial_2": {},
    "healer_partial_2": {
        "pick-feature-second_class_art_healer-0": "art_facile_healer",
    },
    "vowed_partial_2": {
        "pick-feature-second_class_art_vowed-0": "art_brutal_counter",
        "pick-skill-second_order_skill-0": "trade",
    },
}
TRADITION_PICKS: dict[str, dict[str, str]] = {
    "no_tradition": {},
    "high_mage": {
        "pick-feature-tradition_art_high-0": "art_arcane_lexicon",
        "pick-feature-tradition_art_high-1": "art_counter_magic",
        "pick-feature-tradition_spells_high-0": "spell_coruscating_coffin",
        "pick-feature-tradition_spells_high-1": "spell_damnation_of_the_sense",
        "pick-feature-tradition_spells_high-2": "spell_long_amber_moment",
        "pick-feature-tradition_spells_high-3": "spell_wardpact_invocation",
    },
    "high_mage_partial": {
        "pick-feature-tradition_art_high-0": "art_arcane_lexicon",
        "pick-feature-tradition_spells_high-0": "spell_coruscating_coffin",
        "pick-feature-tradition_spells_high-1": "spell_damnation_of_the_sense",
    },
    "necromancer": {
        "pick-feature-tradition_art_necromancer-0": "art_bonetalker",
        "pick-feature-tradition_spells_necromancer-0": "spell_coruscating_coffin",
        "pick-feature-tradition_spells_necromancer-1": "spell_damnation_of_the_sense",
        "pick-feature-tradition_spells_necromancer-2": "spell_long_amber_moment",
        "pick-feature-tradition_spells_necromancer-3": "spell_wardpact_invocation",
    },
    "necromancer_partial": {
        "pick-feature-tradition_art_necromancer-0": "art_bonetalker",
        "pick-feature-tradition_spells_necromancer-0": "spell_coruscating_coffin",
        "pick-feature-tradition_spells_necromancer-1": "spell_damnation_of_the_sense",
    },
    "elementalist": {
        "pick-feature-tradition_art_elementalist-0": "art_elemental_blast",
        "pick-feature-tradition_spells_elementalist-0": "spell_coruscating_coffin",
        "pick-feature-tradition_spells_elementalist-1": "spell_damnation_of_the_sense",
        "pick-feature-tradition_spells_elementalist-2": "spell_long_amber_moment",
        "pick-feature-tradition_spells_elementalist-3": "spell_wardpact_invocation",
    },
    "elementalist_partial": {
        "pick-feature-tradition_art_elementalist-0": "art_elemental_blast",
        "pick-feature-tradition_spells_elementalist-0": "spell_coruscating_coffin",
        "pick-feature-tradition_spells_elementalist-1": "spell_damnation_of_the_sense",
    },
}


def _campaign(
    client_factory: Callable[[], TestClient],
    *,
    class_id: str = "warrior",
    class2: str | None = None,
    tradition: str | None = None,
    tradition2: str | None = None,
    options: dict[str, str] | None = None,
    kit: str | None = None,
    name: str = NAME,
) -> tuple[FastAPI, int]:
    """A campaign with one player character of the chosen class, created through the form."""
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(campaigns_service, "roll_stats", _fixed_roll)
        page = client.get(f"/campaigns/{campaign_id}").text
        fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
        fields.update(_choices(page))
        fields.update(_picks(page))
        fields.update(options or {})
        if kit is not None:
            fields["template-kit"] = kit
        fields["template-class"] = class_id
        fields.update(CLASS_PICKS[class_id])
        fields["pick-skill-class_free_skill-0"] = "notice"
        if class_id == "vowed":
            fields.setdefault("pick-skill-order_skill-0", "exert")
        if class2 is not None:
            fields["template-class2"] = class2
            fields.update(CLASS2_PICKS[class2])
        if tradition is not None:
            if class_id != "mage" and tradition in {"high_mage", "necromancer", "elementalist"}:
                tradition = f"{tradition}_partial"
            fields["template-tradition"] = tradition
            fields.update(TRADITION_PICKS[tradition])
        else:
            fields["template-tradition"] = "no_tradition"
        if tradition2 is not None:
            fields["template-tradition2"] = tradition2
            fields.update(TRADITION2_PICKS[tradition2])
        fields = _named_picks(fields)
        create_character_direct(client, campaign_id, name=name, fields=fields)
    return client.app, campaign_id


def _campaign_context(
    app: FastAPI,
    campaign_id: int,
    *,
    seed: int = 0,
    rule_set=None,
):
    """Build the same rule-set tool context as a running campaign."""
    rule_set = WWN if rule_set is None else rule_set
    world = LIBRARY.world_content("unwritten-world", rule_set.id)
    return campaigns_service._tool_context(
        app.state.engine,
        random.Random(seed),
        FakeProvider(),
        "fake/scripted",
        campaign_id,
        rule_set,
        world,
    )


def _world_tools(
    app: FastAPI,
    campaign_id: int,
    *,
    seed: int = 0,
    spec: Any = None,
) -> WorldTools:
    """The campaign's world tools, configured as `services/campaigns.py` configures them."""
    rule_set = WWN if spec is None else replace(WWN, character=spec)
    context = _campaign_context(app, campaign_id, seed=seed, rule_set=rule_set)
    assert isinstance(context.world, WorldTools)
    return context.world


def _ctx(tools: WorldTools) -> Any:
    """A tool context holds the world the rule set's functions read."""
    return SimpleNamespace(world=tools)


def _pc(app: FastAPI, campaign_id: int):
    with Session(app.state.engine) as db:
        character = load_state(db, campaign_id).pc()
    assert character is not None
    return character


def _stat(app: FastAPI, campaign_id: int, stat: str) -> int:
    return _pc(app, campaign_id).stats[stat]


def _counter(app: FastAPI, campaign_id: int, counter: str) -> int:
    return _pc(app, campaign_id).counters.get(counter, WWN.counters[counter].initial)


def _events_of(app: FastAPI, campaign_id: int, kind: str) -> list[Any]:
    with session_scope(app.state.engine) as db:
        return [decode(row) for row in events_since(db, campaign_id) if row.kind == kind]


def _set_stat(tools: WorldTools, stat: str, value: int) -> None:
    tools.set_stat_max(NAME, stat, value, cause="a test")


def _give_pick(tools: WorldTools, counter: str, amount: int) -> None:
    tools.adjust_counter(NAME, counter, amount)


def test_the_submitted_warrior_and_mage_keep_their_class_and_tradition(
    client_factory: Callable[[], TestClient],
) -> None:
    warrior_app, warrior_campaign = _campaign(client_factory, class_id="warrior")
    warrior = _pc(warrior_app, warrior_campaign)
    assert warrior.templates["class"] == "warrior"
    assert warrior.templates["tradition"] == "no_tradition"
    assert warrior.stats["hp"] == 10  # 6 rolled + a Warrior's +2 + Die Hard's +2
    assert warrior.stats["attack_bonus"] == 1

    mage_app, mage_campaign = _campaign(client_factory, class_id="mage", tradition="high_mage")
    mage = _pc(mage_app, mage_campaign)
    assert mage.templates["class"] == "mage"
    assert mage.templates["tradition"] == "high_mage"
    assert mage.stats["hp"] == 7  # 6 rolled - a Mage's -1 + Die Hard's +2
    assert mage.stats["attack_bonus"] == 0
    assert mage.skills.get("magic") == 0  # the tradition's free skill
    assert "art_arcane_lexicon" in mage.features
    assert "spell_coruscating_coffin" in mage.features


def test_gain_xp_accumulates_and_marks_the_next_level_threshold(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))

    below = wwn.gain_xp(NAME, 2, "a first session", ctx)
    assert below["xp"] == 2
    assert below["level"] == 1
    assert below["next_level"] == 2
    assert below["next_threshold"] == 3
    assert below["ready"] is False
    assert _counter(app, campaign_id, "xp") == 2

    with pytest.raises(InvalidInput, match="level 2 needs 3"):
        wwn.apply_level_up(NAME, ctx)
    assert _counter(app, campaign_id, "level") == 1

    at = wwn.gain_xp(NAME, 1, "a second session", ctx)
    assert at["xp"] == 3
    assert at["ready"] is True
    assert _counter(app, campaign_id, "xp") == 3


def test_gain_growth_draws_the_background_table_through_campaign_tools(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    ctx = _campaign_context(app, campaign_id, seed=7)
    pc = _pc(app, campaign_id)
    background = pc.templates["background"]
    table = f"growth_{background}"
    expected = random.Random(7).choice(ctx.world.tables[table]).value
    with Session(app.state.engine) as db:
        event_count = len(events_since(db, campaign_id))

    result = wwn.gain_growth(NAME, ctx)

    assert result == {
        "character": NAME,
        "background": background,
        "table": table,
        "growth": expected,
    }
    with Session(app.state.engine) as db:
        assert len(events_since(db, campaign_id)) == event_count


def test_gain_growth_reports_a_missing_background_table(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    background = _pc(app, campaign_id).templates["background"]
    table = f"growth_{background}"
    rule_set = replace(
        WWN,
        content=replace(
            WWN.content,
            tables={key: value for key, value in WWN.tables.items() if key != table},
        ),
    )
    ctx = _campaign_context(app, campaign_id, rule_set=rule_set)

    assert table not in ctx.world.tables
    with pytest.raises(InvalidInput, match="Wren has no background Growth table"):
        wwn.gain_growth(NAME, ctx)


def test_level_up_raises_hp_attack_bonus_skill_points_and_focus_picks(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id, seed=0)
    ctx = _ctx(tools)
    _set_stat(tools, "con", 10)  # no Constitution modifier
    before = _pc(app, campaign_id)

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.apply_level_up(NAME, ctx)

    # random.Random(0) rolls 4 and 4 on the two d6; a Warrior adds +2 a level and Die Hard
    # adds 2 more, so the rerolled total is 8 + 4 + 2 = 14, better than the old 10.
    assert result["level"] == 2
    assert result["hp"] == 14
    assert result["attack_bonus"] == 2
    assert result["skill_points"] == 3
    assert result["focus_picks"] == 1
    assert result["castings"] is None

    character = _pc(app, campaign_id)
    assert character.counters["level"] == 2
    assert character.stats["hp"] == 14
    assert character.stats["attack_bonus"] == 2
    assert character.counters["skill_points"] == 3
    assert character.counters["focus_picks"] == 1
    # The saves lose one point a level.
    assert character.stats["save_luck"] == before.stats["save_luck"] - 1


def test_an_expert_pairing_gains_two_skill_points_on_level_up(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="adventurer_expert_warrior")
    ctx = _ctx(_world_tools(app, campaign_id))

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.apply_level_up(NAME, ctx)

    # Quick Learner gives the Expert and partial Expert an extra point each level.
    assert result["skill_points"] == 4
    assert _counter(app, campaign_id, "skill_points") == 4


def test_a_healer_and_partial_warrior_advance_on_the_combined_chart(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="healer", class2="warrior_partial_2")
    tools = _world_tools(app, campaign_id, seed=0)
    ctx = _ctx(tools)
    _set_stat(tools, "con", 10)  # no Constitution modifier
    assert _stat(app, campaign_id, "attack_bonus") == 1

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.apply_level_up(NAME, ctx)

    # The combined chart takes the Warrior's +2 HP a level and its higher attack; seed 0 rolls
    # 4 and 4 on the two d6, so the rerolled total is 8 + 2 levels x 2 = 12.
    assert result["hp"] == 12
    assert result["attack_bonus"] == 2
    assert result["skill_points"] == 3
    assert result["focus_picks"] == 1
    assert _stat(app, campaign_id, "attack_bonus") == 2
    assert _counter(app, campaign_id, "focus_picks") == 1


def test_a_healer_and_partial_expert_gains_quick_learner_skill_points(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="healer", class2="expert_partial_2")
    ctx = _ctx(_world_tools(app, campaign_id))

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.apply_level_up(NAME, ctx)

    # A partial Expert's Quick Learner gives the fourth skill point.
    assert result["skill_points"] == 4
    assert _counter(app, campaign_id, "skill_points") == 4
    options = {
        option["id"] for option in wwn.focus_options(NAME, SimpleNamespace(exclude=set()), ctx)
    }
    # The second partial class's own focus pick widens the options past the Healer's list.
    assert "focus_polymath" in options


def test_a_healer_and_partial_vowed_combines_both_art_traditions(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="healer", class2="vowed_partial_2")
    ctx = _ctx(_world_tools(app, campaign_id))
    character = _pc(app, campaign_id)
    assert {"effort_healer", "effort_vowed"} <= set(character.stats)

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.apply_level_up(NAME, ctx)

    # Both partial art charts grant a pick at level 2.
    assert result["art_picks"] == 2
    options = {
        option["id"] for option in wwn.art_options(NAME, SimpleNamespace(exclude=set()), ctx)
    }
    assert {"art_facile_healer", "art_faultless_awareness"} <= options
    assert wwn.learn_art(NAME, "art_facile_healer", ctx)["art_picks"] == 1
    assert wwn.learn_art(NAME, "art_faultless_awareness", ctx)["art_picks"] == 0


def test_a_healer_and_partial_mage_casts_as_a_partial_mage(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(
        client_factory,
        class_id="healer",
        class2="mage_partial_2",
        tradition="high_mage_partial",
    )
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    # Int 18 gives +2 and the second partial Mage takes the partial caster's -1.
    assert _stat(app, campaign_id, "effort_high") == 2

    wwn.gain_xp(NAME, 18, "a long road", ctx)
    castings = [wwn.apply_level_up(NAME, ctx)["castings"] for _ in range(4)]

    # The partial Mage chart casts 1, 1, 2, 2; a full Mage would cast 1, 2, 2, 3.
    assert castings == [1, 1, 2, 2]
    assert _stat(app, campaign_id, "castings") == 2
    options = wwn.spell_options(NAME, SimpleNamespace(exclude=set()), ctx)
    ranks = {option["rank"] for option in options}
    assert 2 in ranks
    # A partial Mage at level 5 casts spells of level 2 at most.
    assert max(ranks) == 2


def test_a_dual_partial_summary_reports_the_combined_gains(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="healer", class2="warrior_partial_2")
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)

    summary = wwn.level_up_summary(NAME, ctx)

    assert summary["new_level"] == 2
    assert summary["attack_bonus"] == 2
    assert summary["castings"] is None
    assert summary["grants"] == {
        "skill_points": 3,
        "focus_picks": 1,
        "art_picks": 1,
        "spell_picks": 0,
    }
    assert "Attack bonus: 2." in summary["lines"]
    assert "Focus pick: 1." in summary["lines"]


@pytest.mark.parametrize(
    ("class_id", "class2", "tradition"),
    [
        ("warrior", None, None),
        ("mage", None, "high_mage"),
        ("healer", "warrior_partial_2", None),
    ],
)
def test_the_level_summary_and_the_applied_level_grant_the_same_pools(
    client_factory: Callable[[], TestClient],
    class_id: str,
    class2: str | None,
    tradition: str | None,
) -> None:
    app, campaign_id = _campaign(
        client_factory, class_id=class_id, class2=class2, tradition=tradition
    )
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)

    summary = wwn.level_up_summary(NAME, ctx)
    applied = wwn.apply_level_up(NAME, ctx)

    assert applied["grants"] == summary["grants"]
    assert applied["attack_bonus"] == summary["attack_bonus"]
    assert applied["castings"] == summary["castings"]


@pytest.mark.parametrize(
    ("class_id", "class2", "tradition", "spell_picks"),
    [
        ("mage", None, "high_mage", 2),
        ("mage", None, "necromancer", 1),
        ("mage", None, "elementalist", 1),
        ("healer", "mage_partial_2", "high_mage_partial", 1),
    ],
)
def test_the_casting_table_sets_the_spell_picks(
    client_factory: Callable[[], TestClient],
    class_id: str,
    class2: str | None,
    tradition: str,
    spell_picks: int,
) -> None:
    app, campaign_id = _campaign(
        client_factory, class_id=class_id, class2=class2, tradition=tradition
    )
    ctx = _ctx(_world_tools(app, campaign_id))
    wwn.gain_xp(NAME, 3, "a session", ctx)

    assert wwn.level_up_summary(NAME, ctx)["grants"]["spell_picks"] == spell_picks


def test_die_hard_hp_per_level_is_feature_data() -> None:
    die_hard = WWN.character.features["focus_die_hard"]

    assert die_hard.name == "Die Hard"
    assert die_hard.level_hp_bonus == 2
    assert WWN.character.features["focus_alert"].level_hp_bonus == 0


def test_a_caster_raises_castings_from_the_class_table_on_level_up(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    ctx = _ctx(_world_tools(app, campaign_id))
    assert _stat(app, campaign_id, "castings") == 1

    wwn.gain_xp(NAME, 3, "a session", ctx)
    second = wwn.apply_level_up(NAME, ctx)
    assert second["level"] == 2
    assert second["castings"] == 1
    assert _stat(app, campaign_id, "castings") == 1

    wwn.gain_xp(NAME, 3, "another session", ctx)
    third = wwn.apply_level_up(NAME, ctx)
    assert third["level"] == 3
    assert third["castings"] == 2
    assert _stat(app, campaign_id, "castings") == 2


def test_a_mage_with_a_poor_constitution_gains_at_least_one_hit_point(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    # Seed 2 rolls 1 on the first d6. A Mage adds -1 a level, Constitution 3 adds -2, and
    # Die Hard adds +2.
    tools = _world_tools(app, campaign_id, seed=2)
    ctx = _ctx(tools)
    _set_stat(tools, "con", 3)
    tools.set_stat_max(NAME, "hp", 5, cause="a test")

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.apply_level_up(NAME, ctx)

    assert result["hp"] == 8
    assert _stat(app, campaign_id, "hp") == 8


def test_spend_skill_point_raises_a_skill_and_spends_one_point(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    _give_pick(tools, "skill_points", 3)
    before = _pc(app, campaign_id).skills.get("sneak", -1)
    cost = before + 2

    result = wwn.spend_skill_point(NAME, "sneak", ctx)

    assert result["level"] == before + 1
    assert result["cost"] == cost
    assert result["remaining"] == 3 - cost
    assert _pc(app, campaign_id).skills["sneak"] == before + 1
    assert _counter(app, campaign_id, "skill_points") == 3 - cost
    (changed,) = _events_of(app, campaign_id, SkillChanged.kind)
    assert (changed.skill, changed.after) == ("sneak", before + 1)


def test_spend_skill_point_uses_the_configured_first_level(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    progression = WWN.character.skill_progression
    assert progression is not None
    progression = replace(progression, first_level=1)
    spec = replace(WWN.character, skill_progression=progression)
    tools = _world_tools(app, campaign_id, spec=spec)
    character = _pc(app, campaign_id)
    skill = next(skill_id for skill_id in spec.skills if skill_id not in character.skills)
    _give_pick(tools, "skill_points", 5)

    result = wwn.spend_skill_point(NAME, skill, _ctx(tools))

    assert result["level"] == progression.first_level
    assert result["cost"] == 2
    assert _pc(app, campaign_id).skills[skill] == progression.first_level


def test_spend_skill_point_uses_the_spec_skill_maximum(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    progression = WWN.character.skill_progression
    assert progression is not None
    progression = replace(progression, creation_maximum=2, maximum=2)
    spec = replace(WWN.character, skill_progression=progression)
    tools = _world_tools(app, campaign_id, spec=spec)
    character = _pc(app, campaign_id)
    skill = next(skill_id for skill_id in spec.skills if skill_id not in character.skills)
    tools.raise_skill(NAME, skill, progression.maximum, cause="a test")
    _give_pick(tools, "skill_points", 20)

    with pytest.raises(InvalidInput, match="already at level 2"):
        wwn.spend_skill_point(NAME, skill, _ctx(tools))


def test_spend_skill_point_rejects_levels_outside_the_wwn_chart(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    progression = WWN.character.skill_progression
    assert progression is not None
    spec = replace(
        WWN.character,
        skill_progression=replace(progression, maximum=5),
    )
    tools = _world_tools(app, campaign_id, spec=spec)
    character = _pc(app, campaign_id)
    skill = next(skill_id for skill_id in spec.skills if skill_id not in character.skills)
    tools.raise_skill(NAME, skill, 4, cause="a test")
    tools.adjust_counter(NAME, "level", 8)
    _give_pick(tools, "skill_points", 20)

    with pytest.raises(
        InvalidInput,
        match="Worlds Without Number skill advancement chart does not support level 5",
    ):
        wwn.spend_skill_point(NAME, skill, _ctx(tools))


def test_spend_skill_point_caps_a_skill_at_four(client_factory: Callable[[], TestClient]) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    tools.adjust_counter(NAME, "level", 8)
    _give_pick(tools, "skill_points", 20)

    for _ in range(10):
        if _pc(app, campaign_id).skills.get("sneak", -1) >= 4:
            break
        wwn.spend_skill_point(NAME, "sneak", ctx)

    assert _pc(app, campaign_id).skills["sneak"] == 4
    points = _counter(app, campaign_id, "skill_points")
    with pytest.raises(InvalidInput, match="already at level 4"):
        wwn.spend_skill_point(NAME, "sneak", ctx)
    assert _counter(app, campaign_id, "skill_points") == points


def test_spend_skill_point_refuses_without_a_point_or_a_skill(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))
    assert _counter(app, campaign_id, "skill_points") == 0
    untrained = next(
        skill for skill in WWN.character.skills if skill not in _pc(app, campaign_id).skills
    )

    with pytest.raises(InvalidInput, match="costs 1 skill point"):
        wwn.spend_skill_point(NAME, untrained, ctx)
    with pytest.raises(InvalidInput, match="there is no skill named 'wizardry'"):
        wwn.spend_skill_point(NAME, "wizardry", ctx)


def test_learn_focus_spends_a_pick_and_applies_skill_stat_and_armor_effects(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    synthetic = Feature(
        id="focus_test_effects",
        name="Test Effects",
        feature_type="feat",
        description="A focus that exercises each effect the advancement tools apply.",
        size="normal",
        uses=0,
        armor=0,
        tag="focus",
        rank=1,
        effects=(
            {"op": "skill", "id": "sneak", "delta": 1},
            {"op": "stat", "id": "str", "delta": 1},
            {"op": "armor", "delta": 1},
        ),
    )
    spec = replace(
        WWN.character,
        features={**WWN.character.features, synthetic.id: synthetic},
    )
    tools = _world_tools(app, campaign_id, spec=spec)
    ctx = _ctx(tools)
    _give_pick(tools, "focus_picks", 1)
    _set_stat(tools, "str", 10)
    _set_stat(tools, "dex", 10)
    tools.set_armor(NAME, 10)
    before = _pc(app, campaign_id)

    result = wwn.learn_focus(NAME, synthetic.id, ctx)

    character = _pc(app, campaign_id)
    assert result["focus_picks"] == 0
    assert result["rank"] == 1
    assert character.features[synthetic.id] == 1
    assert character.counters["focus_picks"] == 0
    # A Focus taken during advancement spends three skill points on its bonus skill.
    skill_before = before.skills.get("sneak", -1)
    expected = skill_before
    budget = 3
    while expected < 4:
        cost = WWN.rule_data["progression"].skill_costs.get(expected + 1)
        if cost is None or cost > budget:
            break
        budget -= cost
        expected += 1
    assert character.skills["sneak"] == expected
    assert character.stats["str"] == 11
    assert character.armor == 11  # 10 + Dexterity 0 + one point of armor
    (learned,) = _events_of(app, campaign_id, FeatureLearned.kind)
    assert (learned.feature, learned.name) == (synthetic.id, NAME)


def test_learn_focus_uses_the_configured_first_level_for_an_untrained_skill(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    before = _pc(app, campaign_id)
    skill = next(skill_id for skill_id in WWN.character.skills if skill_id not in before.skills)
    progression = WWN.character.skill_progression
    assert progression is not None
    progression = replace(progression, first_level=1)
    synthetic = Feature(
        id="focus_test_first_skill_level",
        name="First Skill Level",
        feature_type="feat",
        description="A focus that grants one untrained skill level.",
        size="normal",
        uses=0,
        armor=0,
        tag="focus",
        rank=1,
        effects=({"op": "skill", "id": skill, "delta": 1},),
    )
    spec = replace(
        WWN.character,
        features={**WWN.character.features, synthetic.id: synthetic},
        skill_progression=progression,
    )
    tools = _world_tools(app, campaign_id, spec=spec)
    _give_pick(tools, "focus_picks", 1)

    wwn.learn_focus(NAME, synthetic.id, _ctx(tools))

    assert _pc(app, campaign_id).skills[skill] == progression.first_level


def test_learn_focus_uses_the_spec_skill_maximum(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    before = _pc(app, campaign_id)
    skill = next(
        skill_id for skill_id in WWN.character.skills if before.skills.get(skill_id, -1) < 2
    )
    _world_tools(app, campaign_id).raise_skill(NAME, skill, 2, cause="a test")
    progression = WWN.character.skill_progression
    assert progression is not None
    progression = replace(progression, creation_maximum=2, maximum=2)
    synthetic = Feature(
        id="focus_test_skill_maximum",
        name="Skill Maximum",
        feature_type="feat",
        description="A focus that tries to grant a skill above its configured maximum.",
        size="normal",
        uses=0,
        armor=0,
        tag="focus",
        rank=1,
        effects=({"op": "skill", "id": skill, "delta": 1},),
    )
    spec = replace(
        WWN.character,
        features={**WWN.character.features, synthetic.id: synthetic},
        skill_progression=progression,
    )
    tools = _world_tools(app, campaign_id, spec=spec)
    _give_pick(tools, "focus_picks", 1)

    wwn.learn_focus(NAME, synthetic.id, _ctx(tools))

    assert _pc(app, campaign_id).skills[skill] == progression.maximum


@pytest.mark.parametrize(
    ("before", "expected", "leftover"),
    [(-1, 1, 0), (0, 1, 1), (1, 2, 0), (2, 2, 3), (3, 3, 3), (4, 4, 3)],
)
def test_a_focus_bonus_skill_is_worth_three_skill_points(
    client_factory: Callable[[], TestClient], before: int, expected: int, leftover: int
) -> None:
    """The book spends three skill points on a Focus's bonus skill. That raises an untrained
    skill to level 1 and a level-1 skill to level 2, but not a level-2 skill, which needs four;
    points that buy no level remain available to spend later."""
    app, campaign_id = _campaign(client_factory)
    skill = next(
        skill_id
        for skill_id in WWN.character.skills
        if WWN.character.skill_level(_pc(app, campaign_id).skills, skill_id) <= before
    )
    synthetic = Feature(
        id=f"focus_test_bonus_skill_{before + 1}",
        name="Bonus Skill",
        feature_type="feat",
        description="A focus that grants one bonus skill.",
        size="normal",
        uses=0,
        armor=0,
        tag="focus",
        rank=1,
        effects=({"op": "skill", "id": skill, "delta": 1},),
    )
    spec = replace(
        WWN.character,
        features={**WWN.character.features, synthetic.id: synthetic},
    )
    tools = _world_tools(app, campaign_id, spec=spec)
    if before >= 0:
        tools.raise_skill(NAME, skill, before, cause="a test")
    _give_pick(tools, "focus_picks", 1)
    points_before = _counter(app, campaign_id, "skill_points")

    wwn.learn_focus(NAME, synthetic.id, _ctx(tools))

    assert _pc(app, campaign_id).skills[skill] == expected
    assert _counter(app, campaign_id, "skill_points") == points_before + leftover


def test_learn_focus_needs_a_pick_and_the_first_rank(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)

    with pytest.raises(InvalidInput, match="no focus picks to spend"):
        wwn.learn_focus(NAME, "focus_armsmaster", ctx)

    _give_pick(tools, "focus_picks", 2)
    with pytest.raises(InvalidInput, match="needs Armsmaster first"):
        wwn.learn_focus(NAME, "focus_armsmaster_2", ctx)
    assert _counter(app, campaign_id, "focus_picks") == 2

    wwn.learn_focus(NAME, "focus_armsmaster", ctx)
    assert _counter(app, campaign_id, "focus_picks") == 1
    wwn.learn_focus(NAME, "focus_armsmaster_2", ctx)
    assert _counter(app, campaign_id, "focus_picks") == 0

    _give_pick(tools, "focus_picks", 1)
    with pytest.raises(InvalidInput, match="already has Armsmaster"):
        wwn.learn_focus(NAME, "focus_armsmaster", ctx)


@pytest.mark.parametrize("focus", ["focus_developed_attribute", "focus_specialist"])
def test_learn_focus_can_repeat_an_explicitly_repeatable_first_rank(
    client_factory: Callable[[], TestClient], focus: str
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    _give_pick(tools, "focus_picks", 2)

    wwn.learn_focus(NAME, focus, _ctx(tools))
    wwn.learn_focus(NAME, focus, _ctx(tools))

    assert _pc(app, campaign_id).features[focus] == 2
    assert _counter(app, campaign_id, "focus_picks") == 0
    assert [event.repeat for event in _events_of(app, campaign_id, FeatureLearned.kind)] == [
        True,
        True,
    ]


def test_learn_focus_repeats_spirit_familiar_second_rank_only_after_first_rank(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    _give_pick(tools, "focus_picks", 3)

    with pytest.raises(InvalidInput, match="needs Spirit Familiar first"):
        wwn.learn_focus(NAME, "focus_spirit_familiar_2", ctx)
    assert _counter(app, campaign_id, "focus_picks") == 3

    wwn.learn_focus(NAME, "focus_spirit_familiar", ctx)
    wwn.learn_focus(NAME, "focus_spirit_familiar_2", ctx)
    wwn.learn_focus(NAME, "focus_spirit_familiar_2", ctx)

    features = _pc(app, campaign_id).features
    assert features["focus_spirit_familiar"] == 1
    assert features["focus_spirit_familiar_2"] == 2
    assert _counter(app, campaign_id, "focus_picks") == 0


def test_learn_focus_applies_effects_again_for_each_repeatable_copy(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    synthetic = Feature(
        id="focus_repeatable_effect",
        name="Repeatable Effect",
        feature_type="feat",
        description="A repeatable focus with a measurable effect.",
        size="normal",
        uses=0,
        armor=0,
        tag="focus",
        rank=1,
        effects=({"op": "stat", "id": "str", "delta": 1},),
        repeatable=True,
    )
    spec = replace(WWN.character, features={**WWN.character.features, synthetic.id: synthetic})
    tools = _world_tools(app, campaign_id, spec=spec)
    _set_stat(tools, "str", 10)
    _give_pick(tools, "focus_picks", 2)

    wwn.learn_focus(NAME, synthetic.id, _ctx(tools))
    wwn.learn_focus(NAME, synthetic.id, _ctx(tools))

    character = _pc(app, campaign_id)
    assert character.features[synthetic.id] == 2
    assert character.stats["str"] == 12


@pytest.mark.parametrize("focus", ["focus_developed_attribute", "focus_nullifier"])
@pytest.mark.parametrize(
    ("class_id", "tradition", "tradition2"),
    [
        ("mage", "high_mage", None),
        ("adventurer_expert_mage", "high_mage", None),
        ("adventurer_warrior_mage", "high_mage", None),
        ("adventurer_mage_mage", "high_mage", "necromancer_partial_2"),
        ("healer", None, None),
        ("vowed", None, None),
    ],
)
def test_mage_classes_cannot_learn_forbidden_foci_during_advancement(
    client_factory: Callable[[], TestClient],
    class_id: str,
    tradition: str | None,
    tradition2: str | None,
    focus: str,
) -> None:
    app, campaign_id = _campaign(
        client_factory, class_id=class_id, tradition=tradition, tradition2=tradition2
    )
    tools = _world_tools(app, campaign_id)
    _give_pick(tools, "focus_picks", 1)

    with pytest.raises(InvalidInput, match="not available"):
        wwn.learn_focus(NAME, focus, _ctx(tools))
    assert _counter(app, campaign_id, "focus_picks") == 1
    assert focus not in _pc(app, campaign_id).features


@pytest.mark.parametrize(
    ("class_id", "tradition", "focus", "allowed"),
    [
        ("warrior", None, "focus_armored_magic", False),
        ("warrior", None, "focus_polymath", False),
        ("warrior", None, "focus_specialist", True),
        ("expert", None, "focus_polymath", True),
        ("expert", None, "focus_alert", True),
        ("mage", "high_mage", "focus_armored_magic", True),
        ("adventurer_expert_mage", "high_mage", "focus_polymath", True),
        ("healer", None, "focus_armored_magic", True),
        ("vowed", None, "focus_armored_magic", True),
    ],
)
def test_focus_advancement_uses_general_not_class_specific_pick_restrictions(
    client_factory: Callable[[], TestClient],
    class_id: str,
    tradition: str | None,
    focus: str,
    allowed: bool,
) -> None:
    app, campaign_id = _campaign(client_factory, class_id=class_id, tradition=tradition)
    tools = _world_tools(app, campaign_id)
    _give_pick(tools, "focus_picks", 1)

    if allowed:
        wwn.learn_focus(NAME, focus, _ctx(tools))
        assert _pc(app, campaign_id).features[focus] == 1
        assert _counter(app, campaign_id, "focus_picks") == 0
    else:
        with pytest.raises(InvalidInput, match="not available"):
            wwn.learn_focus(NAME, focus, _ctx(tools))
        assert _counter(app, campaign_id, "focus_picks") == 1


def test_focus_advancement_rechecks_class_eligibility_for_a_higher_rank(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    tools = _world_tools(app, campaign_id)
    _give_pick(tools, "focus_picks", 1)
    with Session(app.state.engine) as db:
        record(
            db,
            campaign_id,
            FeatureLearned(name=NAME, feature="focus_nullifier", feature_name="Nullifier"),
        )
        db.commit()

    with pytest.raises(InvalidInput, match="not available"):
        wwn.learn_focus(NAME, "focus_nullifier_2", _ctx(tools))
    assert _counter(app, campaign_id, "focus_picks") == 1
    assert "focus_nullifier_2" not in _pc(app, campaign_id).features


def test_learn_spell_adds_a_tradition_spell_of_a_legal_level(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)

    result = wwn.learn_spell(NAME, "spell_apprehending_arcane_form", ctx)

    assert result["rank"] == 1
    assert result["tradition"] == "high"
    assert "spell_apprehending_arcane_form" in _pc(app, campaign_id).features

    with pytest.raises(InvalidInput, match="can cast spells of level 1 at most"):
        wwn.learn_spell(NAME, "spell_calculation_evoked_servitor", ctx)
    with pytest.raises(InvalidInput, match="not a High Magic spell"):
        wwn.learn_spell(NAME, "spell_command_the_dead", ctx)
    with pytest.raises(InvalidInput, match="is not a spell"):
        wwn.learn_spell(NAME, "focus_alert", ctx)
    with pytest.raises(InvalidInput, match="already knows"):
        wwn.learn_spell(NAME, "spell_apprehending_arcane_form", ctx)


def test_set_ac_preserves_an_adjustment_when_equipment_changes(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    _set_stat(tools, "dex", 14)  # a +1 modifier
    # The kit's shield would add its own point over the Buff Coat, so set it aside.
    tools.drop_item(NAME, "Large Shield")
    tools.give_item(NAME, "Buff Coat")
    tools.equip_armor(NAME, "Buff Coat")

    result = tools.set_armor(NAME, 15)

    assert result["armor"] == 15
    assert _pc(app, campaign_id).defense_adjustment == 2
    armor = _events_of(app, campaign_id, ArmorSet.kind)[-1]
    assert (armor.name, armor.armor) == (NAME, 15)
    tools.equip_armor(NAME, "Buff Coat", equipped=False)
    assert _pc(app, campaign_id).armor == 13


def test_set_ac_requires_a_valid_total(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    schema = WWN.tool_calls["set_ac"].parameters

    assert check_arguments(schema, {"name": NAME, "ac": -1}) == ("argument 'ac' must be at least 5")
    assert check_arguments(schema, {"name": NAME, "ac": 31}) == ("argument 'ac' must be at most 30")

    assert tools.set_armor(NAME, 14)["armor"] == 14


def test_the_slow_experience_track_uses_its_own_thresholds(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    ctx = _ctx(_world_tools(app, campaign_id))

    below = wwn.gain_xp(NAME, 5, "a session", ctx, rate="slow")
    assert below["next_threshold"] == 6
    assert below["ready"] is False
    with pytest.raises(InvalidInput, match="on the slow track"):
        wwn.apply_level_up(NAME, ctx, rate="slow")

    at = wwn.gain_xp(NAME, 1, "another session", ctx, rate="slow")
    assert at["ready"] is True
    assert wwn.apply_level_up(NAME, ctx, rate="slow")["level"] == 2


def test_an_attribute_boost_costs_more_each_time_and_needs_a_level(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    _give_pick(tools, "skill_points", 20)

    with pytest.raises(InvalidInput, match="needs character level 2"):
        wwn.improve_attribute(NAME, "str", ctx)

    tools.adjust_counter(NAME, "level", 1)
    first = wwn.improve_attribute(NAME, "str", ctx)
    assert (first["boosts"], first["cost"], first["remaining"]) == (1, 1, 19)
    second = wwn.improve_attribute(NAME, "str", ctx)
    assert (second["boosts"], second["cost"], second["remaining"]) == (2, 2, 17)

    with pytest.raises(InvalidInput, match="needs character level 3"):
        wwn.improve_attribute(NAME, "str", ctx)


def test_level_up_keeps_the_old_hit_points_plus_one_when_the_reroll_is_worse(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id, seed=2)
    ctx = _ctx(tools)
    tools.set_stat_max(NAME, "hp", 20, cause="a test")

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.apply_level_up(NAME, ctx)

    # The old 20 plus one, plus Die Hard's 2.
    assert result["hp"] == 23
    assert _stat(app, campaign_id, "hp") == 23


def test_a_full_mage_starts_with_four_spells_and_two_arts(
    client_factory: Callable[[], TestClient],
) -> None:
    _app, _campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    character = _pc(_app, _campaign_id)
    spells = [f for f in character.features if f.startswith("spell_")]
    arts = [f for f in character.features if f in {"art_arcane_lexicon", "art_counter_magic"}]
    assert len(spells) == 4
    assert len(arts) == 2


def test_art_picks_come_from_the_class_chart_and_buy_arts(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.apply_level_up(NAME, ctx)
    assert result["art_picks"] == 1
    assert _counter(app, campaign_id, "art_picks") == 1

    learned = wwn.learn_art(NAME, "art_empowered_sorcery", ctx)
    assert learned["art_picks"] == 0
    assert "art_empowered_sorcery" in _pc(app, campaign_id).features

    with pytest.raises(InvalidInput, match="no art picks to spend"):
        wwn.learn_art(NAME, "art_hang_sorcery", ctx)


def test_a_focus_pick_can_buy_a_nonhuman_origin(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    _give_pick(tools, "focus_picks", 1)
    before = _pc(app, campaign_id)

    wwn.learn_focus(NAME, "focus_origin_dwarf", ctx)

    character = _pc(app, campaign_id)
    assert "focus_origin_dwarf" in character.features
    assert character.skills["craft"] == min(4, before.skills.get("craft", -1) + 1)


def test_a_warrior_cannot_take_a_magical_tradition(
    client_factory: Callable[[], TestClient],
) -> None:
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields["template-class"] = "warrior"
    fields["template-tradition"] = "high_mage"

    fields = _named_picks(fields)
    created = _create_character(client, campaign_id, name="Wren", fields=fields)

    error = re.search(r'<p class="form-error"[^>]*>(.*?)</p>', created.text, re.DOTALL)
    assert created.status_code == 400
    assert error is not None and "cannot be chosen" in error.group(1)


def test_invalid_manual_skill_cap_is_reported_on_the_creation_form(
    client_factory: Callable[[], TestClient],
) -> None:
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields.update(
        {
            "template-background": "hunter",
            "template-class": "warrior",
            "template-tradition": "no_tradition",
            "pick-skill-background_learning-0": "notice",
            "pick-skill-background_learning-1": "notice",
            "pick-skill-class_free_skill-0": "notice",
        }
    )

    fields = _named_picks(fields)
    created = _create_character(client, campaign_id, name="Wren", fields=fields)

    assert created.status_code == 400
    error = re.search(r'<p class="form-error"[^>]*>(.*?)</p>', created.text, re.DOTALL)
    assert error is not None
    assert "cannot raise 'notice' above level 1" in unescape(error.group(1))
    app = client.app
    assert isinstance(app, FastAPI)
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
        assert state.pc() is None
        assert not state.options

    fields["pick-skill-class_free_skill-0"] = "craft"
    accepted = _create_character(client, campaign_id, name="Wren", fields=_named_picks(fields))

    assert accepted.status_code == 303
    with Session(app.state.engine) as db:
        pc = load_state(db, campaign_id).pc()
        assert pc is not None
        assert pc.skills["notice"] == 1


def test_a_frail_character_recovers_no_hit_points_or_strain(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id = _campaign(client_factory, class_id="mage", tradition="high_mage")
    tools = _world_tools(app, campaign_id)
    tools.set_stat_max(NAME, "hp", 20, cause="a test")
    tools.set_stat_max(NAME, "strain", 10, cause="a test")
    tools.harm_character(NAME, "hp", 5, "a test")
    tools.add_condition(NAME, "frail", "a test")
    tools.give_item(NAME, "Rations, One Week")
    tools.give_item(NAME, "Waterskin")

    tools.rest("night", reason="a test")

    character = _pc(app, campaign_id)
    assert character.harm["hp"] == 5
    assert "strain" not in character.harm


def test_a_night_rest_eats_a_days_rations(client_factory):
    """The source feeds a resting character; the engine spends the ration itself."""
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id)
    if not any(i.name == "Waterskin" for i in _pc(app, campaign_id).items):
        tools.give_item(NAME, "Waterskin")
    rations = next(i for i in _pc(app, campaign_id).items if i.name == "Rations, One Week")
    tools.harm_character(NAME, "hp", 2, "a test")

    result = tools.rest("night", reason="camp")

    after = _pc(app, campaign_id)
    left = next(i for i in after.items if i.name == "Rations, One Week")
    assert left.uses == rations.uses - 1
    assert NAME in result["recovered"]
    # A night restores hit points equal to the character's level.
    assert after.harm.get("hp", 0) == 1
    assert after.marks.get("privation_food") is None


def test_a_rest_uses_the_food_list_from_the_hooks_options(client_factory):
    """The rule set's food and water items are content, not Python constants."""
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id)
    tools.drop_item(NAME, "Rations, One Week")
    tools.give_item(NAME, "Daily Ration")
    options = dict(WWN.hooks.options)
    options["food_items"] = ["Daily Ration"]
    options["water_items"] = ["Daily Ration"]
    custom = replace(tools, hooks=replace(WWN.hooks, options=options))
    assert any(i.name == "Daily Ration" for i in _pc(app, campaign_id).items)

    result = custom.rest("night", reason="a test")

    assert not any(i.name == "Daily Ration" for i in _pc(app, campaign_id).items)
    assert result["held_back"] == []


def test_a_rest_refuses_missing_food_and_water_lists(client_factory):
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id)
    options = dict(WWN.hooks.options)
    options["food_items"] = []
    custom = replace(tools, hooks=replace(WWN.hooks, options=options))

    with pytest.raises(InvalidInput, match="food_items"):
        custom.rest("night", reason="a test")


def test_a_night_rest_begun_after_midnight_ends_the_same_morning():
    """The night part is the six hours before morning. Resting two hours into it ends four
    hours later at the next morning, not a day and four hours later."""
    # Day 2, night, hour 2: four hours before the next morning boundary.
    start = ((2 - 1) * len(WWN.clock.parts) + 3) * WWN.clock.hours_per_part + 2

    assert rest_target(start, WWN.rests["night"], WWN.clock) == start + 4


def test_stabilizing_without_a_heal_check_is_refused(client_factory):
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id)
    with Session(app.state.engine) as db:
        record(db, campaign_id, CharacterDying(name=NAME, hours=1))
        db.commit()

    with pytest.raises(InvalidInput, match="Heal check"):
        tools.stabilize(NAME, "A healer helps.")

    forced = tools.stabilize(NAME, "A healer helps.", force=True)

    assert forced["stabilized"] is True


def test_a_character_without_food_or_water_is_held_back_and_gains_strain(
    client_factory,
):
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id)
    tools.set_stat_max(NAME, "strain", 10, cause="a test")
    tools.drop_item(NAME, "Rations, One Week")
    if any(i.name == "Waterskin" for i in _pc(app, campaign_id).items):
        tools.drop_item(NAME, "Waterskin")
    tools.harm_character(NAME, "hp", 2, "a test")

    first = tools.rest("night", reason="no supplies")

    character = _pc(app, campaign_id)
    assert NAME in first["held_back"]
    assert NAME not in first["recovered"]
    assert character.harm.get("hp") == 2
    assert character.harm.get("strain") == 2
    assert character.marks.get("privation_food") == 1
    assert character.marks.get("privation_water") == 1

    second = tools.rest("night", reason="still no supplies")

    character = _pc(app, campaign_id)
    assert NAME in second["held_back"]
    assert character.harm.get("strain") == 2 + 1 + 3
    assert character.marks.get("privation_food") == 2
    assert character.marks.get("privation_water") == 2


def test_use_item_spends_a_torches_burn_time(client_factory):
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id)
    before = _pc(app, campaign_id)
    torch = next(i for i in before.items if i.name == "Torch")
    torches = sum(1 for i in before.items if i.name == "Torch")
    if not torch.ready:
        tools.ready_item(NAME, "Torch")

    result = tools.use_item(NAME, "Torch")

    assert result["used_up"] is True
    after = _pc(app, campaign_id)
    assert sum(1 for i in after.items if i.name == "Torch") == torches - 1


def test_a_dual_mage_takes_two_partial_traditions(client_factory):
    app, campaign_id = _campaign(
        client_factory,
        class_id="adventurer_mage_mage",
        tradition="high_mage_partial",
        tradition2="necromancer_partial_2",
    )
    pc = _pc(app, campaign_id)
    assert pc.templates["tradition"] == "high_mage_partial"
    assert pc.templates["tradition2"] == "necromancer_partial_2"
    assert "art_arcane_lexicon" in pc.features
    assert "art_bonetalker" in pc.features
    assert "spell_coruscating_coffin" in pc.features
    assert "spell_long_amber_moment" in pc.features
    assert pc.stats["effort_high"] >= 1
    assert pc.stats["effort_necromancer"] >= 1


def test_a_healer_starts_with_healing_touch_and_an_art(client_factory):
    app, campaign_id = _campaign(client_factory, class_id="healer")
    pc = _pc(app, campaign_id)
    assert pc.skills.get("heal") == 0
    assert "art_healing_touch" in pc.features
    assert "art_empowered_healer" in pc.features
    assert pc.stats["effort_healer"] >= 1


def test_a_vowed_starts_with_the_three_basic_arts_and_another(client_factory):
    app, campaign_id = _campaign(client_factory, class_id="vowed")
    pc = _pc(app, campaign_id)
    assert pc.skills.get("exert", -1) >= 0
    assert {"art_martial_style", "art_unarmed_might", "art_unarmored_defense"} <= set(pc.features)
    assert "art_brutal_counter" in pc.features
    assert pc.stats["effort_vowed"] >= 1


def test_vowed_unarmored_defense_tracks_equipment_and_level(client_factory):
    app, campaign_id = _campaign(client_factory, class_id="vowed")
    tools = _world_tools(app, campaign_id)
    # Unarmored Defense only applies without the starting kit's armor and shield.
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")
    assert _pc(app, campaign_id).armor == 14  # 13 + level // 2 + Dexterity

    tools.give_item(NAME, "Buff Coat")
    tools.equip_armor(NAME, "Buff Coat")
    assert _pc(app, campaign_id).armor == 13
    tools.equip_armor(NAME, "Buff Coat", equipped=False)
    assert _pc(app, campaign_id).armor == 14

    tools.give_item(NAME, "Small Shield")
    tools.equip_armor(NAME, "Small Shield")
    assert _pc(app, campaign_id).armor == 14
    tools.adjust_counter(NAME, "level", 1)
    assert _pc(app, campaign_id).armor == 14
    tools.equip_armor(NAME, "Small Shield", equipped=False)
    assert _pc(app, campaign_id).armor == 15
    tools.adjust_counter(NAME, "level", 1)
    assert _pc(app, campaign_id).armor == 15
    tools.adjust_counter(NAME, "level", 1)
    assert _pc(app, campaign_id).armor == 16


def test_the_experience_track_chosen_at_creation_guides_advancement(client_factory):
    app, campaign_id = _campaign(client_factory, options={"campaign-xp_rate": "slow"})
    ctx = _ctx(_world_tools(app, campaign_id))

    below = wwn.gain_xp(NAME, 5, "a session", ctx)
    assert below["next_threshold"] == 6
    assert below["ready"] is False

    at = wwn.gain_xp(NAME, 1, "another session", ctx)
    assert at["ready"] is True
    assert wwn.apply_level_up(NAME, ctx)["level"] == 2


def test_a_healer_learns_another_healer_art_at_second_level(client_factory):
    app, campaign_id = _campaign(client_factory, class_id="healer")
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)

    wwn.gain_xp(NAME, 3, "a session", ctx)
    result = wwn.apply_level_up(NAME, ctx)
    assert result["art_picks"] == 1
    learned = wwn.learn_art(NAME, "art_facile_healer", ctx)
    assert learned["art"] == "art_facile_healer"
    assert "art_facile_healer" in _pc(app, campaign_id).features


@pytest.mark.parametrize(
    ("body_armor", "shield", "expected"),
    [
        ("", "", 16),
        ("Buff Coat", "", 16),
        ("", "Small Shield", 17),
        ("", "Large Shield", 17),
        ("Buff Coat", "Large Shield", 17),
        ("Great Armor", "Large Shield", 20),
    ],
)
def test_impervious_defense_combines_with_body_armor_and_shields(
    client_factory, body_armor: str, shield: str, expected: int
):
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    # Start from bare skin, since this test is about the focus and the armor it chooses itself.
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")
    tools.adjust_counter(NAME, "focus_picks", 1)
    wwn.learn_focus(NAME, "focus_impervious_defense", ctx)

    for item in (body_armor, shield):
        if item:
            tools.give_item(NAME, item)
            tools.equip_armor(NAME, item)
    dexterity = WWN.character.modifier(_pc(app, campaign_id).stats, "dex")
    character = _pc(app, campaign_id)
    assert character.armor == expected + dexterity
    assert character.equipped_armor == body_armor
    assert character.equipped_shield == shield


@pytest.mark.parametrize(("level", "bonus"), [(1, 1), (2, 1), (3, 2), (4, 2)])
def test_impervious_defense_rounds_half_level_up(client_factory, level: int, bonus: int):
    app, campaign_id = _campaign(client_factory, class_id="warrior")
    tools = _world_tools(app, campaign_id)
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")
    if level > 1:
        tools.adjust_counter(NAME, "level", level - 1)
    tools.adjust_counter(NAME, "focus_picks", 1)
    ctx = _ctx(tools)
    wwn.learn_focus(NAME, "focus_impervious_defense", ctx)

    character = _pc(app, campaign_id)
    assert character.armor == 15 + bonus + WWN.character.modifier(character.stats, "dex")


def test_impervious_defense_updates_on_level_and_dexterity_changes(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")
    ctx = _ctx(tools)
    tools.adjust_counter(NAME, "focus_picks", 1)
    wwn.learn_focus(NAME, "focus_impervious_defense", ctx)
    assert _pc(app, campaign_id).armor == 17

    wwn.gain_xp(NAME, 6, "two sessions", ctx)
    wwn.apply_level_up(NAME, ctx)
    assert _pc(app, campaign_id).armor == 17
    wwn.apply_level_up(NAME, ctx)
    assert _pc(app, campaign_id).armor == 18
    _set_stat(tools, "dex", 3)
    assert _pc(app, campaign_id).armor == 15


def test_impervious_defense_applies_when_selected_at_creation(client_factory, monkeypatch):
    picks = {**CLASS_PICKS["warrior"], "pick-feature-class_focus_any-0": "focus_impervious_defense"}
    monkeypatch.setitem(CLASS_PICKS, "warrior", picks)
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")

    character = _pc(app, campaign_id)
    assert character.features["focus_impervious_defense"] == 1
    assert character.armor == 17


def test_a_warrior_can_start_with_both_ranks_of_a_focus(client_factory, monkeypatch):
    picks = {
        "pick-feature-class_focus_any-0": "focus_impervious_defense",
        "pick-feature-class_focus_warrior-0": "focus_impervious_defense",
    }
    monkeypatch.setitem(CLASS_PICKS, "warrior", picks)
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")

    character = _pc(app, campaign_id)
    assert character.features["focus_impervious_defense"] == 1
    assert character.features["focus_impervious_defense_2"] == 1
    assert character.armor == 17


def test_effect_operations_refresh_defense_after_counter_and_stat_changes(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")
    tools.adjust_counter(NAME, "focus_picks", 1)
    wwn.learn_focus(NAME, "focus_impervious_defense", _ctx(tools))
    assert _pc(app, campaign_id).armor == 17
    effects = EffectContext(
        rng=random.Random(0),
        combat=WWN.combat,
        counters=WWN.counters,
        conditions=WWN.conditions,
        spec=WWN.character,
    )
    with Session(app.state.engine) as db:
        run_effects(
            db,
            campaign_id,
            NAME,
            [{"op": "counter", "id": "level", "delta": 2}],
            effects,
            "a test",
            plus=WWN.plus,
        )
    assert _pc(app, campaign_id).armor == 18
    with Session(app.state.engine) as db:
        run_effects(
            db,
            campaign_id,
            NAME,
            [{"op": "raise_stat", "stat": "dex", "delta": 4, "cap": 18}],
            effects,
            "a test",
            plus=WWN.plus,
        )
    assert _pc(app, campaign_id).armor == 19


def test_generated_characters_and_hirelings_receive_passive_defense(client_factory):
    app, campaign_id = _campaign(client_factory)
    warrior = WWN.character.templates["warrior"]
    warrior = replace(
        warrior,
        features=(*warrior.features, Granted("buff_coat", "1"), Granted("large_shield", "1")),
    )
    spec = replace(WWN.character, templates={**WWN.character.templates, "warrior": warrior})
    tools = _world_tools(app, campaign_id, spec=spec)
    tools.create_location("Yard", "A quiet yard.")
    tools.move_actor(NAME, "Yard")
    choices = {
        "templates": {"class": "warrior", "tradition": "no_tradition"},
        "stats": {"dex": 14},
        "feature_picks": {
            "class_focus_any": ["focus_impervious_defense"],
            "class_focus_warrior": ["focus_alert"],
        },
    }
    tools.create_npc("Scout", "Yard", "A scout.", character=choices, force=True)
    tools.hire_hireling("Guide", NAME, 1, "A guide.", character=choices)
    with Session(app.state.engine) as db:
        record(
            db,
            campaign_id,
            NpcAdded(
                name="Tale",
                location="Yard",
                description="A stranger.",
                personality="",
                accent="",
                quirks=[],
                story_only=True,
            ),
        )
        db.commit()
    tools.update_npc("Tale", character=choices)
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    for name in ("Scout", "Guide", "Tale"):
        character = state.characters[name_key(name)]
        assert character.defense_tracked
        assert character.armor == 18
        assert (character.equipped_armor, character.equipped_shield) == (
            "Buff Coat",
            "Large Shield",
        )
        assert {"Buff Coat", "Large Shield"} <= {item.name for item in character.items}
        assert "buff_coat" not in character.features
        assert "large_shield" not in character.features
    tools.adjust_counter("Guide", "level", 2)
    with Session(app.state.engine) as db:
        guide = load_state(db, campaign_id).characters[name_key("Guide")]
    assert guide.armor == 19


@pytest.mark.parametrize("kind", ["npc", "hireling", "update"])
def test_generated_character_creation_rolls_back_if_defense_initialization_fails(
    client_factory, monkeypatch, kind
):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    tools.create_location("Yard", "A quiet yard.")
    tools.move_actor(NAME, "Yard")
    if kind == "update":
        with Session(app.state.engine) as db:
            record(
                db,
                campaign_id,
                NpcAdded(
                    name="Unfinished",
                    location="Yard",
                    description="A scout.",
                    personality="",
                    accent="",
                    quirks=[],
                    story_only=True,
                ),
            )
            db.commit()

    def fail_initialization(*args, **kwargs):
        raise InvalidInput("defense initialization failed")

    monkeypatch.setattr(world_service, "_initialize_generated_defense", fail_initialization)
    hireling_choices = {
        "templates": {"class": "warrior", "tradition": "no_tradition"},
    }
    with pytest.raises(InvalidInput, match="defense initialization failed"):
        if kind == "npc":
            tools.create_npc("Unfinished", "Yard", "A scout.", force=True)
        elif kind == "hireling":
            tools.hire_hireling("Unfinished", NAME, 1, "A guide.", character=hireling_choices)
        else:
            tools.update_npc("Unfinished", character={})
    with Session(app.state.engine) as db:
        characters = load_state(db, campaign_id).characters
        if kind == "update":
            assert characters[name_key("Unfinished")].story_only
        else:
            assert name_key("Unfinished") not in characters


def test_world_seed_initializes_generated_defense_but_preserves_statblocks(
    app: FastAPI, client_factory, monkeypatch
):
    template = app.state.content.world_templates["unwritten-world"]
    monster_id, monster = next(iter(WWN.monsters.items()))
    people = (
        NpcTemplate("scout", "Scout", "yard", "A scout.", None, None, None),
        NpcTemplate("beast", "Beast", "yard", "A beast.", None, None, None, monster=monster_id),
        NpcTemplate("story", "Story", "yard", "A stranger.", None, None, None, story_only=True),
    )
    monkeypatch.setitem(
        app.state.content.world_templates,
        "unwritten-world",
        replace(template, locations=(LocationTemplate("yard", "Yard", "A yard."),), npcs=people),
    )
    response = _new_campaign(
        _signed_in(client_factory), world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    scout = state.characters["scout"]
    assert scout.defense_tracked
    assert scout.armor == 10 + WWN.character.modifier(scout.stats, "dex")
    beast = state.characters["beast"]
    assert (beast.armor, beast.defense_tracked) == (monster.armor, False)
    assert state.characters["story"].story_only
    assert not state.characters["story"].defense_tracked


@pytest.mark.parametrize("ending", ["rest", "aid", "aid_item", "save"])
def test_indirect_condition_end_effects_refresh_defense(client_factory, ending: str):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")
    tools.adjust_counter(NAME, "focus_picks", 1)
    wwn.learn_focus(NAME, "focus_impervious_defense", _ctx(tools))
    condition = replace(
        WWN.conditions["frail"],
        ends=("rest", "aid"),
        on_end=({"op": "raise_stat", "stat": "dex", "delta": 4, "cap": 18},),
    )
    combat_rules = WWN.combat
    assert combat_rules is not None
    combat = replace(
        combat_rules,
        dying=replace(
            combat_rules.dying,
            dying_conditions=("frail",),
            fatal_conditions=("frail",),
            aid_items=("Bandages",),
        ),
    )
    tools = replace(tools, combat=combat, conditions={"frail": condition})
    with Session(app.state.engine) as db:
        record(db, campaign_id, ConditionAdded(name=NAME, condition="frail", cause="a test"))
        if ending in {"aid", "aid_item"}:
            record(db, campaign_id, CharacterDying(name=NAME, hours=1))
        if ending == "aid_item":
            record(
                db,
                campaign_id,
                ItemAdded(name=NAME, item="Bandages", size="normal", slots=1, uses=1, max_uses=1),
            )
        if ending == "save":
            record(
                db,
                campaign_id,
                Struck(
                    name=NAME, attacker="Foe", weapon="Claw", raw=0, armor=0, harm={}, save="dex"
                ),
            )
        db.commit()
    assert _pc(app, campaign_id).armor == 17

    if ending == "rest":
        tools.give_item(NAME, "Rations, One Week")
        tools.give_item(NAME, "Waterskin")
        tools.rest("extended")
    elif ending == "aid":
        tools.roll_check(
            "skill",
            NAME,
            skill="heal",
            stat="int",
            difficulty=8,
            cause="stabilizing a wound",
            roll=12,
        )
        tools.stabilize(NAME, "A healer helps.")
    elif ending == "aid_item":
        tools.use_item(NAME, "Bandages")
    else:
        roll = 20 if combat.save_succeeds(20, 14) else 1
        assert tools.player_save(roll)["passed"]

    character = _pc(app, campaign_id)
    assert character.conditions == []
    assert (character.stats["dex"], character.armor) == (18, 18)


def test_equipped_items_are_replaced_removed_or_stowed_explicitly(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    # The kit's spare weapons fill the Readied pool the items below need.
    tools.drop_item(NAME, "Short Sword")
    tools.drop_item(NAME, "Dagger")
    _set_stat(tools, "str", 18)
    for item in ("Buff Coat", "Pieced Armor", "Small Shield", "Large Shield", "Dagger"):
        tools.give_item(NAME, item)

    with pytest.raises(InvalidInput, match="not equippable"):
        tools.equip_armor(NAME, "Dagger")
    with pytest.raises(InvalidInput, match="carries no"):
        tools.equip_armor(NAME, "Plate Armor")
    tools.equip_armor(NAME, "Buff Coat")
    tools.equip_armor(NAME, "Small Shield")
    assert _pc(app, campaign_id).armor == 14
    tools.equip_armor(NAME, "Pieced Armor")
    tools.equip_armor(NAME, "Large Shield")
    character = _pc(app, campaign_id)
    assert (character.armor, character.equipped_armor, character.equipped_shield) == (
        16,
        "Pieced Armor",
        "Large Shield",
    )
    tools.ready_item(NAME, "Large Shield", False)
    character = _pc(app, campaign_id)
    assert (character.armor, character.equipped_shield) == (15, "")
    with pytest.raises(InvalidInput, match="stowed"):
        tools.equip_armor(NAME, "Large Shield")
    tools.ready_item(NAME, "Large Shield", True)
    assert _pc(app, campaign_id).equipped_shield == ""
    tools.equip_armor(NAME, "Large Shield")
    tools.drop_item(NAME, "Pieced Armor")
    character = _pc(app, campaign_id)
    assert (character.armor, character.equipped_armor, character.equipped_shield) == (
        15,
        "",
        "Large Shield",
    )


@pytest.mark.parametrize("operation", ["drop", "stow", "use", "consume"])
def test_removing_equipment_keeps_inventory_and_defense_in_one_transaction(
    client_factory, monkeypatch, operation
):
    app, campaign_id = _campaign(client_factory)
    marketplace = WWN.marketplace
    assert marketplace is not None
    original = next(item for item in marketplace.items if item.name == "Large Shield")
    items = tuple(
        replace(item, uses=1) if item.id == original.id else item for item in marketplace.items
    )
    tools = replace(
        _world_tools(app, campaign_id),
        marketplace=replace(marketplace, items=items),
    )
    # The test's shield replaces the starting kit's armor and shield.
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")
    _set_stat(tools, "str", 18)
    tools.give_item(NAME, "Large Shield")
    tools.equip_armor(NAME, "Large Shield")
    assert _pc(app, campaign_id).armor == 15

    def remove():
        if operation == "drop":
            tools.drop_item(NAME, "Large Shield")
        elif operation == "stow":
            tools.ready_item(NAME, "Large Shield", False)
        else:
            tools.use_item(NAME, "Large Shield")

    if operation == "consume":
        remove()
        character = _pc(app, campaign_id)
        assert character.armor == 11
        assert character.equipped_shield == ""
        assert all(item.name != "Large Shield" for item in character.items)
    else:

        def fail_equipment(*args, **kwargs):
            raise InvalidInput("equipment update failed")

        monkeypatch.setattr(world_service.combat, "set_defense_equipment", fail_equipment)
        with pytest.raises(InvalidInput, match="equipment update failed"):
            remove()
        character = _pc(app, campaign_id)
        shield = next(item for item in character.items if item.name == "Large Shield")
        assert (character.armor, character.equipped_shield, shield.ready, shield.uses) == (
            15,
            "Large Shield",
            True,
            1,
        )


def test_buying_defense_equips_better_items_and_keeps_adjustments(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")
    _set_stat(tools, "str", 18)
    tools.change_currency(NAME, 2000, cause="a test")

    tools.buy_item(NAME, "War Shirt")
    tools.buy_item(NAME, "Large Shield")
    character = _pc(app, campaign_id)
    assert (character.armor, character.equipped_armor, character.equipped_shield) == (
        15,
        "War Shirt",
        "Large Shield",
    )
    tools.set_armor(NAME, 17)
    tools.buy_item(NAME, "Buff Coat")
    tools.buy_item(NAME, "Small Shield")
    character = _pc(app, campaign_id)
    assert character.armor == 17
    assert character.defense_adjustment == 2
    assert character.equipped_armor == "Buff Coat"
    assert character.equipped_shield == "Large Shield"
    tools.buy_item(NAME, "Pieced Armor")
    assert _pc(app, campaign_id).armor == 18


def test_buying_set_and_add_armor_updates_slots_and_persistent_adjustment(client_factory):
    app, campaign_id = _campaign(client_factory)
    original_marketplace = WWN.marketplace
    assert original_marketplace is not None
    original = next(item for item in original_marketplace.items if item.name == "Buff Coat")
    replacement = replace(original, id="test_armor", name="Test Armor", armor=2, armor_mode="set")
    bonus = replace(original, id="ward", name="Ward", armor=2, armor_mode="add")
    marketplace = replace(
        original_marketplace, items=(*original_marketplace.items, replacement, bonus)
    )
    tools = replace(_world_tools(app, campaign_id), marketplace=marketplace)
    tools.drop_item(NAME, "Pieced Armor")
    tools.drop_item(NAME, "Large Shield")
    _set_stat(tools, "str", 18)
    tools.change_currency(NAME, 2000, cause="a test")

    tools.buy_item(NAME, "Pieced Armor")
    assert _pc(app, campaign_id).armor == 15
    tools.buy_item(NAME, "Test Armor")
    character = _pc(app, campaign_id)
    assert (character.armor, character.equipped_armor) == (13, "Test Armor")
    tools.buy_item(NAME, "Ward")
    character = _pc(app, campaign_id)
    assert (character.armor, character.defense_adjustment) == (15, 2)
    tools.equip_armor(NAME, "Test Armor", equipped=False)
    assert (_pc(app, campaign_id).armor, _pc(app, campaign_id).defense_adjustment) == (13, 2)


@pytest.mark.parametrize(
    ("package", "normal_ac", "focused_ac"),
    [
        ("pkg_armored_warrior", 15, 17),
        ("pkg_mage_healer_scholar", 10, 16),
        ("pkg_ranger_archer", 12, 16),
        ("pkg_roguish_wanderer", 13, 17),
        ("pkg_adventuring_peasant", 14, 17),
        ("pkg_gentry_wayfarer", 13, 16),
    ],
)
@pytest.mark.parametrize("focus", [False, True])
def test_starting_package_armor_respects_the_innate_floor(
    client_factory, package: str, normal_ac: int, focused_ac: int, focus: bool
):
    app, campaign_id = _campaign(client_factory, class_id="warrior", kit=package)
    tools = _world_tools(app, campaign_id)
    ctx = _ctx(tools)
    _set_stat(tools, "str", 18)
    if focus:
        tools.adjust_counter(NAME, "focus_picks", 1)
        wwn.learn_focus(NAME, "focus_impervious_defense", ctx)

    dexterity = WWN.character.modifier(_pc(app, campaign_id).stats, "dex")
    assert _pc(app, campaign_id).armor == (focused_ac if focus else normal_ac) + dexterity
