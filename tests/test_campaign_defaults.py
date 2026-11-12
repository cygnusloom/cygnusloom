from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary
from tests.conftest import bundled_library
from tests.factories import signed_in_client
from tests.test_content import _core, _rule_set, _world, _write


def _tree(tmp_path: Path, default_campaign: dict[str, str] | None = None) -> Path:
    """A minimal content tree, with an optional default on core's head."""
    core = _core()
    if default_campaign is not None:
        core[0]["default_campaign"] = default_campaign
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _world(),
            "rulesets/core/rule_set.json": core,
            "rulesets/testset/rule_set.json": _rule_set(),
        },
    )
    return tmp_path


def _selected_option(page: str, name: str) -> str:
    select = re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', page, re.DOTALL)
    assert select is not None
    selected = re.search(r'<option value="([^"]+)"[^>]* selected', select.group(1))
    assert selected is not None
    return selected.group(1)


def test_core_json_declares_the_new_campaign_defaults():
    default = bundled_library().core.default_campaign
    assert default is not None
    assert default.rule_set == "worldswithoutnumber"
    assert default.world_template == "unwritten-world"


def test_the_new_campaign_form_starts_on_the_core_defaults(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)

    page = client.get("/campaigns/new").text

    assert _selected_option(page, "rule_set") == "worldswithoutnumber"
    assert _selected_option(page, "world_template") == "unwritten-world"


def test_default_campaign_must_name_a_loaded_rule_set(tmp_path: Path):
    tree = _tree(tmp_path, {"rule_set": "missing", "world_template": "testland"})

    with pytest.raises(ContentError, match="unknown rule set 'missing'"):
        ContentLibrary.load(tree)


def test_default_campaign_must_name_a_loaded_world(tmp_path: Path):
    tree = _tree(tmp_path, {"rule_set": "testset", "world_template": "missing"})

    with pytest.raises(ContentError, match="unknown world 'missing'"):
        ContentLibrary.load(tree)


def test_default_campaign_world_must_fit_the_rule_set(tmp_path: Path):
    world = _world()
    world[0]["rule_set"] = "other"
    core = _core()
    core[0]["default_campaign"] = {"rule_set": "testset", "world_template": "testland"}
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": world,
            "rulesets/core/rule_set.json": core,
            "rulesets/testset/rule_set.json": _rule_set(),
            "rulesets/other/rule_set.json": _rule_set("other"),
        },
    )

    with pytest.raises(ContentError, match="is built for 'other', not 'testset'"):
        ContentLibrary.load(tmp_path)
