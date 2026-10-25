"""Encounter map content (terrain, objects, starter templates) loaded from the core rule set."""

import re
from typing import Any

import pytest

from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import (
    ContentLibrary,
    EncounterCell,
    EncounterTemplate,
    _load_encounter_objects,
    _load_encounter_templates,
    _load_encounter_terrain,
)
from cygnusforge.web.icons import _registry

TERRAIN_IDS = {"floor", "grass", "dirt", "shore", "mud", "rubble", "shallow-water"}
DIFFICULT_TERRAIN = {"mud", "rubble", "shallow-water"}
OBJECT_IDS = {
    "wall",
    "tree",
    "boulder",
    "pillar",
    "well",
    "deep-water",
    "table",
    "door",
    "campfire",
    "crate",
}
BLOCKING_OBJECTS = {"wall", "tree", "boulder", "pillar", "well", "deep-water", "crate"}
TEMPLATE_SIZES = {"room": (10, 8), "corridor": (16, 5), "clearing": (14, 12), "shoreline": (14, 10)}


@pytest.fixture(scope="module")
def library() -> ContentLibrary:
    return ContentLibrary.load()


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


# Real content -----------------------------------------------------------------


def test_core_defines_terrain_objects_and_templates(library: ContentLibrary) -> None:
    core = library.core
    assert TERRAIN_IDS <= set(core.terrain)
    assert OBJECT_IDS <= set(core.encounter_objects)
    assert set(TEMPLATE_SIZES) <= set(core.encounter_templates)


def test_terrain_and_object_flags_match_spec(library: ContentLibrary) -> None:
    core = library.core
    assert {i for i, t in core.terrain.items() if t.difficult} == DIFFICULT_TERRAIN
    assert {i for i, o in core.encounter_objects.items() if o.blocks} == BLOCKING_OBJECTS


def test_every_object_icon_is_registered(library: ContentLibrary) -> None:
    registry = _registry()
    for item in library.core.encounter_objects.values():
        assert item.icon in registry, f"{item.id} uses unknown icon {item.icon!r}"


def test_every_terrain_says_its_rule_effect(library: ContentLibrary) -> None:
    assert all(terrain.rule for terrain in library.core.terrain.values())
    assert library.core.terrain["mud"].rule == "Difficult: each cell entered costs two moves."


def test_every_terrain_and_object_has_a_hex_colour(library: ContentLibrary) -> None:
    for item in [*library.core.terrain.values(), *library.core.encounter_objects.values()]:
        assert re.fullmatch(r"#[0-9a-f]{6}", item.color), f"{item.id} colour {item.color!r}"


def test_only_walls_are_solid_blocks(library: ContentLibrary) -> None:
    solid = {i for i, o in library.core.encounter_objects.items() if o.solid}
    assert solid == {"wall"}


def test_every_template_covers_its_grid_with_resolvable_ids(library: ContentLibrary) -> None:
    core = library.core
    for ident, (columns, rows) in TEMPLATE_SIZES.items():
        template = core.encounter_templates[ident]
        assert (template.columns, template.rows) == (columns, rows)
        # Every template has a background terrain, so every grid cell is painted.
        assert len(template.cells) == columns * rows, ident
        positions = [(cell.column, cell.row) for cell in template.cells]
        assert positions == sorted(positions, key=lambda p: (p[1], p[0])), ident
        for cell in template.cells:
            assert cell.terrain in core.terrain, (ident, cell)
            assert cell.object == "" or cell.object in core.encounter_objects, (ident, cell)


def test_room_layout(library: ContentLibrary) -> None:
    cells = _cells_by_position(library.core.encounter_templates["room"])
    assert cells[(4, 0)].object == "door"
    assert cells[(0, 0)].object == "wall"
    assert cells[(9, 7)].object == "wall"
    assert cells[(5, 4)].object == "table"
    interior = [cell for (x, y), cell in cells.items() if 0 < x < 9 and 0 < y < 7]
    assert all(cell.object in ("", "table") for cell in interior)


def test_corridor_pillar_near_middle(library: ContentLibrary) -> None:
    cells = _cells_by_position(library.core.encounter_templates["corridor"])
    assert cells[(8, 2)].object == "pillar"
    assert cells[(0, 0)].object == "wall" and cells[(15, 4)].object == "wall"


def test_all_rule_sets_see_the_same_encounter_content(library: ContentLibrary) -> None:
    assert "cairn2e" in library.rule_sets
    for name, rule_set in library.rule_sets.items():
        assert rule_set.terrain == library.core.terrain, name
        assert rule_set.encounter_objects == library.core.encounter_objects, name
        assert rule_set.encounter_templates == library.core.encounter_templates, name


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
