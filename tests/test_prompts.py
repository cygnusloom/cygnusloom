from __future__ import annotations

from cygnusloom.services.prompts import (
    JOURNAL_INSTRUCTION,
    OOC_INSTRUCTION,
    STATE_CLOSING,
    STATE_INSTRUCTION,
    checklist_areas,
    journal_instruction,
    journal_review_instruction,
    narrate_instruction,
    speech_instruction,
    state_instruction,
    system_parts,
    turn_checklist,
)
from tests.conftest import content_library
from tests.factories import rules_for

# Shipped Cairn content, for the tests about its own prompt fragments.
CAIRN = rules_for("cairn2e")


def test_dnd_tells_the_gm_that_a_hostile_spell_is_an_encounter_action():
    prompt = state_instruction(rules_for("dnd-srd-2024"))
    assert "A spell aimed at a hostile creature is an encounter action" in prompt


def test_a_rule_set_without_prompt_fragments_gets_the_generic_loop_text_alone():
    prompt = state_instruction(rules_for())
    assert prompt.startswith(STATE_INSTRUCTION)
    assert "Cairn" not in prompt
    assert "relic" not in prompt
    assert "group" not in prompt


def test_cairn_state_prompt_keeps_its_fragments_in_order():
    prompt = state_instruction(CAIRN)
    fragments = [
        fragment
        for fragment in CAIRN.prompt_fragments
        if fragment.step in ("state", "all") and not fragment.unwritten_only
    ]
    positions = [prompt.index(fragment.text) for fragment in fragments]
    assert positions == sorted(positions)
    assert prompt.startswith(STATE_INSTRUCTION)


def test_narration_advice_stays_out_of_the_state_prompt():
    narration = [fragment for fragment in CAIRN.prompt_fragments if fragment.step == "narration"]
    assert narration
    state = state_instruction(CAIRN)
    told = narrate_instruction(CAIRN)
    for fragment in narration:
        assert fragment.text not in state
        assert fragment.text in told


def test_the_state_step_requires_the_turn_checklist():
    text = state_instruction(rules_for())
    assert STATE_CLOSING in text


def test_checklist_areas_drop_groups_the_rule_set_does_not_offer():
    areas = checklist_areas(rules_for().checklist, {"combat"})
    assert "encounter" in [area.id for area in areas]
    assert "market" not in [area.id for area in areas]


def test_an_npc_turn_checklist_reminds_the_gm_about_attacks_and_movement():
    areas = checklist_areas(rules_for().checklist, {"combat"}, npc_turn=True)
    ids = [area.id for area in areas]
    assert "attack" not in [area.id for area in checklist_areas(rules_for().checklist, {"combat"})]
    assert ids.index("attack") == ids.index("movement") + 1
    assert ids.index("tactical_move") == ids.index("attack") + 1
    _, keys = turn_checklist(rules_for().checklist, {"combat"}, npc_turn=True)
    assert "attack" in keys and "tactical_move" in keys


def test_the_system_prompt_carries_the_checklist_as_its_own_part():
    checklist, _ = turn_checklist(rules_for().checklist, {"combat"})
    parts = system_parts("test", {}, frozenset(), "", 0, checklist=checklist)
    checklist_part = next(part for part in parts if part.source == "Turn checklist")
    assert checklist_part.content == checklist


def test_moved_prompts_keep_the_markers_the_fake_provider_matches():
    assert "Speech step." in speech_instruction(None)
    assert "Out-of-character step." in OOC_INSTRUCTION
    assert "Journal step." in JOURNAL_INSTRUCTION
    assert "Journal step." in journal_instruction(1, "")


def test_journal_instruction_keeps_its_day_and_journal_text():
    empty = journal_instruction(3, "")
    assert empty == f"{JOURNAL_INSTRUCTION}\n\nCampaign day 3. The journal is empty so far."
    kept = journal_instruction(4, "Day 1: We set out.")
    assert kept.endswith("Campaign day 4. The journal so far:\nDay 1: We set out.")


def test_combined_journal_review_instruction_does_both_jobs_in_order():
    text = journal_review_instruction(2, "Day 1: We set out.")
    assert text.startswith("Journal and review step.")
    assert "Campaign day 2. The journal so far:\nDay 1: We set out." in text
    assert text.index("Campaign day 2. The journal so far:") < text.index("Turn review step.")


def test_each_combat_ruleset_adds_only_its_own_initiative_rule():
    library = content_library()
    world = library.world_templates["unwritten-world"]
    core = next(
        fragment for fragment in library.core.prompt_fragments if fragment.id == "encounter_start"
    )
    for rule_set_id in ("cairn2e", "worldswithoutnumber"):
        rules = library.rule_sets[rule_set_id]
        prompt = state_instruction(rules, world)
        assert prompt.count(core.text) == 1, rule_set_id
        initiative = next(
            fragment for fragment in rules.prompt_fragments if fragment.id == "encounter_initiative"
        )
        assert initiative.text in prompt, rule_set_id
        assert prompt.index(core.text) < prompt.index(initiative.text), rule_set_id


def test_a_ruleset_without_combat_gets_no_encounter_start():
    library = content_library()
    academy = library.rule_sets["academia-arcana"]
    core = next(
        fragment for fragment in library.core.prompt_fragments if fragment.id == "encounter_start"
    )
    assert core.id not in [fragment.id for fragment in academy.prompt_fragments]
    prompt = state_instruction(academy, library.world_templates["unwritten-world"])
    assert core.text not in prompt
