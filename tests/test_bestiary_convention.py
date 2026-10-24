from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from fastapi.testclient import TestClient

from cygnusforge.services.world import present_rules_text
from tests.conftest import content_library
from tests.test_combat import YARD, _fight
from tests.test_world import _state

LIBRARY = content_library()
RULE_SET = LIBRARY.rule_sets["cairn2e"]
TWIN_LAKES = LIBRARY.world_content("twin-lakes", "cairn2e")
CRITICAL = "Critical Damage:"


def _normal(text: str) -> str:
    return " ".join(text.replace("\u2019", "'").split())


def test_every_bestiary_rule_text_is_verbatim_in_its_description():
    for monster in RULE_SET.monsters.values():
        description = _normal(monster.description)
        for rule_id in monster.rules:
            text = _normal(RULE_SET.attached_rules[rule_id])
            assert text in description, (
                f"{monster.id}: {rule_id} is not verbatim in the description"
            )


def test_every_bestiary_rule_is_a_whole_sentence_run_of_its_description():
    for monster in RULE_SET.monsters.values():
        for rule_id in monster.rules:
            text = RULE_SET.attached_rules[rule_id]
            assert text == text.strip(), f"{rule_id} has stray whitespace"
            assert text.endswith((".", ")", "!", "?")), f"{rule_id} is cut mid-sentence"


def test_a_monster_with_critical_damage_has_that_rule_in_its_rules():
    ghost = RULE_SET.monsters["ghost"]
    assert "ghost-critical-damage" in ghost.rules
    rule = RULE_SET.attached_rules["ghost-critical-damage"]
    assert rule.startswith(CRITICAL)
    assert _normal(rule) in _normal(ghost.description)


def test_every_critical_damage_rule_is_listed_by_its_monster():
    checked = 0
    for monster in RULE_SET.monsters.values():
        for rule_id in monster.rules:
            if rule_id.endswith("-critical-damage"):
                assert RULE_SET.attached_rules[rule_id].startswith(CRITICAL), rule_id
                checked += 1
    assert checked >= 20


def test_a_group_member_at_the_pc_location_brings_its_rules(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(tools, monsters={**tools.monsters, **TWIN_LAKES.monsters})
    tools.create_monster("Bandits", YARD)
    state = _state(app, campaign_id)
    bandits = state.named("Bandits")
    assert bandits is not None
    assert "morale-bandits" in bandits.rules
    rules = {**RULE_SET.rules, **TWIN_LAKES.rules, **RULE_SET.attached_rules}
    text = present_rules_text(state, rules)
    assert "- morale-bandits (from Bandits):" in text


def test_a_monster_present_brings_its_bullet_rules(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    tools.create_monster("Ghost", YARD)
    state = _state(app, campaign_id)
    ghost = state.named("Ghost")
    assert ghost is not None
    assert "ghost-rule-1" in ghost.rules
    assert "ghost-critical-damage" in ghost.rules
    text = present_rules_text(state, {**RULE_SET.rules, **RULE_SET.attached_rules})
    assert "- ghost-critical-damage (from Ghost):" in text
    assert "Target must save WIL or is possessed" in text
