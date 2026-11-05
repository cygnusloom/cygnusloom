"""Genre tags on generation tables, and the rule-set and campaign choices they serve.

A table's `genres` says which settings it fits. A rule set offers an inherited core table when
it is untagged, tagged `generic`, or shares a tag with the rule set's selection. A campaign may
narrow the rule set's choice at creation, which filters the tables the GM's tools see.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import (
    DATA_DIR,
    TABLE_GENRES,
    ContentLibrary,
    _load_creative_table,
    _patch_creative_table,
    effective_table_genres,
)
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.services.campaigns import _tool_context
from cygnusforge.services.world import WorldTools
from tests.conftest import content_library
from tests.factories import campaign_ready
from tests.support import content_tree
from tests.test_campaigns import _new_campaign, _signed_in


def _dice_table(ident: str, genres: list[str] | None = None) -> dict[str, Any]:
    """A 1d2 dice table, tagged when `genres` is given."""
    definition: dict[str, Any] = {
        "type": "dice_table",
        "id": ident,
        "name": ident.replace("_", " ").title(),
        "dice": "1d2",
        "rows": [{"roll": "1", "result": "one"}, {"roll": "2", "result": "two"}],
    }
    if genres is not None:
        definition["genres"] = genres
    return definition


def _creative_table(ident: str, genres: list[str] | None = None) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "type": "creative_table",
        "id": ident,
        "entries": ["one", "two"],
    }
    if genres is not None:
        definition["genres"] = genres
    return definition


def test_a_table_reads_its_genre_tags(tmp_path: Path):
    tree = content_tree(
        tmp_path,
        core=[
            _dice_table("realm", ["fantasy"]),
            _creative_table("spark", ["generic", "fantasy"]),
            _dice_table("plain"),
        ],
    )
    library = ContentLibrary.load(tree)
    assert library.core.dice_tables["realm"].genres == ("fantasy",)
    assert library.core.dice_tables["spark"].genres == ("generic", "fantasy")
    assert library.core.dice_tables["spark"].creative
    assert library.core.dice_tables["plain"].genres == ()


@pytest.mark.parametrize(
    ("genres", "message"),
    [
        (["steampunk"], "unknown genre"),
        (["fantasy", "fantasy"], "only once"),
        (["fantasy", ""], "non-empty text"),
    ],
)
def test_a_bad_genre_tag_is_rejected(genres: list[str], message: str):
    with pytest.raises(ContentError, match=message):
        _load_creative_table(
            Definition(
                type="creative_table",
                id="plot",
                data={"type": "creative_table", "id": "plot", "entries": ["a"], "genres": genres},
            ),
            "testset",
        )


def test_a_core_table_is_left_out_when_the_rule_set_does_not_fit_it(tmp_path: Path):
    tree = content_tree(
        tmp_path,
        core=[
            _dice_table("realm", ["fantasy"]),
            _dice_table("hunt", ["horror"]),
            _dice_table("coin", ["generic"]),
            _dice_table("untagged"),
            _creative_table("spark", ["generic", "fantasy"]),
        ],
        rule_set={"table_genres": ["generic", "horror"]},
        definitions=[_dice_table("own_fantasy", ["fantasy"])],
    )
    library = ContentLibrary.load(tree)
    rules = library.rule_sets["testset"]
    assert rules.table_genres == ("generic", "horror")
    assert {"hunt", "coin", "untagged", "spark", "own_fantasy"} <= set(rules.dice_tables)
    assert "realm" not in rules.dice_tables
    # The sourcebook keeps every table, so the compendium can still describe them.
    assert "realm" in library.core.dice_tables


def test_a_rule_set_without_a_choice_offers_generic_and_its_own_genres(tmp_path: Path):
    tree = content_tree(
        tmp_path,
        core=[
            _dice_table("realm", ["fantasy"]),
            _dice_table("hunt", ["horror"]),
            _dice_table("coin", ["generic"]),
        ],
        definitions=[_dice_table("curse", ["horror"])],
    )
    rules = ContentLibrary.load(tree).rule_sets["testset"]
    assert rules.table_genres == ("generic", "horror")
    assert {"coin", "hunt", "curse"} <= set(rules.dice_tables)
    assert "realm" not in rules.dice_tables


def test_a_rule_set_refuses_an_unknown_genre(tmp_path: Path):
    content_tree(tmp_path, rule_set={"table_genres": ["steampunk"]})
    with pytest.raises(ContentError, match="unknown genre"):
        ContentLibrary.load(tmp_path)


def test_a_creative_table_patch_keeps_or_overrides_the_core_genres():
    core = {
        "plot": Definition(
            type="creative_table",
            id="plot",
            data={
                "type": "creative_table",
                "id": "plot",
                "entries": ["a", "b"],
                "genres": ["fantasy"],
            },
        )
    }
    inherited = _patch_creative_table(
        Definition(type="creative_table", id="plot", data={"add": ["c"]}),
        core,
        "testset",
    )
    assert _load_creative_table(inherited, "testset").genres == ("fantasy",)
    overridden = _patch_creative_table(
        Definition(
            type="creative_table",
            id="plot",
            data={"add": ["c"], "genres": ["generic"]},
        ),
        core,
        "testset",
    )
    assert _load_creative_table(overridden, "testset").genres == ("generic",)


def test_the_bundled_rule_sets_offer_generic_and_fantasy_tables():
    library = ContentLibrary.load(DATA_DIR)
    for rule_set_id in ("cairn2e", "worldswithoutnumber"):
        rules = library.rule_sets[rule_set_id]
        assert rules.table_genres == ("generic", "fantasy")
        assert rules.dice_tables["setting_premise"].genres == ("fantasy",)
        assert rules.dice_tables["action"].genres == ("generic",)
    assert library.core.dice_tables["setting_premise"].genres == ("fantasy",)


def test_a_campaign_without_a_choice_uses_the_rule_set_default():
    rules = ContentLibrary.load(DATA_DIR).rule_sets["cairn2e"]
    assert effective_table_genres("", rules) == ("generic", "fantasy")
    assert effective_table_genres('["horror"]', rules) == ("horror",)
    assert effective_table_genres("not json", rules) == ("generic", "fantasy")
    assert effective_table_genres("[]", rules) == ("generic", "fantasy")


def test_the_campaign_genres_filter_the_tables_the_tools_see(client_factory):
    client, campaign_id = campaign_ready(client_factory)
    library = content_library()
    rules = library.rule_sets["cairn2e"]
    world = library.world_content("twin-lakes", "cairn2e")

    def context(*genres: str) -> WorldTools:
        ctx = _tool_context(
            client.app.state.engine,
            random.Random(0),
            FakeProvider(),
            "fake/scripted",
            campaign_id,
            rules,
            world,
            table_genres=genres,
        )
        assert isinstance(ctx.world, WorldTools)
        return ctx.world

    generic = context("generic")
    assert "die_of_fate" in generic.dice_tables
    assert "dungeon_events" not in generic.dice_tables
    assert "setting_premise" not in generic.dice_tables
    listed = {table["id"] for table in generic.list_dice_tables()["tables"]}
    assert "die_of_fate" in listed
    assert "setting_premise" not in listed
    # A world's own tables are authored for it, so a genre choice never removes them.
    assert set(world.dice_tables) <= set(generic.dice_tables)

    # No selection at all keeps the rule set's default rather than dropping tagged tables.
    default = context()
    assert "dungeon_events" in default.dice_tables
    assert "setting_premise" in default.dice_tables


def test_the_new_campaign_form_hides_the_genre_picker(client_factory):
    client = _signed_in(client_factory)
    page = client.get("/campaigns/new").text
    start = page.index('<details class="table-genres">')
    end = page.index("</details>", start)
    section = page[start:end]
    assert "<summary>Creative and lore tables</summary>" in section
    assert "Optional." in section
    for genre in TABLE_GENRES:
        assert f'name="table_genres" value="{genre}"' in section
    # The form starts on the selected rule set's own choice: the bundled sets are generic+fantasy.
    assert 'name="table_genres" value="generic" checked' in section
    assert 'name="table_genres" value="fantasy" checked' in section
    assert 'name="table_genres" value="horror" checked' not in section
    assert " open" not in page[start : start + len('<details class="table-genres">')]


def test_a_campaign_may_choose_genres_the_rule_set_has_no_tables_for(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(
        client,
        world_template="twin-lakes",
        rule_set="cairn2e",
        table_genres=["generic", "historical"],
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/campaigns/")


def test_an_unknown_genre_is_refused_and_the_rest_stay_checked(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(
        client,
        world_template="twin-lakes",
        rule_set="cairn2e",
        table_genres=["generic", "steampunk"],
    )
    assert response.status_code == 400
    assert "unknown genre" in response.text
    assert 'name="table_genres" value="generic" checked' in response.text
    assert 'value="steampunk"' not in response.text
