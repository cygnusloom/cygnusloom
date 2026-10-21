"""World templates and rule sets, loaded once from the bundled data directory."""

from __future__ import annotations

import importlib
import inspect
import re
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from cygnusforge.content.character import (
    CampaignSpec,
    CharacterSpec,
    TableOption,
    build_campaign,
    build_character,
    character_schema,
)
from cygnusforge.content.definitions import (
    ContentError,
    Definition,
    read_definitions,
    required_text,
)
from cygnusforge.rules.dice import is_formula

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
PROMPT_ARGUMENT = "prompt"
# A tool flagged `character_options` gets this argument, built from the rule set's character spec.
CHARACTER_ARGUMENT = "character"
# The engine passes its ToolContext to a function that declares this parameter. The model
# never sees it, so it must not appear in a tool's schema.
CONTEXT_ARGUMENT = "ctx"
CORE_RULE_SET = "core"
# Every rule set must define these tables. NPCs get a random entry from each one by default.
REQUIRED_TABLES = ("personality", "accent", "quirk")


@dataclass(frozen=True)
class LocationTemplate:
    id: str
    name: str
    description: str
    # Known to everyone in the world from the start, so the player's map shows it.
    discovered: bool = False
    x: float | None = None
    y: float | None = None
    # The id of the place this one is inside, such as a building in a village. A place inside
    # another has no map position of its own.
    parent: str | None = None


@dataclass(frozen=True)
class NpcTemplate:
    """An NPC from the world definition. Traits left as None are generated when the campaign starts."""

    id: str
    name: str
    location: str
    description: str
    personality: str | None
    accent: str | None
    quirks: tuple[str, ...] | None


@dataclass(frozen=True)
class WorldTemplate:
    id: str
    name: str
    description: str
    locations: tuple[LocationTemplate, ...]
    npcs: tuple[NpcTemplate, ...]


@dataclass(frozen=True)
class PlayerInput:
    """The player supplies a value for a tool before it runs.

    The player rolls `dice` (a formula such as "1d20") or types free-form text. A roll's
    total is passed to the function as keyword argument `param`. `details`, if set, is a
    template filled from the arguments and shown with the prompt, such as the modifiers."""

    dice: str
    param: str
    details: str | None = None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    description: str
    parameters: Mapping[str, Any]
    function: Callable[..., Any]
    player_input: PlayerInput | None = None
    # A str.format template shown to the player after the tool runs. Fields come from the
    # arguments and the result.
    activity: str | None = None
    # True when the function declares CONTEXT_ARGUMENT and the engine must pass it.
    takes_context: bool = False
    # True for tools that only read the world. The GM lists them with list_lookups.
    lookup: bool = False


@dataclass(frozen=True)
class EncounterGrid:
    """The scale of an encounter map: how many feet a cell is, and how far a participant may move
    in one turn."""

    cell_feet: int
    move_feet: int

    @property
    def move_cells(self) -> int:
        return self.move_feet // self.cell_feet


@dataclass(frozen=True)
class RuleSet:
    id: str
    name: str
    description: str
    rules: Mapping[str, str]
    tool_calls: Mapping[str, ToolCall]
    character: CharacterSpec
    # Options shared by every character in a campaign, such as the omen.
    campaign: CampaignSpec
    # Named option lists, such as "personality", used to generate NPC traits.
    tables: Mapping[str, tuple[TableOption, ...]] = field(default_factory=dict)
    # Rules whose text goes into every prompt. The others are read with lookup_rule.
    important: frozenset[str] = frozenset()
    # The scale of encounter maps, or None when the rule set has no grid: its encounters have no
    # movement limit.
    grid: EncounterGrid | None = None


@dataclass(frozen=True)
class ContentLibrary:
    world_templates: Mapping[str, WorldTemplate]
    # Selectable rule sets. The core rule set is not listed here: it is merged into each of these.
    rule_sets: Mapping[str, RuleSet]
    core: RuleSet

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> ContentLibrary:
        """Load every world template and rule set. Each subdirectory is one of either.

        The `core` rule set is loaded on its own first, then merged under every other rule
        set. A definition in a rule set replaces the core definition with the same type and id."""
        worlds = {d.name: _load_world(d) for d in _subdirectories(data_dir / "worlds")}
        rule_set_dirs = _subdirectories(data_dir / "rulesets")
        core_dir = next((d for d in rule_set_dirs if d.name == CORE_RULE_SET), None)
        if core_dir is None:
            raise ContentError(f"missing the {CORE_RULE_SET!r} rule set")
        core_definitions = read_definitions(core_dir)
        core = _build_rule_set(core_definitions, CORE_RULE_SET)
        rule_sets = {
            d.name: _load_rule_set(d, core_definitions)
            for d in rule_set_dirs
            if d.name != CORE_RULE_SET
        }
        return cls(world_templates=worlds, rule_sets=rule_sets, core=core)


def _subdirectories(path: Path) -> list[Path]:
    if not path.is_dir():
        raise ContentError(f"missing content directory {path}")
    return sorted(child for child in path.iterdir() if child.is_dir())


def _single(definitions: list[Definition], kind: str, template: str) -> Definition:
    matches = [d for d in definitions if d.type == kind]
    if len(matches) != 1:
        raise ContentError(
            f"{template}: expected exactly one {kind!r} object, found {len(matches)}"
        )
    return matches[0]


def _check_directory_id(template: str, ident: str) -> None:
    if ident != template:
        raise ContentError(f"{template}: id must match the directory name, got {ident!r}")


def _load_world(directory: Path) -> WorldTemplate:
    template = directory.name
    definitions = read_definitions(directory)
    world = _single(definitions, "world", template)
    _check_directory_id(template, world.id)
    label = f"{template}: world"
    locations = tuple(_load_location(d, template) for d in definitions if d.type == "location")
    npcs = tuple(_load_npc(d, template) for d in definitions if d.type == "npc")
    location_ids = {location.id for location in locations}
    for npc in npcs:
        if npc.location not in location_ids:
            raise ContentError(
                f"{template}: npc {npc.id!r} is in unknown location {npc.location!r}"
            )
    _check_parents(locations, template)
    _check_unique_names([*locations, *npcs], template)
    return WorldTemplate(
        id=world.id,
        name=required_text(world.data, "name", label),
        description=required_text(world.data, "description", label),
        locations=locations,
        npcs=npcs,
    )


def _load_location(definition: Definition, template: str) -> LocationTemplate:
    label = f"{template}: location {definition.id}"
    data = definition.data
    discovered = data.get("discovered", False)
    if not isinstance(discovered, bool):
        raise ContentError(f"{label}: discovered must be true or false")
    x, y = data.get("x"), data.get("y")
    if (x is None) != (y is None):
        raise ContentError(f"{label}: give both x and y, or neither")
    for key, value in (("x", x), ("y", y)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise ContentError(
                f"{label}: {key} must be a number of kilometers from the map's center"
            )
    return LocationTemplate(
        id=definition.id,
        name=required_text(data, "name", label),
        description=required_text(data, "description", label),
        discovered=discovered,
        x=None if x is None else float(x),
        y=None if y is None else float(y),
        parent=_optional_text(data, "parent", label),
    )


def _check_parents(locations: tuple[LocationTemplate, ...], template: str) -> None:
    """A place can be inside another place, but only one level deep, and only a place at the top
    level has a map position."""
    by_id = {location.id: location for location in locations}
    for location in locations:
        if location.parent is None:
            continue
        label = f"{template}: location {location.id}"
        if location.parent == location.id:
            raise ContentError(f"{label}: cannot be inside itself")
        outer = by_id.get(location.parent)
        if outer is None:
            raise ContentError(
                f"{label}: parent {location.parent!r} is not a location in this world"
            )
        if outer.parent is not None:
            raise ContentError(
                f"{label}: parent {location.parent!r} is itself inside {outer.parent!r}"
            )
        if location.x is not None:
            raise ContentError(f"{label}: a place inside another place has no map position")


def _load_npc(definition: Definition, template: str) -> NpcTemplate:
    label = f"{template}: npc {definition.id}"
    data = definition.data
    quirks = data.get("quirks")
    if quirks is not None:
        if not isinstance(quirks, list) or not all(
            isinstance(q, str) and q.strip() for q in quirks
        ):
            raise ContentError(f"{label}: 'quirks' must be a list of non-empty text")
        quirks = tuple(quirks)
    return NpcTemplate(
        id=definition.id,
        name=required_text(data, "name", label),
        location=required_text(data, "location", label),
        description=required_text(data, "description", label),
        personality=_optional_text(data, "personality", label),
        accent=_optional_text(data, "accent", label),
        quirks=quirks,
    )


def _optional_text(data: Mapping[str, Any], key: str, label: str) -> str | None:
    if data.get(key) is None:
        return None
    return required_text(data, key, label)


def _check_unique_names(entries: list[LocationTemplate | NpcTemplate], template: str) -> None:
    """Locations and NPCs share one namespace, so the GM can refer to either by name."""
    seen: set[str] = set()
    for entry in entries:
        key = entry.name.casefold()
        if key in seen:
            raise ContentError(f"{template}: duplicate name {entry.name!r}")
        seen.add(key)


def _load_rule_set(directory: Path, core_definitions: list[Definition]) -> RuleSet:
    """A rule set is its own definitions plus every core definition it does not replace."""
    template = directory.name
    own = read_definitions(directory)
    own_keys = {(d.type, d.id) for d in own}
    inherited = [
        d for d in core_definitions if d.type != "rule_set" and (d.type, d.id) not in own_keys
    ]
    return _build_rule_set([*own, *inherited], template)


def _build_rule_set(definitions: list[Definition], template: str) -> RuleSet:
    head = _single(definitions, "rule_set", template)
    _check_directory_id(template, head.id)
    label = f"{template}: rule_set"
    modules = {d.id: _import_module(d, template) for d in definitions if d.type == "python_module"}
    rules = {
        d.id: required_text(d.data, "text", f"{template}: rule {d.id}")
        for d in definitions
        if d.type == "rule"
    }
    important = frozenset(
        d.id
        for d in definitions
        if d.type == "rule" and _flag(d.data, "important", f"{template}: rule {d.id}")
    )
    tables = {d.id: _load_table(d, template) for d in definitions if d.type == "table"}
    for name in REQUIRED_TABLES:
        if name not in tables:
            raise ContentError(f"{template}: missing table {name!r}")
    table_names = {d.id: _table_label(d, template) for d in definitions if d.type == "table"}
    character = build_character(definitions, tables, table_names, template)
    campaign = build_campaign(definitions, tables, table_names, template)
    grids = [d for d in definitions if d.type == "encounter_grid"]
    if len(grids) > 1:
        raise ContentError(f"{template}: more than one encounter grid")
    grid = _load_grid(grids[0], template) if grids else None
    tool_calls: dict[str, ToolCall] = {}
    tool_names: set[str] = set()
    for d in definitions:
        if d.type != "tool_call":
            continue
        call = _load_tool_call(d, modules, character, template)
        if call.name in tool_names:
            raise ContentError(f"{template}: duplicate tool name {call.name!r}")
        tool_names.add(call.name)
        tool_calls[call.id] = call
    return RuleSet(
        id=head.id,
        name=required_text(head.data, "name", label),
        description=required_text(head.data, "description", label),
        rules=rules,
        important=important,
        tool_calls=tool_calls,
        character=character,
        campaign=campaign,
        tables=tables,
        grid=grid,
    )


def _load_grid(definition: Definition, template: str) -> EncounterGrid:
    label = f"{template}: encounter grid {definition.id}"
    cell_feet = definition.data.get("cell_feet")
    move_feet = definition.data.get("move_feet")
    for key, value in (("cell_feet", cell_feet), ("move_feet", move_feet)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ContentError(f"{label}: {key!r} must be a whole number of feet, at least 1")
    if move_feet < cell_feet:
        raise ContentError(f"{label}: 'move_feet' must be at least one cell")
    return EncounterGrid(cell_feet=cell_feet, move_feet=move_feet)


def _load_table(definition: Definition, template: str) -> tuple[TableOption, ...]:
    label = f"{template}: table {definition.id}"
    options = definition.data.get("options")
    if not isinstance(options, list) or not options:
        raise ContentError(f"{label}: 'options' must be a non-empty list")
    loaded = tuple(_load_option(option, label) for option in options)
    if len({option.value for option in loaded}) != len(loaded):
        raise ContentError(f"{label}: every option must be unique")
    return loaded


def _load_option(option: object, label: str) -> TableOption:
    """An option is text, or an object with a `name` and a `description`."""
    if isinstance(option, str) and option.strip():
        return TableOption(name=None, description=option)
    if (
        isinstance(option, dict)
        and set(option) == {"name", "description"}
        and all(isinstance(option[key], str) and option[key].strip() for key in option)
    ):
        return TableOption(name=option["name"], description=option["description"])
    raise ContentError(
        f"{label}: every option must be non-empty text or an object with "
        "non-empty 'name' and 'description' text"
    )


def _table_label(definition: Definition, template: str) -> str:
    """The name a player or the GM sees for a table. It defaults to the table id."""
    name = definition.data.get("name", definition.id)
    if not isinstance(name, str) or not name.strip():
        raise ContentError(f"{template}: table {definition.id}: 'name' must be non-empty text")
    return name


def _import_module(definition: Definition, template: str) -> ModuleType:
    label = f"{template}: python_module {definition.id}"
    name = required_text(definition.data, "module", label)
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ContentError(f"{label}: cannot import {name!r}: {exc}") from exc


def _load_tool_call(
    definition: Definition,
    modules: Mapping[str, ModuleType],
    character: CharacterSpec,
    template: str,
) -> ToolCall:
    label = f"{template}: tool_call {definition.id}"
    data = definition.data
    module_id = required_text(data, "python_module", label)
    if module_id not in modules:
        raise ContentError(f"{label}: unknown python_module {module_id!r}")
    function_name = required_text(data, "function", label)
    function = getattr(modules[module_id], function_name, None)
    if not callable(function):
        raise ContentError(f"{label}: {function_name!r} is not a function in {module_id!r}")
    name = required_text(data, "name", label)
    if not TOOL_NAME.fullmatch(name):
        raise ContentError(f"{label}: 'name' must be 1 to 64 letters, digits, '_' or '-'")
    parameters = _check_parameters(data.get("parameters"), label)
    if _flag(data, "character_options", label):
        parameters = _with_character(parameters, character, label)
    if CONTEXT_ARGUMENT in parameters["properties"]:
        raise ContentError(f"{label}: {CONTEXT_ARGUMENT!r} is reserved and not a parameter")
    player_input = _check_player_input(data.get("player_input"), parameters, function, label)
    activity = _check_activity(data.get("activity"), label)
    _check_signature(function, parameters, label, consumed=_consumed(player_input))
    return ToolCall(
        id=definition.id,
        name=name,
        description=required_text(data, "description", label),
        parameters=parameters,
        function=function,
        player_input=player_input,
        activity=activity,
        takes_context=CONTEXT_ARGUMENT in inspect.signature(function).parameters,
        lookup=_flag(data, "lookup", label),
    )


def _flag(data: Mapping[str, Any], key: str, label: str) -> bool:
    flag = data.get(key, False)
    if not isinstance(flag, bool):
        raise ContentError(f"{label}: {key!r} must be true or false")
    return flag


def _with_character(
    parameters: dict[str, Any], character: CharacterSpec, label: str
) -> dict[str, Any]:
    """Add the rule set's character options as a `character` argument. It is optional, so the
    GM may create an NPC with no options and the rest are rolled or left blank."""
    if CHARACTER_ARGUMENT in parameters["properties"]:
        raise ContentError(f"{label}: {CHARACTER_ARGUMENT!r} is generated and must not be declared")
    properties = {**parameters["properties"], CHARACTER_ARGUMENT: character_schema(character)}
    return {**parameters, "properties": properties}


def _consumed(player_input: PlayerInput | None) -> frozenset[str]:
    """Schema arguments the engine uses itself rather than passing to the function."""
    return frozenset({PROMPT_ARGUMENT}) if player_input else frozenset()


def _check_player_input(
    data: object, parameters: Mapping[str, Any], function: Callable[..., Any], label: str
) -> PlayerInput | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ContentError(f"{label}: 'player_input' must be an object")
    dice = data.get("dice")
    if not is_formula(dice):
        raise ContentError(f"{label}: 'player_input.dice' must be a formula such as '1d20'")
    param = data.get("param")
    if not isinstance(param, str) or not param:
        raise ContentError(f"{label}: 'player_input.param' must name a function parameter")
    prompt = parameters["properties"].get(PROMPT_ARGUMENT)
    if not isinstance(prompt, dict) or prompt.get("type") != "string":
        raise ContentError(
            f"{label}: a tool with 'player_input' needs a string '{PROMPT_ARGUMENT}' parameter"
        )
    if PROMPT_ARGUMENT not in parameters.get("required", []):
        raise ContentError(f"{label}: '{PROMPT_ARGUMENT}' must be required")
    if param in parameters["properties"]:
        raise ContentError(f"{label}: 'player_input.param' must not be a schema parameter")
    accepted = inspect.signature(function).parameters
    if param not in accepted:
        raise ContentError(f"{label}: function does not accept parameter {param!r}")
    details = _check_template(data.get("details"), "player_input.details", label)
    if details is not None:
        _check_fields(details, parameters["properties"], "player_input.details", label)
    return PlayerInput(dice=dice, param=param, details=details)


def _check_activity(template: object, label: str) -> str | None:
    return _check_template(template, "activity", label)


def _check_template(template: object, key: str, label: str) -> str | None:
    if template is None:
        return None
    if not isinstance(template, str) or not template.strip():
        raise ContentError(f"{label}: '{key}' must be a non-empty text template")
    try:
        list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ContentError(f"{label}: '{key}' is not a valid template: {exc}") from exc
    return template


def _check_fields(template: str, arguments: Mapping[str, Any], key: str, label: str) -> None:
    """A details template may only name schema arguments, so a typo fails at load."""
    for _, name, _, _ in string.Formatter().parse(template):
        if name and name not in arguments:
            raise ContentError(f"{label}: '{key}' names unknown argument {name!r}")


def _check_parameters(schema: object, label: str) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ContentError(f"{label}: 'parameters' must be an object schema with type 'object'")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ContentError(f"{label}: 'parameters' needs a 'properties' object")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(item, str) and item in properties for item in required
    ):
        raise ContentError(f"{label}: 'required' must list names from 'properties'")
    return schema


def _check_signature(
    function: Callable[..., Any],
    parameters: Mapping[str, Any],
    label: str,
    consumed: frozenset[str] = frozenset(),
) -> None:
    """The GM calls the function with keyword arguments taken from the schema, so both must agree."""
    accepted = inspect.signature(function).parameters
    takes_any_keyword = any(p.kind is p.VAR_KEYWORD for p in accepted.values())
    if not takes_any_keyword:
        for name in parameters["properties"]:
            if name not in accepted and name not in consumed:
                raise ContentError(f"{label}: function does not accept parameter {name!r}")
    required = set(parameters.get("required", []))
    for name, param in accepted.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD) or name == CONTEXT_ARGUMENT:
            continue
        if param.default is param.empty and name not in required:
            raise ContentError(
                f"{label}: parameter {name!r} has no default, so it must be required"
            )
