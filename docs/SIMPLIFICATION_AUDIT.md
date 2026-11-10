# Simplification audit

**Branch:** `4.0-ruleset-refactor`  
**Date:** 2026-11-10

This is a follow-up to [RULESET_REFACTORING_REPORT.md](RULESET_REFACTORING_REPORT.md).
It records what was still duplicated or hardcoded after the 4.0 data cleanup, what
this pass changed, and the next highest-value simplifications. The goal is an
engine and content format that a new ruleset or world can extend without adding
Python branches or restating the same fields.

## Principles used

- Prefer a generic authoring mechanism over bundled-data compression.
- Keep every persisted id, event payload, and player-visible value stable.
- Put ruleset-specific behavior behind data, registries, or the rule set's own
  Python module.
- Make mistakes fail at load time, not at the table.
- Add shorthands only when they have one obvious merge rule.

## Implemented in this pass

### File-scoped defaults

`read_definitions` now accepts `defaults` entries in any JSON file:

```json
{"type": "defaults", "id": "foci",
 "when": {"type": "feature"},
 "set": {"feature_type": "feat", "tag": "focus"}}
```

- `when` matches top-level values; `null` means the field must be absent.
- A nested object in `set` fills missing keys inside a definition object.
- The definition always wins; earlier matching defaults win over later ones.
- `set` cannot change `type` or `id`.
- A defaults entry is not a content definition and never reaches a loader.

The bundled content uses this for constant feature types, WWN monster stat
defaults, standalone shop-item defaults, and the Twin Lakes NPC HP tier. This
removed roughly 1,000 JSON lines without changing ids or behavior.

### Feature types are a real registry

`feature_type` definitions now drive behavior, not only display text:

- feature loading checks the rule set's registry, not a global tuple;
- template `choose` and `feat_picks` validate against the same registry;
- a type's `unique` flag supplies the default for feature picks;
- `enum_from` feature-type filters use the registry;
- level-up feature picks use the registry;
- world features are checked against the rule set's stats, skills, counters, and
  feature types;
- custom-feature creation accepts every type the rule set defines;
- starting inventory uses the type's `inventory` flag;
- compendium grouping uses the type's `group`/`plural` and optional
  `tag_groups` map, so WWN arts and foci share the feat type without sharing a
  heading;
- a `feature_type` definition overlays the engine default with the same id, so
  changing one label or adding one tag group does not restate the whole type.

A rule set can now add a new type and use it in features, picks, and tools
without editing `content/character.py` or the web layer.

### Content validation

- `ContentLibrary.load()` validates every world against its declared rule set.
  A bad declared pair is now a load error instead of a runtime surprise.
- `ContentLibrary.validate(all_pairs=False)` collects declared-pair errors and,
  with `all_pairs=True`, reports undeclared pair mismatches as warnings.
- `cygnusloom content validate` exposes the same check, with `--data-dir`,
  `--all-pairs`, and `--strict`.
- The custom-feature tool's `feature_type` enum is now generated from the rule
  set instead of a hardcoded list.

### Small engine cleanups

- `level_up._new_level` uses the configured level counter instead of `"level"`.
- The unused `StatRestored` compatibility alias was removed; callers use
  `Recovered`.

## Remaining high-value opportunities

Ordered by leverage. None of these is required for the current bundled content
to work.

| Opportunity | Why it matters | Risk |
|---|---|---|
| One effect-op registry | Feature effects and move/condition effects still have two validators and two payload-key tables. One registry with context flags would let traits apply conditions and moves add armor without duplicating checks. | Medium |
| World inheritance/prototypes | Worlds still cannot use `extends`/`patch`. File defaults cover constant fields; a world prototype layer would cover variant worlds and shared regions/NPCs. | Medium |
| WWN class/attack profiles | `progression.json` repeats `hit_die`, `focus_levels`, and four attack arrays across 14 classes. Named profiles would remove the repetition and make a new class a short record. | Low |
| WWN focus pick lists | Warrior and expert exclusion lists are repeated across classes and templates. A named `pick_lists` reference would make one source of truth. | Low |
| WWN background generator | `learning` already generates background tables, but `growth_<id>` tables and the common `background_learning` pick are still written per background. | Medium |
| Definition registry | The loader still keeps several hand-maintained type sets and key allowlists. A single type -> loader/key/dataclass registry would make new definition types safer. | Medium |
| Capability cross-check | A declared capability is not yet compared with the definitions present, so `magic: "prepared"` with no magic block loads and fails later. | Low |
| Custom-feature extras | `subject`, `base`, `mods`, and `stress` are still Academia-shaped fields in the core custom-feature event and tool. A generic extras map or a ruleset-declared field block would remove the last large ruleset vocabulary from core. | High |
| Compendium label metadata | Feature-type `rank_label`/`tag_label` are loaded but not yet used in entry text; `TABS`/`GROUP_ORDER` remain static. | Low |
| Monster attack parser | Content statblocks use a map; runtime NPC editing uses a list. Both validate dice, flags, and Shock separately. | Low |
| Character/item lookup helpers | `state.characters.get(name_key(...))` and same-name item lookup are repeated across services. `state.require_character` and `Character.require_item` would remove boilerplate. | Low |
| Magic payload validation | A `magic` definition can carry blocks that the selected `model` ignores. Rejecting mismatched blocks would turn silent config errors into load errors. | Low |
| Test snapshots | Many tests pin exact content counts and property sets. One content snapshot per rule set would make additive authoring safe without touching unrelated tests. | Low |

## Traps

- Do not derive JSON allowlists purely from dataclasses. `learning`, `generate`,
  `preset`, and shop-item `feature` are loader-only or aliased keys.
- Do not route creation-time feature effects through runtime effect executors
  without a creation context; skill replacement, armor accumulation, and caps
  differ from runtime recovery.
- Keep `feature_type == "item"` and `FeatureTypeRules.inventory` distinct when
  handling magic: spells can be carried as magic items without being items.
- Do not make all world x rule-set pairs hard failures. Worlds with no declared
  rule set are intentionally portable; report those mismatches as warnings.
- File defaults are a shorthand, not inheritance. If a value must change per
  definition, write it on the definition or use `extends`/`patch` in a rule set.
