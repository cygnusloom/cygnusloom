# Ruleset Refactoring Audit

**Version:** Cygnus Loom 4.0.0  
**Date:** 2026-11-08  
**Scope:** `core`, `cairn2e`, `worldswithoutnumber`, `academia-arcana`, the content loader, the rules/engine services, and the test contracts that constrain them.  
**Status:** Initial audit was read-only. The refactor is being implemented on the 4.0 branch.

**4.0 compatibility policy (decided):**
- Databases continue to migrate; existing database files must still open.
- Campaigns are not backward compatible. Each campaign declares its major version.
- A campaign from a different major version remains **exportable** but is **not playable** in 4.0.
- Legacy content ids, event decoders, import aliases, and compatibility shims are removed rather than preserved.
- Where the published source and current behavior differ, the published Cairn 2e / Worlds Without Number source wins. Academia Arcana has no separate published source; its current design is canonical unless a written reference is supplied.

**Implementation status on `4.0-ruleset-refactor`:**
- Done: campaign major-version boundary and migration/export/import handling; capability manifest and tool/prompt gates; defaults layer and `extends`/`patch`/`remove`/`disabled`; definition-type and feature-type registries; magic `item`/`prepared`/`move` facade; common `SpellCast` for move magic; party-level rest with `after_rest`; data-driven clock week and WWN journey; single XP source, `LevelGrants`/`project_level`, progression-data costs, data-driven Die Hard and caster classification; year-banded Academia caps; shared learnable-feature filter; faction cap in data; condition-trigger validation; dead combat-helper removal; version 4.0.0.
- Done in the final 4.0 consolidation pass: thin WWN tool wrappers removed (`rest`, `player_attack`, `player_roll_save`, strain tools, starting silver); universal game-loop tools owned by core (`advance_time`, `rest`, `rest_history`, `roll_tables`, market, inventory, hirelings, `search_monster`); shared gated combat/encounter and project tools owned by core behind new `combat` and `social.projects` capabilities; attack, `player_attack`, `roll_save`, and `travel` expressed as one core definition plus small ruleset extensions; Cairn inherits the core map-cell tool; one `WorldTools.travel` implementation for terrain-day journeys with JSON `journey.events`; exact tool catalogs and lookup lists replaced with derived contracts in `tests/test_tool_contract.py`, `test_tools.py`, `test_content.py`, `test_wwn_magic.py`, and `test_world.py`.
- Still open after this pass: prompt data deduplication, item/shop definition unification and inventory `slot` metadata, one effect-op registry, multiple advancement/level-up selectors, compendium label data, and world-feature validation. These are tracked by the report's remaining phases, not claimed as complete.

**Shared-tool pattern.** A tool whose semantics are shared belongs in core JSON. A rule set keeps only the differences:

```json
{
  "type": "tool_call",
  "id": "rest",
  "extends": "core:tool_call/rest",
  "description": "Record one party rest. A night restores hit points equal to level..."
}
```

- `extends` overlays the rule set's fields on the core definition; nested objects merge key by key while lists and scalars replace.
- `patch` changes an existing nested value and fails at load time if the path is a typo.
- `disabled` or `remove` drops an inherited definition for one rule set.
- `bind`, `rename`, `enum_from`, `requires`, and `enabled_by` keep the shared schema without copying a Python wrapper.
- Core may author gated tools; each inheriting rule set evaluates the gate against its own capability manifest.

**Verification:** the full suite passes at **3099 tests**. Live DeepSeek sessions covered a WWN opening, one party rest (one `RestTaken`, one `ClockAdvanced`), a one-day journey, a Cairn opening and turn, and an Academia opening plus a move-based spell cast (one `MoveMade`, one `SpellCast` with `model: "move"`).

---

## Contents

1. [Executive summary](#1-executive-summary)
2. [Method and scope](#2-method-and-scope)
3. [Current architecture](#3-current-architecture)
4. [Feature matrix](#4-feature-matrix)
5. [Findings](#5-findings)
6. [Target architecture](#6-target-architecture)
7. [Prioritized roadmap](#7-prioritized-roadmap)
8. [Backward-compatibility guardrails](#8-backward-compatibility-guardrails)
9. [Quick wins](#9-quick-wins)
10. [Decisions needed before implementation](#10-decisions-needed-before-implementation)
11. [Appendices](#11-appendices)
12. [Bottom line](#12-bottom-line)

---

## 1. Executive summary

The engine is **substantially more generic than the ruleset JSON suggests**. There are no Python branches on `cairn2e` / `worldswithoutnumber` / `academia-arcana` ids, and conditions, counters, checks, moves, tallies, relations, periods, projects, factions, clocks, and rests already have ruleset-agnostic services. The main problems are concentrated in five areas:

1. **Duplicated content data**, especially tool definitions, prompts, rest/clock structures, terrain visuals, WWN class/progression data, and Academia year packages.
2. **Three incompatible magic models** (Cairn item magic, WWN prepared spells/arts, Academia move-based casting) behind a `MagicRules` block that only models Cairn.
3. **Ruleset-specific concepts embedded in core code/schema** (Shock, custom-feature Academia fields, faction “cup” vocabulary, five-faction cap, seven-day week, Cairn-style rest recovery, Cairn-style overland travel).
4. **Missing generic layering**: no `extends`/patch/remove, no capability manifest, no definition-type registry, no shared effect-op registry, and no way to opt out of inherited core tools/prompts.
5. **A handful of real correctness/consistency bugs found during the audit**, notably WWN rest advancing the clock once per character, WWN class/XP data duplicated and already drifting, and an Academia Year cap mismatch.

The best path is **not a rewrite**. It is an incremental sequence:

- freeze behavior with contract tests and golden content ids;
- fix the correctness issues;
- add a capability/defaults layer and a type registry;
- deduplicate tools/prompts/terrain/clock/rest;
- introduce magic adapters, a journey model, and a single progression grants model;
- only then rename or move content ids, with aliases and migrations.

**Estimated removable duplication:** ~1,300–1,800 JSON lines of tool definitions alone, plus several hundred lines of terrain, class, rest, prompt, and Academia year data.

---

## 2. Method and scope

The audit covered:

- All JSON under `src/cygnusloom/data/rulesets/{core,cairn2e,worldswithoutnumber,academia-arcana}`.
- `content/library.py`, `content/character.py`, `content/definitions.py`, `rules/context.py`.
- `rules/{core,cairn2e,wwn,wwn_magic,wwn_progression}.py`.
- All `services/**` modules with ruleset-facing behavior, with deep dives into combat, inventory, market, advancement, magic, rest, clock, travel, wilderness, factions, and compendium.
- The test suite’s ruleset contracts, strictness tests, export/import coverage, and fixture layout.

Findings use these IDs:

- **D** — exact duplication.
- **N** — near-duplication / parameterization gap.
- **L** — ruleset-specific logic leaking into core.
- **S** — schema/loader gap.
- **B** — correctness/consistency bug found during the audit.
- **T** — test/contract friction.

Impact is rated **High**, **Medium**, or **Low**.

---

## 3. Current architecture

### 3.1 Loading and inheritance

`ContentLibrary.load()` reads every ruleset directory and merges `core` under each ruleset by `(type, id)`:

```python
# content/library.py:2674-2698 (paraphrased)
own = read_definitions(ruleset_dir)
own_keys = {(d.type, d.id) for d in own}
inherited = [
    d for d in core_definitions
    if d.type != "rule_set"
    and (d.type, d.id) not in own_keys
    and table_genre_offered(d)
]
build_rule_set(own + inherited)
```

Key consequences:

- A ruleset can override a core definition **only wholesale**.
- There is no field-level patch, no `extends`, no `remove`, and no `disabled`.
- The core `rule_set` head is **not inherited**, so `plus`, `condition_ends`, `npc_hp`, `table_genres`, `skill_progression`, `optional_templates`, and `starting_silver` must be restated per ruleset.
- Lists are replaced, not merged. The only exception is `creative_table` `add`/`remove`.
- Core tables are filtered by the ruleset’s own `table_genres`; a ruleset with narrow genres can accidentally drop inherited fantasy tables.
- Unknown definition `type`s are silently dropped (see S-01).

### 3.2 Ruleset inventory

| Ruleset | Data files | Core concepts |
|---|---|---|
| `core` | 7 | rules, prompts, 64 tools, 3 NPC tables, 2 character fields, 75 dice tables, 11 creative tables, encounter terrain/objects/templates |
| `cairn2e` | ~30 | 4 stats, 20 backgrounds, 71 features, 84 monsters, 49 tools, 67 shop items, wilderness/dungeon, conditions, rest/clock/travel, 31 world terrains |
| `worldswithoutnumber` | ~30 | 20 stats, 21 skills, 50 templates, 227 features, 56 monsters, 79 tools, 74 shop items, 81 faction assets, 7 counters, 5 conditions, level-up/progression, 30 world terrains |
| `academia-arcana` | ~30 | 5 stats, 12 templates, 99 features, 17 moves, 9 conditions, 5 counters, advancements/periods/tallies/relations, 10 shop items, 14 world terrains |

### 3.3 Engine and hooks

The engine already has strong generic primitives:

- `services/resolution.py` — one roll path for `roll_under`/`roll_over`.
- `services/effects.py` — effect-op registry/executor.
- `services/conditions.py`, `counters.py`, `periods.py`, `tallies.py`, `relations.py` — data-driven.
- `services/hooks.py` — arbitrary named hooks with `options` and `configure`.
- `services/combat/damage.py` — data-driven damage steps.
- `services/morale.py` — data-driven triggers.
- `content/library.py` tool enrichment — `world_method`, `enum_from`, `npc_hp_tier`, `travel`.

The weak points are where core services encode one ruleset’s procedure, or where a ruleset’s Python module duplicates data that also lives in JSON.

### 3.4 Test contracts

The suite is excellent at guarding the generic engine (`test_contract_ruleset.py`, `rolltest`) and the event log. However, ruleset-specific facts are restated as literals in ~15 files. The most visible ambiguity is that the core-tool constants describe two different scopes:

- `tests/test_tools.py:126` lists **61** regularly offered tools.
- `tests/test_content.py:92` and `tests/test_tool_binding.py:212` cover **64** definitions, including three turn-phase tools (`append_journal`, `replace_in_journal`, `undo_narration`) that are not in the regular offered set.

This is not necessarily behavior drift, but the names (`CORE_TOOLS`, `CORE_TOOL_NAMES`) do not say which scope they mean, so the contract is easy to misread.

There are also exact counts for monsters, assets, spells, regions, kits, skills, moves, and conditions, plus ~15 per-type unknown-key rejection tests.

---

## 4. Feature matrix

| Concept | Cairn 2e | Worlds Without Number | Academia Arcana |
|---|---|---|---|
| Stats | 4 rolled stats, no modifier bands | 20 stats, modifier bands, derived saves/effort | 5 assigned-value stats |
| Skills | — | 21 skills, 2d6 checks | — |
| Moves | — | — | 17 moves with bands/choices |
| Character fields | physique/skin/hair/face/speech/clothing/virtue/vice/bond | look/personality/accent/quirk/goal | major/college/signature-spell/ambition |
| Templates | 20 backgrounds | 9 classes + 7 traditions + 5 class2 + 6 kits | 5 playbooks + 7 year packages |
| Features | items + 1 spell | spells, arts, foci, equipment | spells, potions, enchantments, subjects, feats |
| Inventory | single pool, 10 slots, full-pack HP drain | 2 pools (stowed/readied), no drain | single pool, 10 slots, no drain |
| Combat | auto-hit, armor reduces damage, injuries, morale | roll-vs-defense, Shock, dual wield, execution | inherits Cairn combat |
| Magic | item-based casting | prepared spells + castings + Effort + Strain | move-based `cast_a_spell` + Stress |
| Rest | data-driven restores/clears | Python recovery, castings/Strain/Effort | data-driven, no HP recovery |
| Travel | authored modes + wilderness watches | Python constants + separate `travel` tool | narrative fallback |
| Progression | none | XP tracks + level-up picks + dual-partial classes | advancement counter + year periods |
| Social | conditions | conditions, counters, checks, projects, factions | conditions, counters, moves, relations, tallies, periods |
| World terrain | 31 | 30 | 14 |
| Tools | 49 | 79 | 16 |
| Prompts | 19 | 19 | 16 + core 3 |

---

## 5. Findings

### 5.1 Exact duplication (D)

| ID | Finding | Evidence | Impact |
|---|---|---|---|
| **D-01** | **38 duplicated tool ids** across rulesets; ~1,300–1,800 JSON lines. Cairn+WWN share 33 ids (`attack`, `rest`, `buy_item`, `start_encounter`, …); Cairn+AA share 6; WWN+AA share 9, including 4 project tools. Schemas drift, and the same id can have different semantics (WWN `rest` is per-character). | `cairn2e/tool_calls.json`, `worldswithoutnumber/adventure_tools.json`, `academia-arcana/tool_calls.json`; examples: `advance_time` (Cairn 715, WWN 565, AA 257), `rest` (Cairn 748, WWN 539, AA 290), `search_marketplace` (Cairn 1130, WWN 916, AA 329) | High |
| **D-02** | **Core personality/accent/quirk tables copied into WWN** to change one value. 33 of 34 entries are identical; core says “mushroom”, WWN says “carved stone”. Core is Cairn-flavored. | `core/rule_set.json:64-112`; `worldswithoutnumber/tables.json:3-53` | High |
| **D-03** | **WWN XP arrays and skill-level minimums stored twice**: `level_up.json:11-12,24` equals `progression.json:134-135,220-228`. | as cited | High |
| **D-04** | **WWN class constants repeated 14×**: every class row has `hit_die: 6` and `focus_levels: [2,5,7,10]`. | `worldswithoutnumber/progression.json:22-132` | High |
| **D-05** | **WWN focus exclusion lists copied 4× each** (warrior 22 entries, expert 15 entries) across `classes.json` and `templates.json`; `focus_whirlwind_assault` appears in four lists. | `classes.json:22-43,66-81,190-211,219-234,259-274,300-321`; `templates.json:14-35,64-79` | High |
| **D-06** | **Academia year packages repeat constants**: 7-subject tag list 14×, 5-forbidden-spell list 8× (7 year templates + `advancements.json`), and the year progression is restated in `rules.json`, `periods.json`, `counters.json`, and `prompts.json`. | `academia-arcana/templates.json:78,84,104,…,244`; `advancements.json:21` | High |
| **D-07** | **Clock data is identical between Cairn and Academia except the definition id and `wage_currency`.** | `cairn2e/clock.json:3-11`; `academia-arcana/clock.json:3-11` | Medium |
| **D-08** | **28 of 31 world-terrain ids shared between Cairn and WWN**; visuals are identical, only `rule` prose differs. Academia reuses several visuals too. | `cairn2e/world_terrain.json:22-29`; `worldswithoutnumber/world_terrain.json:22-29`; `academia-arcana/world_terrain.json:31-38` | High |
| **D-09** | **WWN custom-feature/item currency duplicated**: `silver` feature defined in both `equipment.json` and `academia-arcana/features.json`; currency id repeated in `marketplace.currency` and `clock.wage_currency`. | `worldswithoutnumber/equipment.json:78-84`; `academia-arcana/features.json:3-9` | Low |
| **D-10** | **Common tool schemas duplicated**: `search_marketplace`, `buy_item`, `buy_service`, `advance_time`, `rest`, `rest_history`, project tools, map tools. | as D-01 | High |
| **D-11** | **Dead duplicate combat helpers**: `services/combat/damage.py:124-219` is byte-identical to `services/combat/attack.py:61-155`; `combat/__init__.py` exports only the `attack.py` versions. | as cited | Medium (quick win) |
| **D-12** | **Penalty-item construction duplicated** in `inventory.penalty_item`, `magic.py:145-149`, and `rules/cairn2e.py:126-129`. | as cited | Low |
| **D-13** | **Same physical item defined twice** in Cairn/WWN: `features.json` item vs `marketplace.json` shop item, with independent size/price/uses/armor fields. | `cairn2e/features.json:278,404` vs `marketplace.json:590,242`; `WWN/equipment.json` vs shop items | High |

### 5.2 Near-duplication and parameterization gaps (N)

| ID | Finding | Evidence | Proposal |
|---|---|---|---|
| **N-01** | **Rest is the same skeleton with a renamed long rest.** `moments` and `night` are structurally identical across all three; differences are `restores`/`clears`/`ends`/`upkeep` and `week`/`extended`/`downtime`. | `cairn2e/rest.json:3-60`; `WWN/rest.json:3-40`; `AA/rest.json:3-40` | Core rest preset + per-ruleset overrides + length aliases (never rename persisted `RestTaken.length`). |
| **N-02** | **Academia conditions encode three identical mechanical pairs**: injured/burnout (`*: -1`), exhausted/arcana-fatigue (`practice: -2`), marked/suspicion (`cunning: -2`). | `academia-arcana/conditions.json:3-8,11-16,35-40,43-48,59-64,67-72` | Condition templates/presets; keep ids/prose. |
| **N-03** | **Projects have the same shape**; WWN adds cost/skill keys, AA omits them. | `WWN/projects.json`; `AA/projects.json` | Shared base + optional fields. |
| **N-04** | **Map tools duplicated**: core `create_local_map`/`update_local_map`/`set_map_cells`; Cairn/WWN add encounter-map tools; Cairn redundantly overrides `set_map_cells`. | `core/tool_calls.json:1472-1758`; `cairn2e/tool_calls.json:1320-1582`; `WWN/adventure_tools.json:1029-1104` | One map-tool family with mode/parameter; keep ids. |
| **N-05** | **Three models of appearance**: core `appearance`, WWN `look`, Cairn six fields; `goal` vs `ambition` are the same concept. | `core/character_fields.json`; `WWN/character_fields.json`; `cairn2e/fields.json`; `AA/character_fields.json` | Canonical field vocabulary with aliases; do not rename persisted field ids without migration. |
| **N-06** | **Prompt ids collide with different text** (`player_rolls`, `encounter_start`, `monster_abilities`), and different ids cover the same concern (`conditions` vs `condition_tools`; `time_and_rest` vs `rest_and_recovery` vs `downtime_and_market`). | ruleset `prompts.json` files | Shared prompt library with `supersedes`/`requires`, and neutral core fragments. |
| **N-07** | **`setting_forge` and `academy_forge` both fire** because both are `unwritten_only`; the academy world has no authored setting. | `core/prompts.json`; `academia-arcana/prompts.json` | Add prompt `supersedes`; let academia replace the core forge. |
| **N-08** | **WWN stat modifier bands/effort/save definitions are copy-paste blocks**: 6× modifier bands, 3× effort derivations, 3× save bases. | `WWN/stats.json:4-61,170-292,298-355` | Named presets (`modifier_bands`, `save_base`, `effort_stat`). |
| **N-09** | **WWN class data encoded three times** (`progression.json`, `classes.json`, `templates.json`), with class ids needing manual sync. | as cited | Make `progression.classes` authoritative and derive template stats; or a single class record with views. |
| **N-10** | **Starting equipment has two incompatible models**: Cairn `features` + dice-string quantities; WWN `grant.items` by display name with `equip`. | `cairn2e/backgrounds.json`; `WWN/templates.json:150-205` | Shared `grant` model with item id/quantity/equip and computed starting currency. |
| **N-11** | **Academia Year templates are 7 copies** of the same constants, growing by one pick each year. | `AA/templates.json:70-247` | One `template_pool`/row set with per-year counts. |
| **N-12** | **WWN growth/learning tables**: 20 backgrounds × 2 tables from one base pool; growth_artisan and growth_barbarian are identical; background skills encoded three ways. | `WWN/tables.json:195-695`; `backgrounds.json:7`; `tables.json:110-190` | Shared pool + per-background delta. |
| **N-13** | **Cairn `dungeon_reaction` overrides a near-identical core table** (same dice/bands, short labels). | `core/worldgen_tables.json:2677-2712`; `cairn2e/dungeon.json:60-104` | Patch core table or keep only label deltas. |
| **N-14** | **WWN travel data in four places**: rule prose, tool description, Python constants, terrain prose. | `WWN/rules.json:119-121`; `adventure_tools.json:144`; `rules/wwn.py:1447-1460`; `world_terrain.json` | `JourneyRules` in data; tool enum generated; Python formatter only. |
| **N-15** | **Academia training features hand-expanded**: 15 `training-<stat>-<tier>` features differing only by cap (2/3/4), repeated as `training-7`. | `AA/features.json:78-207`; `templates.json` | One feature per stat with counter-scaled effects, or generated at load. |

### 5.3 Ruleset-specific behavior in core (L)

| ID | Leak | Evidence | Target |
|---|---|---|---|
| **L-01** | **Magic is item-only in the generic `MagicRules` model**: exactly three roles (reusable, single-use, charged). Prepared-spell and move-based casting cannot declare themselves. | `content/library.py:4784-4804`; `services/magic.py:109-159` | `MagicRules.model: item | prepared | move` with adapters. |
| **L-02** | **Shock is a core character/feature concept**: `Feature.shock/shock_ac`, `Character.attack_shock`, NPC parsing, combat, compendium. WWN-only. | `content/character.py:213-216`; `state.py:266-268`; `world_support.py:1226-1265`; `combat/attack.py:148-155` | Generic `weapon_modifiers`/attack traits; keep `shock` aliases for back-compat. |
| **L-03** | **Custom features carry Academia fields in core**: `subject`, `base`, `mods`, `stress` in `CustomFeature`, `CustomFeatureCreated`, and the core GM tool. | `custom_features.py:31,114-117`; `state.py:160-174`; `events.py:3418-3455`; `core/tool_calls.json:1760-1846` | Generic `extras` mapping validated per ruleset; legacy decoder. |
| **L-04** | **`FEATURE_TYPES` is a closed enum with ruleset-specific words** (`potion`, `enchantment`, `subject`), re-checked in three places; `spell` is uniquely default. | `content/character.py:22,1630`; `library.py:5545-5549,6709-6711`; `custom_features.py:31`; `character_form.py:517-524` | Per-ruleset `feature_types` registry with defaults/labels/unique flags. |
| **L-05** | **Faction “cup” vocabulary and five-faction cap are generic**: `MAX_FACTIONS = 5`, `"cup"` key, core tool text names Colleges/College Cup. | `services/factions.py:38,557`; `core/tool_calls.json:1895-2000` | `TallyRules.points_label`/`max_entries`; neutral tool text. |
| **L-06** | **Seven-day week is hardcoded** in the clock service. | `services/clock.py:90-92` | `ClockRules.week_days` (default 7). |
| **L-07** | **WWN rest recovery hardcodes rest ids and amounts** (`night`/`extended`, level×nights HP, strain, castings, frail). | `rules/wwn.py:1303,1317-1322` | `RestLength.restores` effect specs or `after_rest` hook. |
| **L-08** | **Overland travel watch algorithm lives in the generic travel mixin**, with Cairn-specific fatigue/wilderness calls. | `services/world_travel.py:79-190`; `wilderness.py:523-565` | `JourneyRules`/`overland_journey` hook; keep Cairn as one implementation. |
| **L-09** | **Cairn Fatigue is the universal spell cost** in the shared magic service. | `services/magic.py:145-149` | `MagicRules.cast_cost` data. |
| **L-10** | **Inventory decides “is equipment” from combat fields** (`item.damage`, `shield`/`armor` flags) to choose the Readied pool. | `inventory.py:263-265` | `ShopItem.slot`/`ItemKind` metadata. |
| **L-11** | **Shield bash recognized by English name prefix**, not an attack flag. | `combat/attack.py:869-874` | `AttackFlag(suspends_shield=True)`. |
| **L-12** | **Condition-end validation is dead**: `condition_ends` is checked but no caller passes it. | `services/conditions.py:88-113`; callers omit it (`world.py:1698`, `rests.py:131`, `aid.py:56`, `combat/save.py:189`) | Thread it through or remove the claim. |
| **L-13** | **WWN class/tradition mappings and focus special cases are hardcoded**: `_CLASS_TRADITIONS`, `focus_die_hard`, `"mage"`, `"full_mage"`, `("fast","slow")`. | `rules/wwn.py:55-60,413-419,530,573-590` | Move to `progression.json`; use existing `full_mage_classes`/`partial_mage_classes`. |
| **L-14** | **Compendium taxonomy is hardcoded to WWN/Academia vocabulary** (Arts/Foci/Feats/Spells, College Cup). | `services/compendium.py:68-118,859-871` | Ruleset `compendium_groups`/labels. |
| **L-15** | **`World` protocol unions optional capabilities**; ruleset modules can call `cast_spell`/`wilderness_action` even when absent. | `rules/context.py:307-420` | Split capability protocols or expose `ctx.capabilities`. |
| **L-16** | **Core tool descriptions encode ruleset vocabulary**: `lookup_feature` names spells/arts/Focus; `create_custom_feature` names spells/potions/enchantments/Stress/subject; faction tools name Colleges. | `core/tool_calls.json:1329-1335,1763-1809,1895-2000`; `world.py:2060-2067` | Neutral descriptions + capability-gated optional tools. |

### 5.4 Schema and loader gaps (S)

| ID | Finding | Evidence | Proposal |
|---|---|---|---|
| **S-01** | **Unknown definition `type` is silently discarded**; worlds additionally restrict to `WORLD_TYPES` and drop the rest without diagnosis. | `library.py:1918,1942`; no residual check in `_build_rule_set` | Type registry + “all definitions consumed” assertion; warn/allow flag for third-party content. |
| **S-02** | **No `extends`/patch/remove/disable layer**; rulesets can only replace definitions wholesale and cannot drop inherited core content. | `_load_rule_set` `2686-2697`; only `creative_table` patches | Add `extends`, `patch`, `remove`, `disabled`, with cycle checks; content without them behaves as today. |
| **S-03** | **Core `rule_set` head is not inherited**, so shared defaults (`plus`, `condition_ends`, `npc_hp`, `table_genres`, `skill_progression`, …) cannot be centralized. | `library.py:2689` | Add a separate defaults layer or inherit a base head. |
| **S-04** | **Two effect-op registries with divergent `counter` payloads**; features cannot apply conditions/`set_max`, moves cannot add armor/stats. | `character.py:26,1128-1164`; `library.py:6243-6285` | One op registry with capability contexts (`feature`, `move`, `condition`, …). |
| **S-05** | **Inventory has two models in one schema without exclusivity**; `capacity` silently becomes 0 when pools are present and omitted. | `library.py:1426-1461,4604-4686` | Discriminated `model: single | pools`; validate exclusivity. |
| **S-06** | **Three progression systems with no shared budget concept**: `advancement`, `level_up`, `period`. | `library.py:5314-5463,5680-5726` | Shared grants/budget abstraction; allow multiple tracks. |
| **S-07** | **Two travel models**, validated asymmetrically; routes validated only against wilderness. | `travel` `2953-3007`; `wilderness.travel` `3092-3131`; `_check_authored_routes` `2265-2287` | Explicit `model`/`JourneyRules`; forbid both in one ruleset. |
| **S-08** | **Two terrain schemas** (`encounter_terrain` vs `world_terrain`) with shared visual fields. | `library.py:4107-4185` | Shared visual base; encounter adds movement, world adds rules. |
| **S-09** | **Closed/hardcoded vocabularies**: `"dex"` initiative default, item size `"normal"`, `FEATURE_TYPES`, `"panicking"`, `"attributes"` group label, `"morning"` rest `until`, `SHOP_EFFECTS`. | `library.py:1117,3483,4959-4967`; `character.py:943`; `wilderness.py:212`; `compendium.py:127,1242`; `4530,4559`; `market.py:283-291` | Move to ruleset data with compatibility defaults. |
| **S-10** | **Tool parameter schemas are barely validated**; `_check_parameters` validates only `type: object` and `properties`. | `library.py:7002-7016` | Validate property types/enums/defaults and signature alignment. |
| **S-11** | **Hook names are unvalidated free strings**; typos load and never run. | `library.py:4933-4941`; `services/hooks.py:53-63`; call sites in `clock.py:114`, `rests.py:84,100`, `wilderness.py:244,348`, `market.py:262`, `views.py:348`, `world.py:277`, `world_travel.py:113` | Hook registry with known names/arities; warn on unknown. |
| **S-12** | **World features are under-validated**: loaded with no stat/skill/counter checks; world rules reject `important`. | `library.py:2061-2166` | Validate world features against the ruleset; align rule schema. |
| **S-13** | **`ContentLibrary` mutates a cache on a frozen dataclass**, lazily and unbounded, with no lock. | `library.py:1843-1856` | Lock/bound or precompute. |
| **S-14** | **Key allowlists duplicate loader fields** (`CHARACTER_DEFINITION_KEYS`, `FEATURE_EFFECT_KEYS`, pick/choose/grant keys), requiring two-file edits. | `library.py:2482-2608`; `character.py:922-1020,1370-1430,1522-1660` | Derive allowlists from dataclasses/schemas. |
| **S-15** | **Cross-type id collisions bypass the merge key** and surface later as duplicate dice-table errors; same-id table/dice-table allowed. | `library.py:2684-2685,2168-2184,2138-2141` | Global `(type,id)` diagnostics or namespacing. |
| **S-16** | **Cairn's `magic` block is named generically but models only item roles**; `_load_magic` requires all three distinct roles. | `library.py:4784-4804` | Model discriminator + optional roles. |

### 5.5 Correctness and consistency issues found during the audit (B)

These are not merely refactors; they should be verified and likely fixed first.

| ID | Issue | Evidence | Impact |
|---|---|---|---|
| **B-01** | **WWN rest advances the clock once per character.** `wwn.rest` takes a character and calls party-level `world.rest`, which records `RestTaken` and advances the clock. The prompt tells the GM to call it per character, so N characters produce N nights; only the named character is recovered. | `rules/wwn.py:1251-1267`; `services/world.py:242-260`; `services/rests.py:38-110`; `WWN/prompts.json:55-56` | High |
| **B-02** | **WWN class attack values are encoded twice and already drift.** `classes.json` gives Warrior `attack_bonus: 0`; `progression.json` begins at `1`. Creation and level-up read different sources. Verify against the SRD and make one authoritative. | `WWN/classes.json:4-8`; `WWN/progression.json:13-20`; `rules/wwn.py:535-540`; tests assert only id sets | High |
| **B-03** | **XP thresholds have two live readers**: the advancement page reads `level_up.json` tracks; `apply_level_up`/`gain_xp` read `progression.xp_*`. A drift makes the page say “ready” and the tool refuse. | `services/level_up.py:56-72`; `rules/wwn.py:421-470,478-527,592-620` | High |
| **B-04** | **WWN skill/attribute costs exist as Python constants and `level_up.json`**: `SKILL_LEVEL_COSTS`, `ATTRIBUTE_BOOST_MINIMUMS`, `MAX_ATTRIBUTE` vs JSON costs/minimums/maximum. | `rules/wwn.py:959-961,43`; `WWN/level_up.json:31-63`; `services/level_up.py:181-257` | Medium |
| **B-05** | **Academia Year cap data contradicts its text**: text says +2 Years 1–3, +3 Years 4–6, +4 Year 7; effect is a static `cap: 4`. | `AA/advancements.json:8,13`; `library.py:5379-5389`; tests pin static behavior | Medium |
| **B-06** | **WWN `site_turns` reference likely wrong**: it rolls `dungeon_event`, a core 1d20 omen table, as a wandering check; likely meant the dungeon event/reaction table. | `WWN/prompts.json:66`; `core/worldgen_tables.json:4983-5065` | Medium |
| **B-07** | **`setting_forge` and `academy_forge` both fire** in the Academy opening; conflicting instructions. | `core/prompts.json`; `AA/prompts.json`; `library.py:296-300` | Medium |
| **B-08** | **Advancement page and validator disagree**: `option_views` omits `repeatable` and rank-order checks that `_learnable`/`check_ordered_feature_rank` enforce. | `services/level_up.py:259-308`; `services/advancements.py:133-170`; `content/character.py:3075+` | Medium |
| **B-09** | **Item-use spending implemented four ways**; wilderness `_spend_use` bypasses readiness/charged/consumable checks. | `inventory.py:508-547`; `wilderness.py:368-382`; `rules/cairn2e.py:99-108`; `magic.py:163-179` | Medium |
| **B-10** | **`best_stat` compares raw scores, while the roll uses `spec.modifier`**; a banded-stat ruleset could pick the wrong stat. | `services/moves.py:109-110,319-321,120-124` | Low |
| **B-11** | **`learn_spell` spending differs from focus/art**: focus/art decrement internally, spell is decremented by the wrapper. | `rules/wwn.py:875-888` | Low |
| **B-12** | **Level-up grants are computed twice** (`level_up_summary` and `apply_level_up`), then reconciled with a `current < new_level` heuristic. | `services/level_up.py:339-350,389`; `rules/wwn.py:478-666` | High |
| **B-13** | **Only the first advancement/level-up definition is used**; multiple tracks are rejected. | `library.py:5329,5401`; `services/world.py:893`; `services/level_up.py:110` | Medium |
| **B-14** | **`_blast` and `_blast_attack` are 80% duplicate**, including per-target state reloads and `_strike` kwargs. | `combat/attack.py:425-573` | Medium |
| **B-15** | **`condition_ends` is never passed**, so the documented validation never runs. | `services/conditions.py:88-113` | Medium |
| **B-16** | **Shield bash and shield-round behavior depend on English names and flags** rather than attack metadata. | `combat/attack.py:853-874` | Medium |

**Verification note:** a reported `_load_save` inversion for `roll_over` was checked and rejected. When the save block is absent, `mode` defaults to `roll_under`, so `auto_success=1` / `auto_fail=check_sides` is correct; when the block sets `mode: roll_over` and omits the auto fields, they remain `None` (`library.py:3506-3523`). No change is needed there.

### 5.6 Test and contract friction (T)

| ID | Finding | Evidence |
|---|---|---|
| **T-01** | **Core-tool test constants use ambiguous scopes**: 61 “regularly offered” tools vs 64 “all definitions” (including three turn-phase tools). The numbers are not necessarily wrong, but the names do not say which set they mean. | `test_content.py:92`; `test_tools.py:126`; `test_tool_binding.py:212` |
| **T-02** | **WWN tool binding facts are asserted in four places**, including exact wrapper signatures and an exact non-core binding dict. | `test_tool_binding.py:331`; `test_wwn_magic.py:456-509,512+`; `test_wwn_contract.py:24` |
| **T-03** | **~15 per-type unknown-key rejection tests** must be updated in lockstep with schema changes. | `test_library_strictness.py`, `test_content.py`, `test_inventory_schema.py`, `test_moves.py`, `test_wilderness_content.py`, `test_creative_tables.py`, `test_combat.py`, etc. |
| **T-04** | **Exact content counts are pinned**: 84 monsters, 81 assets, 40 spells, 12 regions, 6 kits, 21 skills, 17 moves, 9 conditions, 15 potions/enchantments. | §3.2 of the test audit |
| **T-05** | **Export/import covers only core events**; nothing round-trips `Move*`, `FactionAsset*`, wilderness/weather, injuries, encounter turns, feature/skill changes, custom features, narration undo, or debug events. | `test_export_import.py:79-170` |
| **T-06** | **No test for importing a campaign whose ruleset is missing/renamed**, or for a renamed content id. | only model fallback at `test_export_import.py:400` |
| **T-07** | **No unknown-payload-key tolerance test**, though `decode()` filters unknown keys and rolling upgrades depend on it. | `test_export_import.py:440` covers unknown kind only |
| **T-08** | **Fixture and shipped content share one id namespace**; a new bundled `rolltest`/`testhollow` would silently shadow a fixture. | `tests/conftest.py:38-51` |
| **T-09** | **Prompt fragment id lists are exact and ordered**; harmless rewording/reordering breaks tests. | `test_content.py:1242-1268` |
| **T-10** | **`character_schema` and campaign schema assert exact property sets and `additionalProperties: False`**, blocking additive fields. | `test_character.py:197,214`; `test_content.py:1166-1180` |

---

## 6. Target architecture

### 6.1 Capability manifest

Add a `capabilities` block to each `rule_set.json`:

```json
"capabilities": {
  "magic": "item | prepared | move | none",
  "travel": "terrain | narrative | none",
  "wilderness": true,
  "dungeon": true,
  "social": ["conditions", "counters", "checks", "moves",
             "relations", "tallies", "factions", "projects", "periods"]
}
```

Use it to:

- gate inherited `tool_call` and `prompt` definitions (`requires`/`enabled_by`);
- validate that a ruleset’s data supports the tools it inherits;
- let a fourth ruleset opt out of core tools it cannot use;
- keep inference as a compatibility fallback when `capabilities` is absent.

This directly addresses L-01, L-05, L-16, S-02, and the “cannot remove core content” problem.

### 6.2 Core defaults and overlays

Do **not** add default `rest`, `clock`, `travel`, `wilderness`, or `magic` as ordinary core definitions: the loader rejects multiple singleton definitions and would load `core/defaults/` as content. Use a separate defaults layer:

- a single `{"type": "defaults", "id": "core", ...}` definition, or
- a top-level `data/defaults/` directory excluded from `read_definitions`.

Suggested merge rules:

- `clock`: field-wise overlay.
- `rest`: merge `lengths` by id; explicit removal allowed; aliases for persisted names.
- `world_terrain`: core supplies **looks only** (`name`, `color`, `pattern`, `ink`); rulesets list ids and supply `rule`.
- `prompts`: neutral core fragments by id; rulesets override or `supersedes`.
- `tool_calls`: neutral core tools by id; rulesets override or gate by capability.

Add `extends`/`patch`/`remove` for definitions:

```jsonc
// worldswithoutnumber/tables.json
{"type": "table", "id": "quirk", "extends": "core:table/quirk",
 "patch": {"options": {"collects small bits of mushroom":
                       "collects small bits of carved stone"}}}
```

Content without `extends` behaves exactly as today. Resolve after the core merge and before validation, with cycle checks. This is the single highest-leverage change: it removes D-02, D-07, D-08, N-01/N-07/N-12/N-13/N-15 and the “restate the whole map to change one cell” problem.

### 6.3 Definition-type registry

Add a `DEFINITION_TYPES` registry and a post-build assertion that every definition was consumed. Worlds need the same treatment. This fixes S-01 and gives content authors a load-time error instead of silent missing content.

### 6.4 Unified effect-op registry

Unify `FEATURE_EFFECT_OPS` and `EFFECT_RULES` into one table:

```text
op → (payload keys, validator, contexts)
contexts ⊆ {feature, move, condition, counter, advancement, period, injury, band_choice}
```

Keep both current op sets as the initial contents. This fixes the `counter` payload collision (S-04), lets a trait grant a condition, and lets a move add armor. Existing JSON remains valid.

### 6.5 Feature-type registry

Replace the closed `FEATURE_TYPES` with per-ruleset data:

```json
{"type": "feature_type", "id": "spell", "name": "Spell", "plural": "Spells",
 "group": "Magic", "rank_label": "Rank", "tag_label": "Tradition",
 "unique": true, "inventory": false}
```

Use it for `FeatPick`/`Choose`/`enum_from`, the character form labels, custom-feature types, and compendium grouping. Default to the current seven types for compatibility. This fixes L-03/L-04 and the Arts-vs-Feats labeling inconsistency.

### 6.6 Tools and prompts

- Move shared tool schemas to core with `group` metadata and `requires`/`enabled_by` gating.
- Generate `size`/`feature_type`/`rest_length` enums with `enum_from`.
- Parameterize `{currency}`, `{item_size_vocab}`, and similar text.
- Keep tool ids stable. A ruleset should override only genuine mechanical differences.
- Add prompt `supersedes` and shared fragments; neutralize core text. Keep fragment ids stable where models/tests depend on them.

Expected result: ~1,300–1,800 fewer JSON lines and no duplicate `buy_item`/`rest`/`advance_time` drift.

### 6.7 Items and inventory

- One item definition with optional `price`, `category`, `slot` (`readied`/`stowed`/`by_size`), and equipment traits.
- Shop listings become a view of items (`price: null` means not sold), removing D-13.
- Normalize the single-pool inventory to one implicit pool; keep `full_stat` as a pool `full_effects` list.
- Add `ShopItem.slot` and derive the Readied-pool decision from data (L-10).
- Keep persisted item size ids and item names.

### 6.8 Magic adapters

Add `MagicRules.model`:

- `item` — Cairn’s current behavior, moved into an adapter.
- `prepared` — WWN castings/Effort/Strain adapter.
- `move` — Academia’s `cast_a_spell` move adapter.

Keep `SpellCast` as the common audit event. Add optional `model`/`resource` fields with defaults. Add `cast_cost` data (L-09) and `rank_label`/`tag_label` metadata for display.

### 6.9 Rest, clock, and journeys

- Make generic `rest` party-level; add `scope` (`party`/`character`) and an `after_rest` hook.
- Convert WWN `_rest_recovery` into an `after_rest` hook; remove `restore_spell_slots` duplication.
- Add `ClockRules.week_days` (default 7) and drive reaction resets from data/periods.
- Add `JourneyRules` with `model` (`watches`/`terrain_days`), `day_hours`, terrain speeds, road multiplier, weather factors, supplies, forage, privation.
- Move WWN travel constants into data; make the `travel` tool a thin formatter/mover.
- Keep Cairn’s watch/wilderness procedure as one journey model; preserve event order and RNG draws.

This addresses L-06/L-07/L-08, N-01, N-14, S-07, and B-01.

### 6.10 Progression

- One `project_level(found, progression) -> LevelGrants` model. `summary` serializes it; `apply_level` applies it and returns the same grants; `_plan_picks` consumes them. Removes B-12.
- One XP source: `progression.xp` or `level_up.tracks`, with a loader cross-check until the duplicate is removed. Removes B-03.
- Move skill/attribute costs, class tables, focus levels, tradition mapping, and full/partial caster classification into `progression.json`; `wwn.py` becomes arithmetic. Addresses B-02/B-04/L-13.
- Allow multiple advancement/level-up definitions keyed by counter/track; select explicitly. Addresses B-13.
- Make `advancement` caps counter-aware, e.g. `cap: {"counter": "year", "bands": {...}}`. Addresses B-05.
- Share `learnable_features(...)` between the page and validator. Addresses B-08.
- Keep persisted option ids, template ids, feature ids, and counter keys.

### 6.11 Hook contract

Keep the current arbitrary-name hook mechanism. Add:

- a registry of known hook names and arities;
- standard points: `new_day`, `before_rest`, `after_rest`, `held_back`, `after_consume`, `lodge`, `status`, plus `on_spell_cast`, `on_advancement`, `on_period_end`, `on_faction_turn`;
- optional `args`/`returns` metadata in the `hooks` definition;
- a load-time warning for unknown hook names.

This turns silent typos into diagnostics (S-11) and gives a fourth ruleset a clear extension surface.

### 6.12 World validation

- Validate world features against the ruleset’s stats/skills/counters.
- Allow `important` on world rules, or document the difference.
- Add a direct test that fixture ids are disjoint from shipped ids.

---

## 7. Prioritized roadmap

### Phase 0 — Freeze behavior (before any refactor)

- Add an export/import fixture for one campaign per current ruleset.
- Add a golden content-id snapshot per ruleset (stats, features, templates, conditions, counters, relations, item sizes, periods, tallies, terrain).
- Add a ruleset-event round-trip matrix (combat, progression, inventory, wilderness, maps, journal, narration, custom features, debug).
- Add a “campaign whose ruleset is missing” import/load test.
- Add an unknown-payload-key tolerance test.
- Add a populated old-DB migration/fold test.
- Name the two core-tool test scopes explicitly and derive the turn-phase-only set from `journal_step`/`review_step`.

**Exit criteria:** all tests green; the current ids and event payloads are frozen.

### Phase 1 — Correctness fixes (low risk, high value)

1. **B-01** WWN rest: one party-level rest, `after_rest` recovery; keep the `rest` tool id as a compatibility shim.
2. **B-02/B-03/B-04** one source for class attack, XP, and skill/attribute costs; add loader cross-checks now.
3. **B-05** Academia Year cap: verify SRD; either per-year caps or correct the text.
4. **B-06** verify `site_turns` table reference; fix the likely `dungeon_event` typo.
5. **B-07** add prompt `supersedes` so only one forge prompt fires.
6. **B-08** page/validator share `learnable_features`.
7. **B-09** one `inventory.spend_use` used by wilderness/Cairn/magic.
8. **B-10/B-11** `best_stat` uses modifiers; move spell-pick decrement into `learn_spell`.
9. **B-15** thread `condition_ends` through callers or remove the dead parameter.
10. **B-16** replace shield-bash name matching with an attack flag.
12. Delete dead `combat/damage.py` helper copies (D-11).

**Exit criteria:** each fix has a regression test; no event ids or persisted ids change.

### Phase 2 — Capability manifest and type registry

- Add `capabilities` to `rule_set.json` with inference fallback.
- Add `requires`/`enabled_by` to `ToolCall` and `PromptFragment`.
- Add `DEFINITION_TYPES` + consumed-definition assertions.
- Neutralize core tool/prompt text; gate optional AA/WWN tools.
- Add `feature_types` registry and `enum_from: feature_types`.

**Exit criteria:** a minimal fourth test ruleset can inherit core without AA/WWN vocabulary; unknown types fail loudly.

### Phase 3 — Defaults and low-risk data deduplication

- Add the defaults layer and `extends`/`patch`/`remove`.
- Deduplicate clock/rest structure, terrain visuals, personality/accent/quirk, and neutral prompts.
- Move shared tool schemas to core with generated enums.
- Collapse WWN XP/class/focus data and Academia year packages.
- Replace Academia’s 15 training features with a counter-scaled feature or generated rows.

**Exit criteria:** net JSON reduction ≥1,000 lines; existing ids and behavior unchanged.

### Phase 4 — Items, inventory, and magic

- Unify item/shop definitions; add `slot` metadata; normalize inventory pools.
- Add `MagicRules.model` and adapters for item/prepared/move.
- Keep `SpellCast` payloads compatible; add optional fields with defaults.
- Add `rank_label`/`tag_label` and feature-type display metadata.

**Exit criteria:** all three rulesets use the same magic facade; saved campaigns still fold and export/import.

### Phase 5 — Rest, clock, journeys, progression

- Party rest + `after_rest`; `ClockRules.week_days`.
- `JourneyRules` and a data-driven WWN travel; Cairn watch model as one implementation.
- Single `project_level` grants model; one XP source; multiple progression tracks; counter-aware caps.
- Replace WWN hardcoded traditions/focus/caster mappings with progression data.

**Exit criteria:** deterministic event/RNG order preserved; WWN rest/travel and level-up tests pass; new rulesets can add a progression track without code.

### Phase 6 — Social/compendium cleanup

- `TallyRules.points_label`/`max_entries`; neutral faction text.
- Ruleset `compendium_groups`; derive labels from feature types.
- Neutral `time_view`.
- World feature validation; fixture-id disjointness test.

**Exit criteria:** no `cup`/`College`/`Stress`/`subject` wording in core; all ruleset tests green.

---

## 8. Backward-compatibility guardrails

### 8.1 Campaigns and content ids

- 4.0 does **not** preserve campaign compatibility. Every campaign row declares `major_version`; a campaign from another major stays stored and exportable but is refused by every play route.
- Content ids may be renamed freely at the 4.0 boundary. Persisted ids are frozen again after 4.0 ships; any later rename needs a campaign-major bump, not an alias.
- Existing databases must still migrate. `0014_campaign_major_version` stamps pre-4.0 rows as major 3, so they become non-playable instead of silently running under different rules.
- Importing another major's file stores its events opaquely and skips replay; the campaign remains exportable.

### 8.2 Events and exports

- Event fields added in 4.0 must have defaults, but old event kinds and legacy decoders are not preserved.
- The export document carries `campaign.major_version`. A missing key means 3, the last major that wrote format 1 without it.
- Import of another major accepts unknown event kinds, skips required-field/reference checks and state replay, and warns that the campaign is non-playable.
- New event kinds are allowed in 4.0; bump `EXPORT_VERSION` only if the document shape itself becomes incompatible.
- `SpellCast` now carries an optional `model` so move magic can share the event.

### 8.3 Tools and routes

- Tool ids may change in 4.0; they are frozen after release.
- The retired `/upgrade` route and the `CampaignUpgraded` decoder are removed at 4.0.
- `rest` remains a tool id, but WWN's implementation is party-level.
- Prompt fragment ids may be renamed; the 4.0 prompt set is the new contract.

### 8.4 Content loading

- Unknown definition types fail at load instead of being silently dropped.
- A defaults layer and `extends`/`patch` remain the preferred authoring path; bundled data may also be deduplicated directly at the 4.0 boundary.
- Fixture and shipped content share one id namespace; don’t name a bundled rule set `rolltest`/`testhollow`.

---

## 9. Quick wins

Low-risk changes with high cleanup value:

1. Delete `services/combat/damage.py:124-219` and import the shared helpers from `combat/attack.py` or a new `combat/bonuses.py` (D-11).
2. Add `ClockRules.week_days` defaulting to 7 (L-06).
3. Add `TallyRules.max_entries` and set Academia’s `college-cup` to 5 (L-05).
4. Extract `inventory.spend_use(...)` and route wilderness/Cairn/magic through it (B-09).
5. Pass or remove `condition_ends` (B-15).
6. Move the `spell_picks` decrement into `learn_spell` (B-11).
7. Make `best_stat` use `spec.modifier` (B-10).
8. Name the core-tool test scopes explicitly (T-01).
9. Add loader cross-checks for XP tracks and skill/attribute costs (B-03/B-04).
10. Neutralize core tool descriptions (`lookup_feature`, `create_custom_feature`, faction tools) (L-16).
11. Delete the redundant Cairn `set_map_cells` override and encounter-map duplicates (N-04).
12. Fix the `site_turns` table reference (B-06) and the dual forge prompt (B-07).

---

## 10. Decisions needed before implementation

1. **Overlay model:** add `extends`/`patch`, or simply move repeated data into core and accept wholesale overrides for now? The former is more powerful; the latter is a one-release win.
2. **Canonical vocabularies:** choose canonical character-field/feature-type/item-size names, or keep parallel ids with aliases. Renaming persisted ids requires a migration/alias map.
3. **Progression:** unify `level_up` and `advancement` into one model, or keep both and add a shared grants interface? The latter is safer and still removes duplication.
4. **Magic:** is one `MagicRules.model` facade enough, or should magic remain three separate ruleset modules with a common `SpellCast` contract?
5. **Export version:** are new event kinds acceptable, or should the refactor stay field-additive only? This affects forward/backward compatibility.
6. **Custom-feature extras:** generic `extras` mapping vs a ruleset-specific extension schema. The former is simpler; the latter is stricter.
7. **Tool gating:** `requires` on tools/prompts vs separate ruleset tool files. Gating is more reusable; separate files are simpler.
8. **File-name normalization:** rename Cairn’s `fields.json`, split WWN tool files, etc. Cosmetic; best delayed until after ids are frozen.

---

## 11. Appendices

### A. Ruleset file inventory (high-signal)

| Ruleset | Key files |
|---|---|
| core | `rule_set.json` (rules, NPC tables), `tool_calls.json` (64), `prompts.json` (3), `character_fields.json`, `creative_tables.json`, `worldgen_tables.json`, `encounter_content.json` |
| cairn2e | `rule_set.json`, `stats.json`, `fields.json`, `backgrounds.json`, `features.json`, `tables.json`, `warden_tables.json`, `monsters.json`, `marketplace.json`, `inventory.json`, `combat.json`, `conditions.json`, `magic.json`, `rest.json`, `clock.json`, `travel.json`, `wilderness.json`, `dungeon.json`, `world_terrain.json`, `tool_calls.json`, `prompts.json` |
| worldswithoutnumber | `rule_set.json`, `rules.json`, `stats.json`, `skills.json`, `checks.json`, `counters.json`, `conditions.json`, `tables.json`, `backgrounds.json`, `classes.json`, `templates.json`, `foci.json`, `spells.json`, `arts.json`, `monsters.json`, `faction_assets.json`, `equipment.json`, `projects.json`, `level_up.json`, `progression.json`, `world_terrain.json`, `tool_calls.json`, `adventure_tools.json`, `prompts.json` |
| academia-arcana | `rule_set.json`, `rules.json`, `stats.json`, `character_fields.json`, `templates.json`, `features.json`, `spells.json`, `potions.json`, `enchantments.json`, `moves.json`, `conditions.json`, `counters.json`, `advancements.json`, `periods.json`, `relations.json`, `tallies.json`, `projects.json`, `marketplace.json`, `inventory.json`, `world_terrain.json`, `tool_calls.json`, `prompts.json`, `magicschool_tables.json` |

### B. Representative duplicated data

**Personality table copy (D-02):**

```jsonc
// core/rule_set.json:64-112 defines personality/accent/quirk.
// WWN/tables.json:3-53 copies all three to change one option:
"collects small bits of mushroom"  // core:110
"collects small bits of carved stone" // WWN:50
```

**Rest skeleton (N-01):**

```jsonc
// All three define `moments` (1 hour) and `night` (until morning).
// Differences: restores/clears/ends/upkeep and the long-rest id:
"week"      // cairn2e
"extended"  // worldswithoutnumber
"downtime"  // academia-arcana
```

**Clock near-duplicate (D-07):**

```jsonc
// cairn2e/clock.json and academia-arcana/clock.json are identical except:
"wage_currency": "gold"    // cairn
"wage_currency": "silver"  // academia
```

**Terrain visual duplication (D-08):**

```jsonc
// cairn2e/world_terrain.json:22-29, WWN:22-29, AA:31-38
{"id":"forest","name":"Forest","color":"#4d7a3c","pattern":"trees","ink":"#24421f",
 "rule":"…"} // only `rule` differs
```

**WWN class data encoded three times (N-09):**

```
progression.classes  → hp_constant, attack array, focus_levels
classes.json         → template stats {hp, attack_bonus}
templates.json       → class2/kit grants
```

### C. Proposed schema snippets

**Capability manifest:**

```json
"capabilities": {
  "magic": "prepared",
  "travel": "terrain",
  "wilderness": false,
  "dungeon": false,
  "social": ["conditions", "counters", "checks", "factions", "projects"]
}
```

**Feature type:**

```json
{"type": "feature_type", "id": "spell", "name": "Spell", "plural": "Spells",
 "group": "Magic", "rank_label": "Rank", "tag_label": "Tradition",
 "unique": true, "inventory": false}
```

**Tool gating:**

```json
{"type": "tool_call", "id": "buy_service", "requires": "marketplace",
 "group": "market", "description": "… pays {currency} …"}
```

**Overlay:**

```json
{"type": "rest", "id": "cairn", "extends": "preset:rest/standard",
 "aliases": {"week": "extended"},
 "lengths": {"moments": {"restores": ["hp"]}, "week": {"restores": ["hp","str","dex","wil"]}}}
```

**Journey rules:**

```json
{"type": "journey", "id": "wwn", "model": "terrain_days",
 "day_hours": 10,
 "terrain_speeds": {"plains": 3.0, "light_forest": 2.0, "dense_forest": 1.5},
 "road_multiplier": 2.0,
 "weather_factors": {"clear": 1.0, "foul": 0.5, "deep_snow": 0.25},
 "forage": {"plains": 6, "forest": 8}}
```

### D. Test contract recommendations

Add before refactoring:

1. `tests/contracts.py` — one core-tool set, one state-step set, one `KEPT_PYTHON` set.
2. `tests/ruleset_contract.py` — parametrize over all rulesets: every tool schema resolves, every `enum_from` resolves, every `world_method` exists, every referenced stat/feature/template id exists.
3. `tests/content_ids.json` — golden sorted id sets per ruleset; update deliberately.
4. Export/import matrix with one event per subsystem.
5. Unknown-payload-key tolerance test.
6. Missing-ruleset import/load test.
7. Fixture/shipped id disjointness test.
8. Replace exact property-set tests with “schema accepts every property” + one explicit unknown-key rejection.

---

## 12. Bottom line

Cygnus Loom’s engine is already a good generic core. The refactor should **extend the existing data-driven architecture**, not replace it:

- add capabilities and defaults/overlays;
- unify effects, feature types, and item definitions;
- move magic/rest/journey/progression behavior into data + hooks/adapters;
- keep ids and event payloads stable;
- fix the handful of correctness issues first;
- freeze behavior with contract tests and golden content ids before moving anything.

The highest-value single change is the **defaults/overlay layer**; the highest-value correctness fix is the **WWN rest/time model**; the highest-value correctness guard is a **single source of truth for XP/class costs**.
