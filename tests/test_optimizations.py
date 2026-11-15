"""Regression tests for context and review optimizations."""

from __future__ import annotations

import json

from cygnusloom.services.events import CampaignState, ContextPart, Turn
from cygnusloom.services.turns import (
    _compact_old_tool_results,
    _review_only_context,
    _speech_messages,
)


def _round(call_id: str, name: str, result: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def test_old_lookup_results_stay_readable_while_action_results_compact():
    lookup = json.dumps(
        {"location": "The Kettle", "npcs": [{"name": "Dorna Hask", "personality": "brisk"}]}
    )
    action = json.dumps({"name": "Dorn", "location": "The Kettle", "status": "created"})
    messages = [
        *_round("lookup", "all_at_location", lookup),
        *_round("action", "create_npc", action),
        *_round("newer", "roll_move", "{}"),
    ]

    compacted, changed = _compact_old_tool_results(
        messages,
        1,
        preserve_names=frozenset({"all_at_location"}),
        preserve_chars=2000,
    )

    assert changed
    by_id = {
        message["tool_call_id"]: message["content"]
        for message in compacted
        if message.get("role") == "tool"
    }
    # The first lookup is older than the full-result window, but remains a fact the state step
    # can use instead of looking it up again.
    assert "Dorna Hask" in by_id["lookup"]
    assert json.loads(by_id["lookup"]) == json.loads(lookup)
    # The older action result is summarized.
    assert by_id["action"] != action
    assert "create_npc:" in by_id["action"]


def test_oversized_lookup_results_are_capped():
    lookup = json.dumps({"notes": ["x" * 5000]})
    messages = [
        *_round("lookup", "search_notes", lookup),
        *_round("newer", "roll_move", "{}"),
    ]

    compacted, _ = _compact_old_tool_results(
        messages,
        1,
        preserve_names=frozenset({"search_notes"}),
        preserve_chars=2000,
    )

    result = next(m for m in compacted if m.get("tool_call_id") == "lookup")["content"]
    assert result.startswith("search_notes: ")
    assert result.endswith("[truncated]")
    assert len(result) < 2100


def test_only_the_newest_speech_brief_reaches_the_review():
    turn = Turn(
        opener=1,
        text="",
        context=[
            ContextPart("system", "Speech brief (round 1) for Mara", "old brief"),
            ContextPart("system", "Speech brief (round 2) for Mara", "new brief"),
            ContextPart("system", "Speech brief (round 2) for Ospak", "other brief"),
        ],
    )

    briefs = _review_only_context(turn)

    assert [part.content for part in briefs] == ["new brief", "other brief"]


def test_the_speech_brief_excludes_the_superseded_draft():
    turn = Turn(opener=1, text="hello")
    _, brief, _, _, prompt = _speech_messages(
        CampaignState(),
        turn,
        "Mara",
        "Speaker: Mara",
        "Warn Ada.",
        True,
        40,
        previous="The bridge is out, maybe.",
    )

    assert "Warn Ada." in brief
    assert "The bridge is out, maybe." not in brief
    assert "Warn Ada." in prompt[0]["content"]
