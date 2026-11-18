"""The worlds held up first when choosing a rule set and world pair.

A world is recommended for a rule set when it is built for that rule set, when the world names
the rule set in `recommended_rulesets`, or when the rule set names the world in
`recommended_worlds`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary
from tests.conftest import bundled_library
from tests.factories import signed_in_client
from tests.test_content import _core, _rule_set, _world, _write


def _tree(
    tmp_path: Path,
    *,
    world_rule_set: str | None = None,
    recommended_rulesets: list[str] | None = None,
    recommended_worlds: list[str] | None = None,
) -> Path:
    """A minimal content tree with core, testset, and other rule sets and one world."""
    world = _world()
    if world_rule_set is not None:
        world[0]["rule_set"] = world_rule_set
    if recommended_rulesets is not None:
        world[0]["recommended_rulesets"] = recommended_rulesets
    rule_set: list[dict[str, Any]] = _rule_set()
    if recommended_worlds is not None:
        rule_set[0]["recommended_worlds"] = recommended_worlds
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": world,
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": rule_set,
            "rulesets/other/rule_set.json": _rule_set("other"),
        },
    )
    return tmp_path


def test_bundled_rule_sets_recommend_their_preferred_worlds():
    library = bundled_library()

    assert library.recommended_world_ids("cairn2e") == set()
    assert library.recommended_world_ids("academia-arcana") == {"unwritten-academy"}
    assert library.recommended_world_ids("worldswithoutnumber") == {"unwritten-world"}
    assert library.recommending_rule_sets("unwritten-world") == (
        "dnd-srd-2024",
        "worldswithoutnumber",
    )


def test_offered_worlds_leave_out_other_rule_sets_and_put_recommended_first():
    library = bundled_library()

    assert [world.id for world in library.offered_worlds("cairn2e")] == ["unwritten-world"]
    assert [world.id for world in library.offered_worlds("academia-arcana")] == [
        "unwritten-academy",
        "unwritten-world",
    ]
    assert [world.id for world in library.offered_worlds("worldswithoutnumber")] == [
        "unwritten-world"
    ]


def test_a_world_built_for_a_rule_set_is_recommended_automatically(tmp_path: Path):
    library = ContentLibrary.load(_tree(tmp_path, world_rule_set="testset"))

    assert library.recommended_world_ids("testset") == {"testland"}
    assert library.recommending_rule_sets("testland") == ("testset",)


def test_a_world_can_recommend_a_rule_set_in_its_own_head(tmp_path: Path):
    library = ContentLibrary.load(_tree(tmp_path, recommended_rulesets=["testset"]))

    assert library.recommended_world_ids("testset") == {"testland"}
    assert library.recommending_rule_sets("testland") == ("testset",)


def test_a_rule_set_can_recommend_a_world(tmp_path: Path):
    library = ContentLibrary.load(_tree(tmp_path, recommended_worlds=["testland"]))

    assert library.recommended_world_ids("testset") == {"testland"}
    assert library.recommending_rule_sets("testland") == ("testset",)


def test_a_world_must_recommend_a_loaded_rule_set(tmp_path: Path):
    tree = _tree(tmp_path, recommended_rulesets=["missing"])

    with pytest.raises(ContentError, match="world recommends unknown rule set 'missing'"):
        ContentLibrary.load(tree)


def test_a_world_built_for_one_rule_set_cannot_recommend_another(tmp_path: Path):
    tree = _tree(tmp_path, world_rule_set="testset", recommended_rulesets=["other"])

    with pytest.raises(
        ContentError,
        match="world is built for 'testset', so it cannot recommend 'other'",
    ):
        ContentLibrary.load(tree)


def test_a_rule_set_must_recommend_a_loaded_world(tmp_path: Path):
    tree = _tree(tmp_path, recommended_worlds=["mars"])

    with pytest.raises(ContentError, match="rule set recommends unknown world 'mars'"):
        ContentLibrary.load(tree)


def test_a_rule_set_cannot_recommend_a_world_built_for_another(tmp_path: Path):
    tree = _tree(tmp_path, world_rule_set="other", recommended_worlds=["testland"])

    with pytest.raises(
        ContentError,
        match="rule set recommends 'testland', which is built for 'other'",
    ):
        ContentLibrary.load(tree)


@pytest.mark.parametrize(
    ("argument", "message"),
    [
        ("recommended_rulesets", "world: 'recommended_rulesets' repeats an entry"),
        ("recommended_worlds", "rule_set: 'recommended_worlds' repeats an entry"),
    ],
)
def test_a_recommendation_list_does_not_repeat_an_entry(
    tmp_path: Path, argument: str, message: str
):
    tree = _tree(tmp_path, **{argument: ["testland", "testland"]})

    with pytest.raises(ContentError, match=message):
        ContentLibrary.load(tree)


def test_the_new_campaign_form_groups_recommended_worlds_first(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)

    page = client.get("/campaigns/new").text

    recommended = page.index('data-world-group="recommended"')
    other = page.index('data-world-group="other"')
    assert recommended < other
    assert "Recommended for Worlds Without Number" in page
    group = page[recommended:other]
    assert '<option value="unwritten-world"' in group
    assert '<option value="twin-lakes"' not in group
    assert '<option value="unwritten-academy"' not in group
    assert "data-recommended-for='[\"dnd-srd-2024\", \"worldswithoutnumber\"]'" in group


def test_the_campaign_pickers_open_from_the_summary(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)

    page = client.get("/campaigns/new").text

    for name in ("rule_set", "world_template"):
        assert page.count(f'data-open-pick="pick-dialog-{name}"') == 2


def test_the_compendium_groups_recommended_worlds_first(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)

    page = client.get("/compendium?rule_set=cairn2e&world_template=twin-lakes").text

    recommended = page.index('data-world-group="recommended"')
    other = page.index('data-world-group="other"')
    assert recommended < other
    assert "Recommended for Cairn 2e" in page
    group = page[recommended:other]
    assert '<option value="twin-lakes"' not in group
    assert '<option value="unwritten-world"' not in group
    assert '<option value="twin-lakes"' in page[other:]
