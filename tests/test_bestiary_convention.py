from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from cygnusloom.services.views import present_rules_text
from tests.conftest import content_library
from tests.test_combat import YARD, _fight
from tests.test_world import _state

LIBRARY = content_library()
RULE_SET = LIBRARY.rule_sets["cairn2e"]
TWIN_LAKES = LIBRARY.world_content("twin-lakes", "cairn2e")


def test_a_group_member_at_the_pc_location_brings_its_rules(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    tools.create_monster("Bandit", YARD)
    state = _state(app, campaign_id)
    bandit = state.named("Bandit")
    assert bandit is not None
    assert "bandit-loyal" in bandit.rules
    rules = {**RULE_SET.rules, **RULE_SET.attached_rules, **TWIN_LAKES.rules}
    text = present_rules_text(state, rules)
    assert "- bandit-loyal (from Bandit):" in text


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
