"""Encounter map content (terrain, objects, starter templates) loaded from the core rule set."""

import re
from typing import Any

import pytest

from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.content.library import (
    ContentLibrary,
    EncounterCell,
    EncounterTemplate,
    _load_encounter_objects,
    _load_encounter_templates,
    _load_encounter_terrain,
)
from tests.conftest import bundled_library


@pytest.fixture(scope="module")
def library() -> ContentLibrary:
    return bundled_library()


def _definition(kind: str, **data: Any) -> Definition:
    return Definition(type=kind, id=data["id"], data={"type": kind, **data})


def _template(**overrides: Any) -> dict[str, Any]:
    template: dict[str, Any] = {
        "id": "box",
        "name": "Box",
        "columns": 4,
        "rows": 3,
        "background": "floor",
        "fills": [],
        "cells": [],
    }
    template.update(overrides)
    return template


def _load_templates(
    template: dict[str, Any], *, terrain: dict[str, Any] | None = None
) -> dict[str, EncounterTemplate]:
    terrain_defs = {
        "floor": {"name": "Floor", "color": "#4a4d55", "rule": "Normal ground."},
        "mud": {"name": "Mud", "color": "#5b4630", "rule": "Difficult ground."},
    }
    terrain_defs.update(terrain or {})
    definitions = [
        _definition("encounter_terrain", id=ident, **data) for ident, data in terrain_defs.items()
    ]
    definitions.append(
        _definition(
            "encounter_object", id="wall", name="Wall", icon="castle", color="#2b2d33", blocks=True
        )
    )
    definitions.append(_definition("encounter_template", **template))
    loaded_terrain = _load_encounter_terrain(definitions, "core")
    loaded_objects = _load_encounter_objects(definitions, "core")
    return _load_encounter_templates(definitions, loaded_terrain, loaded_objects, "core")


def _cells_by_position(template: EncounterTemplate) -> dict[tuple[int, int], EncounterCell]:
    return {(cell.column, cell.row): cell for cell in template.cells}


def test_all_rule_sets_see_the_same_encounter_content(library: ContentLibrary) -> None:
    assert "cairn2e" in library.rule_sets
    # A rule set may add local map content of its own, such as Hordes' modern streets and
    # interiors, but it never loses core's.
    for name, rule_set in library.rule_sets.items():
        assert library.core.terrain.items() <= rule_set.terrain.items(), name
        assert library.core.encounter_objects.items() <= rule_set.encounter_objects.items(), name
        assert (
            library.core.encounter_templates.items() <= rule_set.encounter_templates.items()
        ), name


# Loader validation ------------------------------------------------------------


def test_fills_then_cells_overwrite_in_order() -> None:
    templates = _load_templates(
        _template(
            fills=[{"from": [0, 0], "to": [3, 0], "object": "wall"}],
            cells=[{"column": 1, "row": 0, "terrain": "mud"}],
        )
    )
    cells = _cells_by_position(templates["box"])
    assert cells[(0, 0)] == EncounterCell(column=0, row=0, terrain="floor", object="wall")
    assert cells[(1, 0)] == EncounterCell(column=1, row=0, terrain="mud", object="wall")
    assert cells[(0, 1)] == EncounterCell(column=0, row=1, terrain="floor", object="")
    assert len(templates["box"].cells) == 12


def test_fill_corners_may_be_given_in_any_order() -> None:
    templates = _load_templates(_template(fills=[{"from": [3, 2], "to": [2, 1], "object": "wall"}]))
    walls = {(c.column, c.row) for c in templates["box"].cells if c.object == "wall"}
    assert walls == {(2, 1), (3, 1), (2, 2), (3, 2)}


def test_empty_background_means_no_terrain() -> None:
    templates = _load_templates(
        _template(background="", cells=[{"column": 0, "row": 0, "object": "wall"}])
    )
    assert templates["box"].background == ""
    assert templates["box"].cells == (EncounterCell(column=0, row=0, terrain="", object="wall"),)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"columns": 0}, "'columns' must be a whole number from 1 to 60"),
        ({"rows": 61}, "'rows' must be a whole number from 1 to 60"),
        ({"columns": True}, "'columns' must be a whole number from 1 to 60"),
        ({"background": "lava"}, "'background' must name a defined terrain"),
        ({"cells": [{"column": 4, "row": 0, "object": "wall"}]}, "outside the 4x3 grid"),
        ({"fills": [{"from": [0, 0], "to": [0, 3], "object": "wall"}]}, "outside the 4x3 grid"),
        ({"cells": [{"column": "1", "row": 0, "object": "wall"}]}, "must be a [column, row] pair"),
        ({"fills": [{"from": [0, 0], "object": "wall"}]}, "'to' must be a [column, row] pair"),
        (
            {"fills": [{"from": [0, 0, 1], "to": [1, 1], "object": "wall"}]},
            "'from' must be a [column, row] pair",
        ),
        (
            {"fills": [{"from": [0, 0], "to": [1, 1], "object": "nope"}]},
            "must name a defined encounter object",
        ),
        ({"cells": [{"column": 0, "row": 0, "terrain": "nope"}]}, "must name a defined terrain"),
        ({"cells": [{"column": 0, "row": 0}]}, "must set 'terrain' or 'object'"),
        ({"fills": "wall"}, "'fills' must be a list of objects"),
    ],
)
def test_invalid_template_raises_content_error(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ContentError, match=re.escape(message)):
        _load_templates(_template(**overrides))


def test_non_boolean_difficult_flag_raises() -> None:
    definitions = [
        _definition(
            "encounter_terrain",
            id="bog",
            name="Bog",
            color="#5b4630",
            rule="Difficult ground.",
            difficult="yes",
        )
    ]
    with pytest.raises(ContentError, match="'difficult' must be"):
        _load_encounter_terrain(definitions, "core")


def test_colour_must_be_a_six_digit_hex_value() -> None:
    definitions = [
        _definition("encounter_terrain", id="bog", name="Bog", color="swamp", rule="Bog.")
    ]
    with pytest.raises(ContentError, match="color must be a hex colour"):
        _load_encounter_terrain(definitions, "core")


def test_terrain_must_say_its_rule_effect() -> None:
    definitions = [_definition("encounter_terrain", id="bog", name="Bog", color="#5b4630")]
    with pytest.raises(ContentError, match="rule"):
        _load_encounter_terrain(definitions, "core")


def test_non_boolean_solid_flag_raises() -> None:
    definitions = [
        _definition(
            "encounter_object", id="rock", name="Rock", icon="rock", color="#6e6a62", solid="yes"
        )
    ]
    with pytest.raises(ContentError, match="'solid' must be"):
        _load_encounter_objects(definitions, "core")


def test_non_boolean_blocks_flag_raises() -> None:
    definitions = [
        _definition(
            "encounter_object", id="rock", name="Rock", icon="rock", color="#6e6a62", blocks=1
        )
    ]
    with pytest.raises(ContentError, match="'blocks' must be"):
        _load_encounter_objects(definitions, "core")


def test_duplicate_terrain_raises() -> None:
    definitions = [
        _definition(
            "encounter_terrain", id="grass", name="Grass", color="#3f6b35", rule="Normal ground."
        ),
        _definition(
            "encounter_terrain",
            id="grass",
            name="Grass again",
            color="#3f6b35",
            rule="Normal ground.",
        ),
    ]
    with pytest.raises(ContentError, match="duplicate encounter terrain 'grass'"):
        _load_encounter_terrain(definitions, "core")


def test_duplicate_object_raises() -> None:
    definitions = [
        _definition("encounter_object", id="tree", name="Tree", icon="tree", color="#2f5a2a"),
        _definition("encounter_object", id="tree", name="Tree again", icon="tree", color="#2f5a2a"),
    ]
    with pytest.raises(ContentError, match="duplicate encounter object 'tree'"):
        _load_encounter_objects(definitions, "core")


def test_duplicate_template_raises() -> None:
    definitions = [
        _definition("encounter_template", **_template()),
        _definition("encounter_template", **_template()),
    ]
    floor = [
        _definition(
            "encounter_terrain", id="floor", name="Floor", color="#4a4d55", rule="Normal ground."
        )
    ]
    with pytest.raises(ContentError, match="duplicate encounter template 'box'"):
        _load_encounter_templates(definitions, _load_encounter_terrain(floor, "core"), {}, "core")


def test_encounter_terrain_pattern_and_ink_are_validated() -> None:
    definitions = [
        _definition(
            "encounter_terrain",
            id="tiles",
            name="Tiles",
            color="#d9d4c8",
            rule="Normal ground.",
            pattern="tiles",
            ink="#b8b2a4",
            category="stone",
        )
    ]
    terrain = _load_encounter_terrain(definitions, "core")
    assert terrain["tiles"].pattern == "tiles"
    assert terrain["tiles"].ink == "#b8b2a4"
    assert terrain["tiles"].category == "stone"

    bad_pattern = [
        _definition(
            "encounter_terrain",
            id="tiles",
            name="Tiles",
            color="#d9d4c8",
            rule="Normal ground.",
            pattern="nope",
            ink="#b8b2a4",
        )
    ]
    with pytest.raises(ContentError, match="pattern must be one of"):
        _load_encounter_terrain(bad_pattern, "core")

    ink_without_pattern = [
        _definition(
            "encounter_terrain",
            id="tiles",
            name="Tiles",
            color="#d9d4c8",
            rule="Normal ground.",
            ink="#b8b2a4",
        )
    ]
    with pytest.raises(ContentError, match="'ink' needs a 'pattern'"):
        _load_encounter_terrain(ink_without_pattern, "core")


def test_encounter_object_icon_must_be_registered() -> None:
    definitions = [
        _definition(
            "encounter_object", id="teapot", name="Teapot", icon="no-such-icon", color="#8a5a3a"
        )
    ]
    with pytest.raises(ContentError, match="'icon' names no icon"):
        _load_encounter_objects(definitions, "core")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"palette": "neon"}, "'palette' must be one of"),
        ({"ambient": "stormy"}, "'ambient' must be one of"),
        ({"background_color": "white"}, "background_color must be a hex colour"),
        ({"grid_color": "#12"}, "grid_color must be a hex colour"),
    ],
)
def test_template_style_is_validated(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ContentError, match=re.escape(message)):
        _load_templates(_template(**overrides))
