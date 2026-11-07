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
    assert "coverage is" in TURN_REVIEW_INSTRUCTION
    assert "a recorded event the player can perceive that the narration leaves out" in (
        TURN_REVIEW_INSTRUCTION
    )
    assert "Formatting is also a fault" in TURN_REVIEW_INSTRUCTION
    assert "Tense is a rule, not a style choice" in TURN_REVIEW_INSTRUCTION
    assert "call undo_narration with the tense slip as the fault" in TURN_REVIEW_INSTRUCTION
    assert "Call undo_narration before any tool that changes the record" in TURN_REVIEW_INSTRUCTION
    assert "never add a speculative action" in TURN_REVIEW_INSTRUCTION
    assert "Do not undo for style" in TURN_REVIEW_INSTRUCTION
    assert "tense, coverage, and formatting faults" in TURN_REVIEW_INSTRUCTION
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


def test_speech_instruction_asks_for_plain_modern_speech():
    text = speech_instruction(None)
    assert "not a character in an old novel" in text
    assert "everyday words, contractions" in text
    assert "no old-fashioned or literary words" in text
    assert "Ignore the style of the narration" in text


def test_state_prompt_keeps_dialogue_for_moments_that_turn_on_speech():
    text = state_instruction(rules_for())
    assert "Call speak only when the moment turns on what is said" in text
    assert "Most actions need no dialogue at all" in text
    assert "An NPC the player has not addressed" in text
    assert "personality makes it likely" in text
    assert "when in doubt, keep them silent" in text


def test_narration_does_not_invent_dialogue_when_none_is_queued():
    text = narrate_instruction(CAIRN)
    assert "when no lines are queued, no NPC speaks in the narration" in text
    assert "Do not invent an NPC's greeting, remark, or reply" in text
    assert "not archaic or old-fashioned ones" in text


def test_every_rule_set_narrates_in_plain_present_tense():
    """The tense and plain-prose rules belong to the engine, not to one world or rule set."""
    for rule_set_id, rule_set in content_library().rule_sets.items():
        text = narrate_instruction(rule_set)
        assert "in the present tense" in text, rule_set_id
        assert 'not "Brenna counted the silver"' in text, rule_set_id
        assert "Do not use similes, metaphors, or poetic comparisons" in text, rule_set_id
        assert "under 40 words" in text, rule_set_id
        assert "under 80 words" in text, rule_set_id


def test_narration_covers_every_perceived_event_since_the_player_acted():
    text = narrate_instruction(CAIRN)
    assert "the turn log is the complete record" in text
    assert "must account for every entry the player can perceive" in text
    assert "Do not skip an entry just to stay short" in text
    assert "include every arrival, departure, move, roll and save outcome" in text
    assert "a later entry never happens before an earlier one" in text


def test_narration_separates_beats_and_dialogue_into_paragraphs():
    text = narrate_instruction(CAIRN)
    assert "short paragraphs separated by a blank line" in text
    assert "Start a new paragraph when the beat, scene, or speaker changes" in text
    assert "never put the whole turn in one block" in text
    assert "Give each line its own paragraph" in text
    assert "Never put dialogue in block quotes, bullet points, labels" in text


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
