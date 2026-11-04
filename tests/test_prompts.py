from __future__ import annotations

from cygnusloom.services.prompts import (
    JOURNAL_INSTRUCTION,
    OOC_INSTRUCTION,
    STATE_INSTRUCTION,
    TURN_REVIEW_INSTRUCTION,
    journal_instruction,
    narrate_instruction,
    speech_instruction,
    state_instruction,
)
from tests.factories import rules_for

# Shipped Cairn content, for the tests about its own prompt fragments.
CAIRN = rules_for("cairn2e")


def test_a_rule_set_without_prompt_fragments_gets_the_generic_loop_text_alone():
    prompt = state_instruction(rules_for())
    assert prompt.startswith(STATE_INSTRUCTION)
    assert "Cairn" not in prompt
    assert "relic" not in prompt
    assert "group" not in prompt
    for tool in ("advance_time", "rest", "give_item", "set_condition", "recharge_charged_item"):
        assert tool not in prompt


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
    assert "recharge_charged_item" in prompt
    assert "resolve_group_break" in prompt


def test_narration_advice_stays_out_of_the_state_prompt():
    narration = [fragment for fragment in CAIRN.prompt_fragments if fragment.step == "narration"]
    assert narration
    state = state_instruction(CAIRN)
    told = narrate_instruction(CAIRN)
    for fragment in narration:
        assert fragment.text not in state
        assert fragment.text in told


def test_turn_review_instruction_is_generic():
    assert TURN_REVIEW_INSTRUCTION.startswith("Turn review step.")
    assert "player's message" in TURN_REVIEW_INSTRUCTION
    assert "what the GM did" in TURN_REVIEW_INSTRUCTION
    assert "narration below" in TURN_REVIEW_INSTRUCTION
    assert "Call undo_narration before any other tool" in TURN_REVIEW_INSTRUCTION
    assert "Prefer the smallest correction" in TURN_REVIEW_INSTRUCTION
    assert "do not add speculative actions" in TURN_REVIEW_INSTRUCTION


def test_ooc_instruction_does_not_restart_the_ooc_step():
    assert "call ooc" not in OOC_INSTRUCTION
    assert "state step already decided" in OOC_INSTRUCTION


def test_speech_instruction_without_a_cap_has_no_numeric_limit():
    assert "say it, one sentence where you can." in speech_instruction(None)


def test_speech_instruction_for_a_single_sentence_cap():
    assert "say it, one sentence." in speech_instruction(1)


def test_speech_instruction_for_the_cairn_cap_of_two_is_unchanged():
    text = speech_instruction(2)
    assert "say it, one sentence where you can and two at most." in text
    assert text.startswith("Speech step. Write only the words the speaker says aloud")


def test_speech_instruction_falls_back_to_digits_above_the_word_table():
    assert "and 11 at most." in speech_instruction(11)


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


def test_cairn_prompt_chooses_the_cycle_from_the_situation():
    prompt = state_instruction(CAIRN)
    assert "Choose the cycle from the situation." in prompt
    assert "wilderness cycle is for broader long-term travel" in prompt
    assert "dungeon cycle is for a dungeon or other dangerous area" in prompt
    assert prompt.count("or in a safe area") == 2
