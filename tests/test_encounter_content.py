"""Encounter map content (terrain, objects, starter templates) loaded from the core rule set."""

import re
from typing import Any

import pytest

from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.content.library import (
    MAP_AMBIENTS,
    MAP_PALETTES,
    TERRAIN_PATTERNS,
    ContentLibrary,
    EncounterCell,
    EncounterTemplate,
    _load_encounter_objects,
    _load_encounter_templates,
    _load_encounter_terrain,
)
from cygnusloom.web.icons import _registry
from tests.conftest import bundled_library

TERRAIN_IDS = {
    "floor",
    "grass",
    "dirt",
    "shore",
    "mud",
    "rubble",
    "shallow-water",
    "marble",
    "wood",
    "carpet",
    "sand",
    "snow",
    "ice",
    "lava",
    "moss",
    "gravel",
    "cobblestone",
    "void",
    "meadow",
}
DIFFICULT_TERRAIN = {"mud", "rubble", "shallow-water", "lava", "void"}
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
    "barrel",
    "bookshelf",
    "bench",
    "statue",
    "altar",
    "bed",
    "stairs-up",
    "stairs-down",
    "gate",
    "portcullis",
    "bones",
    "boat",
    "cart",
    "ladder",
    "fountain",
    "throne",
    "tent",
    "anvil",
    "cauldron",
    "obelisk",
    "window",
    "rug",
    "curtain",
    "candle",
    "lantern",
    "brazier",
    "chandelier",
    "plant",
    "flower",
    "mushroom",
    "bush",
    "pine",
    "rock-small",
    "sack",
    "pot",
    "jar",
    "desk",
    "chair",
    "stool",
    "cabinet",
    "shelf",
    "fireplace",
    "oven",
    "counter",
    "keg",
    "banner",
    "tapestry",
    "globe",
    "telescope",
    "alchemy-table",
    "potion-shelf",
    "crystal",
    "rune-circle",
    "portal",
    "trapdoor",
    "pit",
    "bridge",
    "fence",
    "hay",
    "wagon",
    "bedroll",
    "hammock",
    "armor-stand",
    "weapon-rack",
    "shrine",
    "idol",
    "mirror",
    "painting",
    "clock",
    "cauldron-small",
}
BLOCKING_OBJECTS = {
    "wall",
    "tree",
    "boulder",
    "pillar",
    "well",
    "deep-water",
    "crate",
    "barrel",
    "bookshelf",
    "statue",
    "altar",
    "gate",
    "portcullis",
    "cart",
    "fountain",
    "throne",
    "anvil",
    "cauldron",
    "obelisk",
    "alchemy-table",
    "potion-shelf",
    "pine",
    "desk",
    "cabinet",
    "shelf",
    "fireplace",
    "oven",
    "counter",
    "keg",
    "pit",
    "fence",
    "hay",
    "wagon",
    "shrine",
}
TEMPLATE_SIZES = {
    "room": (10, 8),
    "corridor": (16, 5),
    "clearing": (14, 12),
    "shoreline": (14, 10),
    "tavern": (14, 10),
    "house": (12, 9),
    "shop": (12, 9),
    "temple": (14, 12),
    "crypt": (12, 10),
    "cave": (14, 11),
    "watchtower": (10, 10),
    "market": (16, 12),
    "camp": (12, 10),
    "ship": (16, 7),
    "dungeon_hall": (14, 10),
    "castle_hall": (16, 12),
    "library": (14, 10),
    "cellar": (12, 8),
    "great_hall": (20, 14),
    "dormitory": (16, 10),
    "alchemy_lab": (12, 10),
    "courtyard": (18, 14),
    "market_stall": (10, 8),
    "crypt_annex": (14, 12),
}


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


def test_terrain_patterns_are_valid_and_inked_when_present(library: ContentLibrary) -> None:
    assert TERRAIN_PATTERNS[:2] == ("waves", "trees")
    assert {"bricks", "tiles", "wood", "carpet", "snow", "lava", "hexes", "runes"} <= set(
        TERRAIN_PATTERNS
    )
    patterned = [terrain for terrain in library.core.terrain.values() if terrain.pattern]
    assert patterned
    for terrain in patterned:
        assert terrain.pattern in TERRAIN_PATTERNS, terrain
        assert re.fullmatch(r"#[0-9a-f]{6}", terrain.ink or ""), terrain
    assert library.core.terrain["marble"].pattern == "tiles"
    assert library.core.terrain["lava"].difficult


def test_palettes_and_ambients_are_unique_and_known(library: ContentLibrary) -> None:
    assert len(set(MAP_PALETTES)) == len(MAP_PALETTES)
    assert len(set(MAP_AMBIENTS)) == len(MAP_AMBIENTS)
    for template in library.core.encounter_templates.values():
        assert template.palette in {"", *MAP_PALETTES}
        assert template.ambient in {"", *MAP_AMBIENTS}


def test_new_templates_carry_their_look_and_content(library: ContentLibrary) -> None:
    core = library.core
    great_hall = core.encounter_templates["great_hall"]
    assert (great_hall.palette, great_hall.ambient) == ("royal", "torchlit")
    assert great_hall.background == "marble"
    assert {cell.object for cell in great_hall.cells} >= {"throne", "brazier", "banner", "rug"}
    lab = core.encounter_templates["alchemy_lab"]
    assert (lab.palette, lab.ambient) == ("shadow", "magical")
    assert {cell.object for cell in lab.cells} >= {"alchemy-table", "potion-shelf", "crystal"}
    courtyard = core.encounter_templates["courtyard"]
    assert (courtyard.palette, courtyard.ambient) == ("stone", "day")
    assert {cell.object for cell in courtyard.cells} >= {"fountain", "bench", "plant"}
    market = core.encounter_templates["market_stall"]
    assert (market.palette, market.ambient) == ("parchment", "day")
    assert {cell.object for cell in market.cells} >= {"counter", "crate", "sack"}
    crypt = core.encounter_templates["crypt_annex"]
    assert (crypt.palette, crypt.ambient) == ("shadow", "foggy")
    assert {cell.object for cell in crypt.cells} >= {"idol", "shrine", "brazier"}
    dormitory = core.encounter_templates["dormitory"]
    assert (dormitory.palette, dormitory.ambient) == ("forest", "night")
    assert {cell.object for cell in dormitory.cells} >= {"bed", "cabinet", "shelf"}


def test_object_categories_are_grouped(library: ContentLibrary) -> None:
    categories = {item.category for item in library.core.encounter_objects.values()}
    assert {"structure", "furniture", "container", "nature", "decor", "hazard", "magic"} <= (
        categories
    )
    assert library.core.encounter_objects["pit"].category == "hazard"
    assert library.core.encounter_objects["alchemy-table"].category == "furniture"


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


def test_every_object_and_template_has_a_player_description() -> None:
    core = bundled_library().core
    assert all(obj.description.strip() for obj in core.encounter_objects.values())
    assert all(template.description.strip() for template in core.encounter_templates.values())


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
