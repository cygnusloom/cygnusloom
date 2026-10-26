from __future__ import annotations

from cygnusforge.content.library import ContentLibrary, RuleSet
from cygnusforge.services.campaigns import STATE_INSTRUCTION, speech_instruction, state_instruction


def _cairn() -> RuleSet:
    return ContentLibrary.load().rule_sets["cairn2e"]


def _core() -> RuleSet:
    return ContentLibrary.load().core


def test_core_state_prompt_is_the_generic_loop_text_alone():
    prompt = state_instruction(_core())
    assert prompt.startswith(STATE_INSTRUCTION)
    assert "Cairn" not in prompt
    assert "relic" not in prompt
    assert "group" not in prompt
    for tool in ("advance_time", "rest", "give_item", "set_condition", "recharge_charged_item"):
        assert tool not in prompt


def test_cairn_state_prompt_keeps_its_fragments_in_order():
    prompt = state_instruction(_cairn())
    positions = [prompt.index(fragment.text) for fragment in _cairn().prompt_fragments]
    assert positions == sorted(positions)
    assert prompt.startswith(STATE_INSTRUCTION)
    assert "recharge_charged_item" in prompt
    assert "resolve_group_break" in prompt


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


def test_cairn_prompt_chooses_the_cycle_from_the_situation():
    prompt = state_instruction(_cairn())
    assert "Choose the cycle from the situation." in prompt
    assert "wilderness cycle is for broader long-term travel" in prompt
    assert "dungeon cycle is for a dungeon or other dangerous area" in prompt
    assert prompt.count("or in a safe area") == 2
