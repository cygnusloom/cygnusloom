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
    "background": ["backpack", {"feature": "gold", "quantity": "3d6"}, "rations"]
  }
}
```

A feature grant is either a feature id, which grants one, or an object naming a
feature and a different `quantity`. A template of that kind receives the
defaults first, then its own grants. If the template grants the same feature id,
the template's grant wins and the default is not duplicated.

This is the current pattern for the Cairn background kit. It removes repeated
backpack/gold/rations grants from every background.

## Starting years

A rule set with a year-by-year training path may describe it as one table on the
rule-set head instead of one template per year. Academia Arcana uses this:

```json
{
  "type": "rule_set",
  "id": "academia-arcana",
  "name": "Academia Arcana",
  "description": "...",
  "starting_years": {
    "subjects": ["Charms", "Transfiguration", "Warding"],
    "forbidden_spells": ["command-the-will", "open-the-deep"],
    "years": [
      {
        "description": "Your first year at the academy.",
        "subjects": 1,
        "spells": 2,
        "potions": 1,
        "enchantments": 1,
        "training": 0
      }
    ]
  }
}
```

Each `years` row generates a `year-<n>` template of kind `starting_year`, named
`Year <n>`, with `counters.year` set to `n - 1` and the pick shape:

- `subjects` picks any of the shared `subjects` tags;
- `spells` picks spells tagged with a subject, excluding `forbidden_spells`;
- `potions` and `enchantments` pick from the `Potions` and `Enchantments` tags;
- `training` picks a trait from `training-1-3` (years 1–3), `training-4-6`
  (years 4–6), or `training-7` (year 7).

Counts are non-negative integers. A year with spells needs subjects, because the
spell pick draws its tags from the subject list. An explicit `year-<n>` template
in the same rule set wins over the generated row.

## Marketplace items

A `shop_item` may name a `feature` and inherit its name, size, armor, damage,
uses, description, icon, and encumbrance checks. The shop item's own fields win
when present. Category, price, and armor mode always come from the shop item.

```json
{
  "type": "shop_item",
  "id": "brigandine",
  "feature": "brigandine",
  "category": "armor",
  "price": 40,
  "armor_mode": "worn"
}
```

Use this for any item the character sheet already models as a feature. The shop
then owns pricing and placement instead of restating the whole item.

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

## Stats

A stat's `modifier` is a list of bands with rising minimums, written as compact
`[min, modifier]` pairs or as objects with `min` and `modifier`. A score takes
the modifier of the highest minimum it reaches:

```json
{
  "type": "stat",
  "id": "str",
  "name": "Strength",
  "formula": "3d6",
  "swap_group": "attributes",
  "modifier": [[3, -2], [4, -1], [8, 0], [14, 1], [18, 2]]
}
```

The compact pair form is the bundled convention. Stats that share fields may
name a preset from the rule-set head; the stat's own fields win:

```json
{
  "stat_presets": {
    "attributes": {
      "formula": "3d6",
      "swap_group": "attributes",
      "modifier": [[3, -2], [4, -1], [8, 0], [14, 1], [18, 2]]
    }
  }
}
```

```json
{"type": "stat", "id": "str", "name": "Strength", "preset": "attributes"}
```

A preset cannot set `type`, `id`, `name`, or `preset`.

## Dice tables

A `dice_table` has one formula and rows that cover every roll it can show, in
order and without gaps. A row is either an object with `roll`, `result`, and an
optional `text` note, or the compact array `[roll, result]` /
`[roll, result, text]`:

```json
{
  "type": "dice_table",
  "id": "weather",
  "name": "Weather",
  "dice": "1d6",
  "rows": [
    ["1", "Clear and cold."],
    ["2-3", "A thin grey rain."],
    ["4", "Wind tears at cloaks.", "Loose gear may be lost."],
    ["5-6", "A storm closes the road."]
  ]
}
```

The compact form is the bundled convention. A `creative_table` is simpler
still: `entries` is one list of strings, rolled as `1dN`.

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
A route is written once, between two locations:

```json
{
  "type": "route",
  "id": "twin-lakes-to-old-ford",
  "from": "twin-lakes",
  "to": "old-ford",
  "path": "north along the shore",
  "distance": 2,
  "terrain": "hills"
}
```

Routes are two-way by default. Set `one_way: true` for a trip that only works
from `from` to `to`. The loader adds both directions to the locations' route
maps, so do not also author a `routes` entry for the same pair.
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
