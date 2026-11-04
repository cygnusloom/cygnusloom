"""Smoke tests for the Worlds Without Number ruleset: content, creation, checks, combat."""

from __future__ import annotations

import random
import re

import pytest
from sqlmodel import Session

from cygnusforge.content.character import (
    CharacterError,
    derived_stats,
    describe,
    feature_pick_options,
    resolve,
    skill_pick_options,
)
from cygnusforge.content.library import ContentLibrary
from cygnusforge.rules.wwn_progression import require_wwn_progression
from cygnusforge.services.events import NPC_KIND, PC_KIND, CampaignState, Character, load_state
from cygnusforge.services.views import get_statblock, scene_view
from tests.conftest import content_library
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_character_creation import _choices, _rolled_stats


def _wwn() -> ContentLibrary:
    return content_library()


def test_wwn_content_loads() -> None:
    rule_set = _wwn().rule_sets["worldswithoutnumber"]
    assert set(rule_set.character.stats) >= {
        "str",
        "dex",
        "con",
        "int",
        "wis",
        "cha",
        "hp",
        "save_physical",
        "save_evasion",
        "save_mental",
        "save_luck",
        "strain",
    }
    assert len(rule_set.character.skills) == 21
    assert rule_set.character.features
    assert rule_set.monsters
    assert rule_set.checks["skill"].dice == "2d6"
    assert rule_set.combat is not None
    assert rule_set.combat.hit_mode == "roll_vs_defense"
    assert rule_set.combat.shock_on_miss
    assert rule_set.combat.defeat_at_zero == ("hp",)
    assert rule_set.combat.defense_base == 10
    assert rule_set.combat.defense_modifier == "dex"
    assert rule_set.combat.natural_one_misses
    assert rule_set.combat.natural_max_hits
    assert len(rule_set.dice_tables) >= 70
    assert "dungeon_reaction" in rule_set.dice_tables
    assert "village_rationale" in rule_set.dice_tables
    assert any(f.tag == "origin" for f in rule_set.character.features.values())
    assert any(item.name == "Rope, 50 ft" for item in rule_set.marketplace.items)
    assert "no_unskilled_penalty" in rule_set.character.features["crossbow"].flags
    reaction = rule_set.dice_tables["dungeon_reaction"]
    assert "hostile" in reaction.row_for(2).result
    assert "friendly" in reaction.row_for(12).result


def test_wwn_strength_modifier_bands() -> None:
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    assert spec.modifier({"str": 3}, "str") == -2
    assert spec.modifier({"str": 7}, "str") == -1
    assert spec.modifier({"str": 10}, "str") == 0
    assert spec.modifier({"str": 14}, "str") == 1
    assert spec.modifier({"str": 18}, "str") == 2


def test_wwn_derived_saves_and_effort() -> None:
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    stats = {"str": 14, "dex": 8, "con": 18, "int": 12, "wis": 10, "cha": 3}
    templates = {"class": "mage", "tradition": "high_mage"}
    values = derived_stats(spec, stats, {"level": 3}, {"magic": 1}, templates)
    assert values["save_physical"] == 16 - 3 - 2
    assert values["save_evasion"] == 16 - 3
    assert values["save_mental"] == 16 - 3
    assert values["save_luck"] == 16 - 3
    assert values["strain"] == 18
    assert values["effort_high"] == 1 + 1 + 0
    # Only the chosen tradition's pool is present; the other traditions are left off.
    assert "effort_necromancer" not in values
    assert "effort_elemental" not in values
    assert "effort_healer" not in values
    assert "effort_vowed" not in values


def test_a_wwn_player_character_sheet_has_no_morale_but_a_statblock_does() -> None:
    """Morale is a statblock score. The source gives PCs no Morale, so it is not rolled at
    creation and a PC's sheet leaves it out, while an NPC or monster keeps the printed score."""
    ruleset = _wwn().rule_sets["worldswithoutnumber"]
    spec = ruleset.character
    assert "morale" not in {stat.id for stat in spec.stats.values() if stat.derived is None}
    sheet = resolve(spec, {"templates": {"class": "warrior"}}, random.Random(41), plus=ruleset.plus)
    state = CampaignState()
    state.characters["wren"] = Character(
        kind=PC_KIND,
        name="Wren",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
        stats=dict(sheet.stats),
    )
    block = get_statblock(state, "Wren", spec, ruleset.combat)
    assert "Morale" not in "\n".join(block["statblock"])
    assert all("Morale" not in line for line in scene_view(state, spec).sheet)

    state.characters["hound"] = Character(
        kind=NPC_KIND,
        name="Hound",
        location=None,
        description="A lean hound.",
        personality="",
        accent="",
        quirks=[],
        stats={**sheet.stats, "morale": 10},
    )
    npc_block = get_statblock(state, "Hound", spec, ruleset.combat)
    assert "Morale 10" in "\n".join(npc_block["statblock"])


def test_a_second_class_template_is_labelled_second_class() -> None:
    """A template kind with a trailing number names a later pick of the same kind. The sheet
    spells that out rather than printing the raw kind id."""
    ruleset = _wwn().rule_sets["worldswithoutnumber"]
    spec = ruleset.character
    sheet = resolve(
        spec,
        {"templates": {"class": "healer", "class2": "mage_partial_2"}},
        random.Random(41),
        plus=ruleset.plus,
    )
    text = describe(spec, sheet)
    assert "Second Class:" in text
    assert "Class2" not in text


def test_starting_attack_bonus_matches_each_class_progression() -> None:
    ruleset = _wwn().rule_sets["worldswithoutnumber"]
    progression = require_wwn_progression(ruleset)
    for class_id, chart in progression.class_tables.items():
        sheet = resolve(
            ruleset.character,
            {"templates": {"class": class_id}},
            random.Random(17),
            plus=ruleset.plus,
        )
        assert sheet.stats["attack_bonus"] == chart.attack[0], class_id


@pytest.mark.parametrize(("stat", "skill"), [("effort_healer", "heal"), ("effort_vowed", "exert")])
@pytest.mark.parametrize(("skill_level", "expected"), [(0, 1), (1, 2), (3, 4)])
def test_standalone_partial_mage_effort_uses_skill_and_modifier(stat, skill, skill_level, expected):
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    stats = {"str": 10, "dex": 10, "con": 10, "int": 14, "wis": 10, "cha": 10}
    templates = {"class": "vowed"} if stat == "effort_vowed" else {"class": "healer"}
    fields = {"order_skill": skill} if stat == "effort_vowed" else {}
    values = derived_stats(
        spec, stats, {"level": 1}, {skill: skill_level}, templates, fields=fields
    )
    assert values[stat] == expected


@pytest.mark.parametrize(
    ("template_id", "free_skills"),
    [
        ("artisan", ("craft",)),
        ("barbarian", ("survive",)),
        ("carter", ("ride",)),
        ("courtesan", ("perform",)),
        ("criminal", ("sneak",)),
        ("hunter", ("shoot",)),
        ("laborer", ("work",)),
        ("merchant", ("trade",)),
        ("noble", ("lead",)),
        ("nomad", ("ride",)),
        ("peasant", ("exert",)),
        ("performer", ("perform",)),
        ("physician", ("heal",)),
        ("priest", ("pray",)),
        ("sailor", ("sail",)),
        ("scholar", ("know",)),
        ("slave", ("sneak",)),
        ("soldier", ("stab",)),
        ("thug", ("punch",)),
        ("wanderer", ("survive",)),
        ("healer", ("heal",)),
    ],
)
def test_wwn_templates_grant_their_free_level_zero_skills(template_id, free_skills):
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    template = spec.templates[template_id]
    skill_picks = {
        pick.id: [
            skill_id
            for skill_id, _ in skill_pick_options(spec, pick)
            if skill_id not in free_skills
        ][: pick.count]
        for pick in template.skill_picks
    }
    feature_picks = {
        "class_focus_any": ["focus_unique_gift"],
        "class_art_healer": ["art_empowered_healer"],
        "class_art_vowed": ["art_brutal_counter"],
    }
    selected_features = {pick.id: feature_picks[pick.id] for pick in template.feat_picks}
    sheet = resolve(
        spec,
        {
            "templates": {template.kind: template_id},
            "skill_picks": skill_picks,
            "feature_picks": selected_features,
        },
        random.Random(12),
        plus="sum",
    )
    assert {skill: sheet.skills.get(skill) for skill in free_skills} == dict.fromkeys(
        free_skills, 0
    )


@pytest.mark.parametrize(
    ("class_id", "art_pick", "art"),
    [
        ("healer", "class_art_healer", "art_empowered_healer"),
        ("vowed", "class_art_vowed", "art_brutal_counter"),
    ],
)
def test_standalone_partial_classes_receive_the_general_skill_choice(class_id, art_pick, art):
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    skill_picks = {"background_learning": ["craft", "trade"], "class_free_skill": ["notice"]}
    if class_id == "vowed":
        skill_picks["order_skill"] = ["exert"]
    sheet = resolve(
        spec,
        {
            "templates": {"background": "artisan", "class": class_id},
            "skill_picks": skill_picks,
            "feature_picks": {"class_focus_any": ["focus_unique_gift"], art_pick: [art]},
        },
        random.Random(12),
        plus="sum",
    )
    assert sheet.skills["notice"] == 0


def test_vowed_order_skill_is_an_additional_noncombat_choice():
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    order = next(pick for pick in spec.templates["vowed"].skill_picks if pick.id == "order_skill")
    options = {skill for skill, _ in skill_pick_options(spec, order)}
    assert {"trade", "magic"} <= options
    assert not options.intersection({"punch", "shoot", "stab"})
    submitted = {
        "templates": {"background": "artisan", "class": "vowed"},
        "stats": {"str": 10, "dex": 10, "con": 10, "int": 18, "wis": 10, "cha": 10},
        "skill_picks": {
            "background_learning": ["craft", "trade"],
            "order_skill": ["trade"],
            "class_free_skill": ["punch"],
        },
        "feature_picks": {
            "class_focus_any": ["focus_unique_gift"],
            "class_art_vowed": ["art_brutal_counter"],
        },
    }
    sheet = resolve(spec, submitted, random.Random(12), plus="sum")
    assert sheet.skills["trade"] == 1
    assert sheet.skills["punch"] == 0
    assert "exert" not in sheet.skills
    assert sheet.fields["order_skill"] == "trade"
    assert sheet.stats["effort_vowed"] == 3

    submitted["skill_picks"]["order_skill"] = ["stab"]
    with pytest.raises(CharacterError):
        resolve(spec, submitted, random.Random(12), plus="sum")


@pytest.mark.parametrize("seed", range(8))
def test_random_vowed_order_skill_is_recorded_and_drives_effort(seed):
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    sheet = resolve(
        spec,
        {
            "templates": {"class": "vowed"},
            "skill_picks": {"class_free_skill": ["notice"]},
            "feature_picks": {
                "class_focus_any": ["focus_unique_gift"],
                "class_art_vowed": ["art_brutal_counter"],
            },
        },
        random.Random(seed),
        plus="sum",
    )
    skill = sheet.fields["order_skill"]
    best = max(
        spec.modifier(sheet.stats, stat) for stat in ("str", "dex", "con", "int", "wis", "cha")
    )
    assert sheet.stats["effort_vowed"] == max(1, sheet.skills[skill] + best)


def test_vowed_creation_form_labels_the_order_and_general_skill_choices(client_factory):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="latter-earth", rule_set="worldswithoutnumber")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    order = re.search(
        r'<fieldset[^>]*data-pick-selects="order_skill".*?</fieldset>', page, re.DOTALL
    )
    general = re.search(
        r'<fieldset[^>]*data-pick-selects="class_free_skill"[^>]*data-pick-template="vowed".*?</fieldset>',
        page,
        re.DOTALL,
    )
    assert order and "<h3>Order skill</h3>" in order.group()
    assert general and "<h3>General skill</h3>" in general.group()


def test_wwn_excess_focus_skill_uses_the_players_replacement():
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    sheet = resolve(
        spec,
        {
            "templates": {"background": "artisan", "class": "expert"},
            "skill_picks": {
                "background_learning": ["craft", "notice"],
                "class_free_skill": ["know"],
            },
            "feature_picks": {
                "class_focus_any": ["focus_artisan"],
                "class_focus_expert": ["focus_connected"],
            },
            "skill_replacements": {"feature": ["perform"]},
        },
        random.Random(12),
        plus="sum",
    )
    assert sheet.skills["craft"] == 1
    assert sheet.skills["perform"] == 0


def test_wwn_creation_form_persists_the_players_focus_skill_replacement(client_factory):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="latter-earth", rule_set="worldswithoutnumber")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields.update(
        {
            "template-background": "artisan",
            "template-class": "expert",
            "template-tradition": "no_tradition",
            "pick-skill-artisan-background_learning-0": "craft",
            "pick-skill-artisan-background_learning-1": "notice",
            "pick-skill-expert-class_free_skill-0": "know",
            "pick-feature-expert-class_focus_any-0": "focus_artisan",
            "pick-feature-expert-class_focus_expert-0": "focus_connected",
            "skill-replacement-feature": "perform",
        }
    )
    created = _create_character(client, campaign_id, name="Wren", fields=fields)
    assert created.status_code == 303, created.text
    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    assert pc.skills["craft"] == 1
    assert pc.skills["perform"] == 0


def test_wwn_creation_form_can_repeat_specialist_for_another_skill(client_factory):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="latter-earth", rule_set="worldswithoutnumber")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields.update(
        {
            "template-background": "artisan",
            "template-class": "expert",
            "template-tradition": "no_tradition",
            "pick-skill-artisan-background_learning-0": "notice",
            "pick-skill-artisan-background_learning-1": "trade",
            "pick-skill-expert-class_free_skill-0": "know",
            "pick-feature-expert-class_focus_any-0": "focus_specialist",
            "pick-feature-expert-class_focus_expert-0": "focus_specialist",
            "repeat-feature-expert-class_focus_expert-0": "repeat",
        }
    )
    created = _create_character(client, campaign_id, name="Wren", fields=fields)
    assert created.status_code == 303, created.text
    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    assert pc.features["focus_specialist"] == 2
    assert "focus_specialist_2" not in pc.features


def test_wwn_character_creation(client_factory) -> None:
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="latter-earth", rule_set="worldswithoutnumber")
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields.update(
        {
            "template-class": "warrior",
            "pick-feature-class_focus_any-0": "focus_die_hard",
            "pick-feature-class_focus_warrior-0": "focus_alert",
        }
    )
    created = _create_character(client, campaign_id, name="Wren", fields=fields)
    error = re.search(r'<p class="form-error"[^>]*>(.*?)</p>', created.text, re.DOTALL)
    assert created.status_code == 303, error.group(1) if error else "no error"
    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
    pc = state.pc()
    assert pc is not None
    assert (
        len([stat for stat in pc.stats if stat in {"str", "dex", "con", "int", "wis", "cha"}]) == 6
    )
    assert pc.skills
    assert "background" in pc.templates and "class" in pc.templates
    assert pc.templates["tradition"] == "no_tradition"
    assert pc.stats["hp"] >= 1
    assert pc.stats["save_physical"] <= 16
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    # The creation form gives every character the starting kit it picked, so the character is
    # armed and armored before the first turn.
    assert pc.templates["kit"] == "pkg_armored_warrior"
    assert pc.equipped_armor == "Pieced Armor" and pc.equipped_shield == "Large Shield"
    assert any(item.name == "Short Sword" for item in pc.items)
    assert pc.armor > 10 + spec.modifier(pc.stats, "dex")


def test_a_starting_kit_hands_over_the_books_gear(client_factory):
    """A character starts with the items, money, and armor of the kit the player chose, so the
    GM never has to invent gear the book already gives."""
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    kits = {kit.id: kit for kit in spec.templates.values() if kit.kind == "kit"}
    assert len(kits) == 6
    for kit in kits.values():
        assert kit.grant.items, kit.id
        assert all(kit.grant.items), kit.id
    scholar = kits["pkg_mage_healer_scholar"]
    assert scholar.grant.silver == 80
    assert "Staff" in scholar.grant.items


def test_a_spell_pick_offers_only_spells() -> None:
    spec = _wwn().rule_sets["worldswithoutnumber"].character
    for template in spec.templates.values():
        for pick in template.feat_picks:
            if not pick.id.startswith("tradition_spells_"):
                continue
            options = feature_pick_options(spec, pick)
            assert options
            assert all(feature.feature_type == "spell" for feature in options)
            assert all(feature.rank == 1 for feature in options)
