# Authoring Rulesets and Worlds

This is a practical guide to the content format as it exists on the 4.0 branch. It
documents the supported authoring surface, not a future design. For the audit and
remaining simplification ideas see [RULESET_REFACTORING_REPORT.md](RULESET_REFACTORING_REPORT.md).

## Directory layout

Content lives under `src/cygnusloom/data`:

```text
data/
  defaults/              shared data applied to every rule set
  rulesets/
    core/                the base every rule set inherits
    <rule-set-id>/       one directory per rule set
  worlds/
    <world-id>/          one directory per world
```

Every `*.json` file under a rule-set or world directory is discovered
recursively and read in sorted path order. Each file is a JSON list of
definitions. A definition needs `type` and `id`; the pair `(type, id)` must be
unique within that directory. File names are conventions, not a schema.

The directory name is the rule-set or world id. The matching `rule_set` or
`world` object must use that same id.

## Minimal rule set

Every rule set needs exactly one `rule_set` definition with a `name` and
`description`. Core supplies the three NPC tables, common character fields,
base rules and prompts, tables, and the universal tool catalog.

```json
[
  {
    "type": "rule_set",
    "id": "my-game",
    "name": "My Game",
    "description": "A short description for the campaign picker."
  }
]
```

That tree loads, but it does not yet describe a playable character. Add the
stats, skills, features, templates, combat, inventory, magic, and other systems
your game uses.

## Core inheritance

A rule set inherits every core definition with the same `type` and `id`. A
definition in the rule set replaces the core definition with that key. You can
also shape an inherited definition:

```json
{
  "type": "tool_call",
  "id": "rest",
  "extends": "core:tool_call/rest",
  "description": "Record one party rest by the rules of this game."
}
```

- `extends` overlays your fields on the target. Nested objects merge key by key;
  lists and scalar values replace.
- `patch` changes an existing nested value. It fails at load time if the path is
  not already present, so typos do not pass silently.
- `disabled: true` or `remove: true` drops an inherited definition with the same
  type and id. The stub may carry only `type` and `id`.
- `supersedes` on a prompt drops the named core prompt fragment.
- `requires` and `enabled_by` gate prompts and tools against the rule set's
  capabilities.

A rule set can extend its own definitions as well as core definitions. Cycles
are rejected.

## Defaults layer

`data/defaults` holds shared data that every rule set may use. It is optional;
a missing or empty defaults directory is a no-op.

Currently supported defaults:

| Type | Behavior |
|---|---|
| `clock` | Fills missing fields of the rule set's clock. |
| `rest` | Adds missing rest lengths and fills `interrupted` when absent. |
| `world_terrain` | Fills missing `name`, `color`, `pattern`, and `ink` when the rule set defines the same terrain id. |
| `prompt` | Adds the fragment when the rule set does not define that id. |
| `tool_call` | Adds the tool when the rule set does not define that id. |

Defaults never add a clock, rest, or terrain that the rule set does not already
define. They only enrich an existing definition. Prompt and tool defaults add a
definition when it is absent.

Shared defaults currently cover the common clock skeleton, the `moments` rest
length, shared world-terrain visuals, and the base prompt/tool data.

## Shared template features

A rule set head may declare default feature grants per template kind:

```json
{
  "type": "rule_set",
  "id": "cairn2e",
  "name": "Cairn 2e",
  "description": "...",
  "template_features": {
    "background": [
      {"feature": "backpack", "quantity": "1"},
      {"feature": "gold", "quantity": "3d6"},
      {"feature": "rations", "quantity": "1"}
    ]
  }
}
```

A template of that kind receives the defaults first, then its own grants. If the
template grants the same feature id, the template's grant wins and the default is
not duplicated.

This is the current pattern for the Cairn background kit. It removes repeated
backpack/gold/rations grants from every background.

## Capabilities

The `capabilities` block on the rule-set head declares what optional systems the
game offers:

```json
"capabilities": {
  "magic": "item",
  "travel": "watches",
  "wilderness": true,
  "dungeon": true,
  "combat": true,
  "social": ["conditions"]
}
```

A declared block is authoritative: omitted booleans are false, and an omitted
`social` list offers no social systems. A rule set with no capabilities block is
treated permissively for inherited core definitions.

Core tools and prompts carry `requires` or `enabled_by` gates. A rule set only
receives a gated definition when its own capabilities satisfy the gate.

## Tool definitions

Most tools should be inherited from core. A rule set normally adds only tools
that express its own mechanics.

Tool JSON has useful defaults:

- `name` defaults to the tool id;
- `world_method` defaults to the tool id when no Python handler is named;
- `parameters` may be omitted for a tool with no arguments;
- `required` may be omitted;
- the root parameter schema's `type` defaults to `object`.

A tool names exactly one handler: a `world_method` on `WorldTools`, or a
`python_module` plus `function`. `bind` supplies fixed or constructed arguments;
`rename` maps schema names to method names. `enum_from` generates choices from
the rule set's own data.

Python-backed tools are trusted code. The loader checks the signature against
the schema, but content can import a Python module.

## World layout

A world needs exactly one `world` definition with `name` and `description`.
Optional fields include `rule_set`, `blurb`, `third_party`, and `start`.

A world may define:

- `route`, with `from` and `to` location ids, `path`, `distance`, `terrain`,
  optional `vast`, and optional `one_way`;
- `location`, including `parent`, `map`, `icon`, `looks_like`, and `discovered`;
- `npc`, including an inline statblock or a `monster` reference;
- `region`, with polygon points and a rule-set `world_terrain` id;
- `monster`, `feature`, `rule`, `table`, `dice_table`, and `creative_table`.

World files are discovered recursively. There is no world manifest and worlds
do not currently support `extends`, `patch`, or `disabled`; keep repeated world
data explicit until that layer exists.

## Common pitfalls

- A `rule_set` head is not inherited. Every rule set needs its own identity and
  capability block.
- Lists replace wholesale. If you need to change one item in an inherited list,
  restate the whole list.
- `patch` can only change paths that already exist.
- A `world.rule_set` is optional, but a world with rule-set-specific monsters,
  features, or terrain may fail when paired with another rule set.
- Author world routes once, as top-level `route` definitions. A two-way route
  is the default; set `one_way: true` to add only the `from` -> `to` direction.
  Do not also put a per-location `routes` entry on either endpoint for the same
  pair, or loading fails.
- A `location.routes` map remains supported for older world data. It is keyed
  by destination location id.
- Capability gates apply only to prompts and tools. Other definition types
  ignore `requires`/`enabled_by`.
- Empty `enum_from` sources produce no enum; check the source data exists.

## Validation

There is currently no standalone content validator command. The test suite
loads and validates the bundled content, including every bundled world/rule-set
pair used by tests. For a new tree, run the loader through a small Python
script or the application test helpers until a `content validate` command is
added.
