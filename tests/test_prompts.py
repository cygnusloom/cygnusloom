from __future__ import annotations

from cygnusloom.services.prompts import (
    JOURNAL_INSTRUCTION,
    OOC_INSTRUCTION,
    OPENING_PROMPT,
    STATE_CLOSING,
    STATE_INSTRUCTION,
    SYSTEM_PROMPT,
    TURN_REVIEW_INSTRUCTION,
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


def test_a_rule_set_without_prompt_fragments_gets_the_generic_loop_text_alone():
    prompt = state_instruction(rules_for())
    assert prompt.startswith(STATE_INSTRUCTION)
    assert "Cairn" not in prompt
    assert "relic" not in prompt
    assert "group" not in prompt
    # Core inventory guidance names give_item, which every rule set inherits. The names below
    # are not part of the generic loop text.
    for tool in ("advance_time", "rest", "set_condition", "recharge_charged_item"):
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


def test_movement_within_a_local_map_is_recorded_before_the_narration():
    assert "place_map_token" in STATE_INSTRUCTION
    assert "onto a free cell beside what they approach" in STATE_INSTRUCTION
    assert "even when the message also speaks or acts" in STATE_INSTRUCTION
    told = narrate_instruction(rules_for())
    assert "only when a place_map_token or place_participant result records the move" in told


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


def test_speech_instruction_asks_for_one_quoted_line_only():
    text = speech_instruction(None)
    assert "inside double quotation marks" in text
    assert "no name, action, or stage direction outside them" in text


def test_state_prompt_keeps_dialogue_for_moments_that_turn_on_speech():
    text = state_instruction(rules_for())
    assert "Call speak only when the moment turns on what is said" in text
    assert "Most actions need no dialogue at all" in text
    assert "An NPC the player has not addressed" in text
    assert "personality makes it likely" in text
    assert "when in doubt, keep them silent" in text


def test_state_prompt_acts_only_on_the_current_player_message():
    text = state_instruction(rules_for())
    assert "The player alone initiates the player character's actions" in text
    assert "Put no deliberate action, words, or choice on the player character" in text
    assert "unless the player's current message states it or clearly implies it" in text
    assert "A plan from an earlier turn, an NPC's urging" in text
    assert "a scene that is ready to move on are not their choice now" in text
    assert "A social beat such as thanks, a greeting, a nod, or a goodbye" in text
    assert "do not resolve a plan the player meant to act on later" in text
    assert "when this action or its events take them" not in text


def test_state_prompt_covers_every_kind_of_player_choice():
    text = state_instruction(rules_for())
    assert "what they say, where they go, what they attack" in text
    assert "what they take, use, buy, give, or spend" in text
    assert "whether they sleep, and what they decide" in text


def test_state_prompt_still_allows_an_action_the_message_clearly_implies():
    text = state_instruction(rules_for())
    assert "accepting an offer or following someone who leaves" in text
    assert "when the player's message says or clearly implies that they go" in text


def test_narration_keeps_player_choices_to_the_message_or_its_clear_implication():
    text = narrate_instruction(CAIRN)
    assert "The player alone initiates the player character's actions" in text
    assert "states or clearly implies them" in text
    assert "An NPC's words or actions do not choose for them" in text


def test_the_state_and_narration_share_the_player_agency_rule():
    state = state_instruction(rules_for())
    told = narrate_instruction(CAIRN)
    assert "plan from an earlier turn" in state
    assert "a plan from an earlier turn" in SYSTEM_PROMPT
    assert "clearly implies" in told


def test_state_prompt_keeps_the_active_storylines_current():
    text = state_instruction(rules_for())
    assert "Keep the active storylines current" in text
    assert "update_storyline" in text
    assert "player-facing summary" in text
    assert "make it the focus with focus_storyline" in text
    assert "not the one that mattered last turn" in text
    assert "resolve it with resolve_storyline" in text
    assert "search_storylines still finds it" in text


def test_state_prompt_keeps_character_summaries_current():
    text = state_instruction(rules_for())
    assert "Keep each character's player-facing summary current" in text
    assert "describe_character" in text
    assert "keep the summary brief and free of GM-only secrets" in text


def test_the_state_step_requires_the_turn_checklist():
    text = state_instruction(rules_for())
    assert "turn checklist" in STATE_CLOSING
    assert STATE_CLOSING in text
    assert "finish_state_step" in text
    assert "The turn checklist does not apply to an out-of-character reply" in STATE_INSTRUCTION


def test_turn_checklist_covers_core_areas_and_the_rule_sets_optional_groups():
    text, keys = turn_checklist(rules_for().checklist, {"combat", "market"})
    assert keys[:8] == (
        "characters",
        "summaries",
        "movement",
        "notes",
        "storylines",
        "objects",
        "time",
        "rules",
    )
    assert "encounter" in keys
    assert "market" in keys
    assert "magic" not in keys
    assert "player-facing summary says what this action changed" in text
    assert "finish_state_step" in text
    for key in keys:
        assert f"- {key}:" in text


def test_checklist_areas_drop_groups_the_rule_set_does_not_offer():
    areas = checklist_areas(rules_for().checklist, {"combat"})
    assert [area.id for area in areas][:8] == [
        "characters",
        "summaries",
        "movement",
        "notes",
        "storylines",
        "objects",
        "time",
        "rules",
    ]
    assert "encounter" in [area.id for area in areas]
    assert "market" not in [area.id for area in areas]


def test_the_system_prompt_carries_the_checklist_as_its_own_part():
    checklist, _ = turn_checklist(rules_for().checklist, {"combat"})
    parts = system_parts("test", {}, frozenset(), "", 0, checklist=checklist)
    checklist_part = next(part for part in parts if part.source == "Turn checklist")
    assert checklist_part.content == checklist


def test_housekeeping_is_a_suggestion_not_a_command():
    text = state_instruction(rules_for())
    assert "Housekeeping block suggests what earlier scenes may have left behind" in text
    assert "It is a prompt for continuity, not a command" in text
    assert "leave it alone when the story is better served" in text
    assert "Treat it as fact, not suggestion" not in text


def test_core_storyline_rule_explains_focus_the_active_limit_and_splitting():
    text = content_library().core.rules["storylines"]
    assert "player-facing name and running summary" in text
    assert "Keep exactly one storyline focused" in text
    assert "at most three active storylines" in text
    assert "deactivate_storyline" in text
    assert "resolve_storyline" in text
    assert "search_storylines" in text
    assert "never hold a secret" in text
    assert "split it" in text
    assert "update the original" in text


def test_the_opening_creates_storylines_for_each_distinct_thread():
    text = OPENING_PROMPT
    assert "one for each distinct thread" in text
    assert "split it" in text
    assert "creating the other with create_storyline" in text
    assert "focus the one the opening starts with" in text


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


def test_core_owns_the_encounter_start_policy():
    library = content_library()
    core = next(
        fragment for fragment in library.core.prompt_fragments if fragment.id == "encounter_start"
    )
    assert core.requires == ("combat",)
    assert "create or choose a map" in core.text
    assert "start the encounter" in core.text
    assert "add every combatant" in core.text
    assert "settle initiative" in core.text


def test_each_combat_ruleset_adds_only_its_own_initiative_rule():
    library = content_library()
    world = library.world_templates["unwritten-world"]
    core = next(
        fragment for fragment in library.core.prompt_fragments if fragment.id == "encounter_start"
    )
    for rule_set_id, initiative_tool in (
        ("cairn2e", "player_initiative"),
        ("worldswithoutnumber", "roll_initiative"),
    ):
        rules = library.rule_sets[rule_set_id]
        prompt = state_instruction(rules, world)
        assert prompt.count(core.text) == 1, rule_set_id
        initiative = next(
            fragment for fragment in rules.prompt_fragments if fragment.id == "encounter_initiative"
        )
        assert initiative.text in prompt, rule_set_id
        assert prompt.index(core.text) < prompt.index(initiative.text), rule_set_id
        assert prompt.index("create or choose a map") < prompt.index("start the encounter")
        assert prompt.index("start the encounter") < prompt.index("add every combatant")
        assert prompt.index("add every combatant") < prompt.index(initiative_tool), rule_set_id


def test_a_ruleset_without_combat_gets_no_encounter_start():
    library = content_library()
    academy = library.rule_sets["academia-arcana"]
    core = next(
        fragment for fragment in library.core.prompt_fragments if fragment.id == "encounter_start"
    )
    assert core.id not in [fragment.id for fragment in academy.prompt_fragments]
    prompt = state_instruction(academy, library.world_templates["unwritten-world"])
    assert core.text not in prompt


def test_cairn_purchase_and_item_rules_come_from_the_generic_state_instruction():
    prompt = state_instruction(CAIRN)
    assert "Use buy_item only for objects a character keeps" in prompt
    assert "Characters use take_object to pick up an object" in prompt
    ids = [fragment.id for fragment in CAIRN.prompt_fragments]
    assert "purchases" not in ids
    assert "give_item_from_place" not in ids
