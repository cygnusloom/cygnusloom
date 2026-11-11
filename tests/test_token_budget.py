"""The prompt budget: tool-schema projection and rule-list detail.

The authored schemas stay authoritative for validation. These tests protect the cheaper wire
forms that the provider receives, since a regression here quietly multiplies every tool round.
"""

from __future__ import annotations

import json

import pytest

from cygnusloom.db.models import Campaign
from cygnusloom.services.campaigns import context_usage
from cygnusloom.services.events import CampaignState
from cygnusloom.services.prompts import system_parts
from cygnusloom.services.tools import tool_specs, wire_tool_specs
from tests.conftest import content_library

RULE_SETS = ("academia-arcana", "cairn2e", "worldswithoutnumber")


def _chars(specs):
    return sum(len(json.dumps(spec, separators=(",", ":"))) for spec in specs)


@pytest.mark.parametrize("rule_set_id", RULE_SETS)
def test_wire_modes_keep_every_tool_and_its_required_arguments(rule_set_id: str):
    calls = content_library().rule_sets[rule_set_id].tool_calls
    full = tool_specs(calls) or []
    compact = wire_tool_specs(calls, mode="compact") or []
    summary = wire_tool_specs(calls, mode="summary") or []
    brief = wire_tool_specs(calls, mode="brief") or []

    assert [spec["function"]["name"] for spec in compact] == [
        spec["function"]["name"] for spec in full
    ]
    for authored, wired in zip(full, compact, strict=True):
        assert wired["function"]["parameters"].get("required", []) == (
            authored["function"]["parameters"].get("required", [])
        )
        assert set(wired["function"]["parameters"]["properties"]) == set(
            authored["function"]["parameters"]["properties"]
        )

    assert _chars(compact) < _chars(full)
    assert _chars(summary) < _chars(compact)
    assert _chars(brief) < _chars(summary)


@pytest.mark.parametrize("rule_set_id", RULE_SETS)
def test_wire_modes_remove_the_repeated_character_schema(rule_set_id: str):
    calls = content_library().rule_sets[rule_set_id].tool_calls
    compact = wire_tool_specs(calls, mode="compact") or []
    character_schemas = [
        spec["function"]["parameters"]["properties"]["character"]
        for spec in compact
        if "character" in spec["function"]["parameters"].get("properties", {})
    ]
    large = [
        schema
        for schema in character_schemas
        if len(json.dumps(schema, separators=(",", ":"))) > 500
    ]

    assert character_schemas
    assert not large


@pytest.mark.parametrize("rule_set_id", RULE_SETS)
def test_wire_modes_meet_a_minimum_budget(rule_set_id: str):
    calls = content_library().rule_sets[rule_set_id].tool_calls
    full = _chars(tool_specs(calls) or [])
    compact = _chars(wire_tool_specs(calls, mode="compact") or [])
    summary = _chars(wire_tool_specs(calls, mode="summary") or [])
    brief = _chars(wire_tool_specs(calls, mode="brief") or [])

    assert compact <= full * 0.85
    assert summary <= full * 0.5
    assert brief <= full * 0.4


def test_rule_index_mode_lists_ids_without_rule_text():
    rules = {
        "attribute-swap": "Swap two attributes. The full text should not be in the index.",
        "hit-protection": "Hit Protection is the first harm. A long explanation follows.",
    }
    parts = system_parts(
        "rolltest",
        rules,
        frozenset(rules),
        "",
        0,
        rule_info_mode="index",
    )
    text = "\n\n".join(part.content for part in parts)

    assert "- attribute-swap" in text
    assert "- hit-protection" in text
    assert "The full text should not be in the index" not in text
    assert "read a rule with lookup_rule" in text.lower()


def test_full_rule_mode_includes_every_rule_in_full():
    rules = {"attribute-swap": "Swap two attributes. Then write them down."}
    parts = system_parts(
        "rolltest",
        rules,
        frozenset(),
        "",
        0,
        rule_info_mode="full",
    )
    text = "\n\n".join(part.content for part in parts)

    assert "- attribute-swap: Swap two attributes. Then write them down." in text


def test_context_usage_counts_tools_and_the_state_instruction():
    library = content_library()
    rule_set = library.rule_sets["cairn2e"]
    campaign = Campaign(
        owner_id=1,
        name="Meter",
        world_template="twin-lakes",
        rule_set="cairn2e",
        model="fake/scripted",
    )

    usage = context_usage(
        campaign,
        CampaignState(),
        [],
        rule_set,
        rule_set.campaign,
        32768,
        {},
        world=library.world_templates["twin-lakes"],
    )

    assert usage.tools > 0
    assert usage.fixed > 0
    assert usage.used == usage.fixed + usage.tools + usage.messages


def test_index_rules_save_prompt_tokens_over_brief_rules():
    library = content_library()
    rule_set = library.rule_sets["cairn2e"]
    campaign = Campaign(
        owner_id=1,
        name="Meter",
        world_template="twin-lakes",
        rule_set="cairn2e",
        model="fake/scripted",
    )

    def used(mode: str) -> int:
        return context_usage(
            campaign,
            CampaignState(),
            [],
            rule_set,
            rule_set.campaign,
            32768,
            {},
            world=library.world_templates["twin-lakes"],
            rule_info_mode=mode,
        ).fixed

    assert used("index") < used("brief") < used("full")
