from __future__ import annotations

from cygnusloom.services.prompts import (
    JOURNAL_INSTRUCTION,
    OOC_INSTRUCTION,
    STATE_INSTRUCTION,
    TURN_REVIEW_INSTRUCTION,
    journal_instruction,
    journal_review_instruction,
    narrate_instruction,
    speech_instruction,
    state_instruction,
)
from tests.conftest import content_library
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
    assert "narration above" in TURN_REVIEW_INSTRUCTION
    assert "Brevity is not a fault" in TURN_REVIEW_INSTRUCTION
    assert "Tense is a rule, not a style choice" in TURN_REVIEW_INSTRUCTION
    assert "call undo_narration with the tense slip as the fault" in TURN_REVIEW_INSTRUCTION
    assert "Call undo_narration before any tool that changes the record" in TURN_REVIEW_INSTRUCTION
    assert "never add a speculative action" in TURN_REVIEW_INSTRUCTION
    assert "Do not undo for style" in TURN_REVIEW_INSTRUCTION
    # The review judges the narration, not the GM's choices in the record.
    assert "do not add an action the turn did not take" in TURN_REVIEW_INSTRUCTION
    assert "clearly needed but did not take" not in TURN_REVIEW_INSTRUCTION


def test_ooc_instruction_does_not_restart_the_ooc_step():
    assert "call ooc" not in OOC_INSTRUCTION
    assert "state step already decided" in OOC_INSTRUCTION


def test_speech_instruction_without_a_ruleset_cap_adds_the_global_two_sentence_limit():
    text = speech_instruction(None)
    assert "say it, one sentence where you can and two at most, and under 40 words." in text


def test_speech_instruction_for_a_single_sentence_cap():
    assert "say it, one sentence, and under 40 words." in speech_instruction(1)


def test_speech_instruction_for_the_cairn_cap_of_two_stays_two():
    text = speech_instruction(2)
    assert "say it, one sentence where you can and two at most, and under 40 words." in text
    assert text.startswith("Speech step. Write only the words the speaker says aloud")


def test_speech_instruction_asks_for_plain_unhurried_lines():
    text = speech_instruction(None)
    assert "no similes, metaphors, or flowery phrasing" in text
    assert "do not string clauses together with dashes" in text
    assert "Keep the one or two points the intent gives and stop" in text


def test_every_rule_set_narrates_in_plain_present_tense():
    """The tense and plain-prose rules belong to the engine, not to one world or rule set."""
    for rule_set_id, rule_set in content_library().rule_sets.items():
        text = narrate_instruction(rule_set)
        assert "in the present tense" in text, rule_set_id
        assert 'not "Brenna counted the silver"' in text, rule_set_id
        assert "Do not use similes, metaphors, or poetic comparisons" in text, rule_set_id
        assert "under 40 words" in text, rule_set_id
        assert "under 80 words" in text, rule_set_id


def test_speech_instruction_falls_back_to_digits_above_the_word_table():
    assert "and 11 at most," in speech_instruction(11)


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
    assert "Bring the journal up to date first" in text
    assert "Campaign day 2. The journal so far:\nDay 1: We set out." in text
    assert text.index("Bring the journal up to date first") < text.index("Turn review step.")
    assert "narration above" in text


def test_cairn_prompt_chooses_the_cycle_from_the_situation():
    prompt = state_instruction(CAIRN)
    assert "Choose the cycle from the situation." in prompt
    assert "wilderness cycle is for broader long-term travel" in prompt
    assert "dungeon cycle is for a dungeon or other dangerous area" in prompt
    assert prompt.count("or in a safe area") == 2
