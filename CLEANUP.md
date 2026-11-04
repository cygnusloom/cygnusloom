# CLEANUP — engine consolidation for 2.0.x

Status: **in progress**
Version: remains `2.0.0`; breaking internal changes are allowed.

This is the working checklist for the architecture cleanup. It turns the audit findings into
concrete work. Mark items done as they land. Keep the "Definition of done" honest.

## Status snapshots

**Landed in this pass:** strict JSON schemas; direct tool binding with zero ruleset wrappers;
one roll-resolution, recovery, and condition-lifecycle primitive; prompt/view extraction; hook,
effect, attack-flag, shop-effect, morale-trigger, rest-trigger, and enum-source registries;
visibility metadata; `RuleSet` reduced from 44 flat fields to 18 with content/progression/time/
item capabilities; neutral contract ruleset and shared test factories; WWN partial-class
pairing, Effort eligibility, Readied/Stowed pools, armor encumbrance, structured bestiary
multi-attacks, and PC Morale correction.

**Still open:** `campaigns.py` orchestrator split; generic event payloads; splitting
`events.py`, `world.py` further, and `combat.py`; `CombatRules` capability grouping; the
`test_scars.py` private-loader migration; WWN shield-bash AC state, combat actions, structured
monster abilities, and structured Focus benefit choices; source-accurate project/magic-item
costs.

## Definition of done

- A ruleset that reuses existing mechanics can be added with JSON only.
- A new mechanic touches one capability module, its loader/schema, and its tests — not every
  service, `WorldTools`, and `RuleSet`.
- No ruleset ships one-line `ctx.world.*` tool wrappers.
- Unknown or misspelled JSON keys fail at load time.
- Parity with each source book is a checked list, not prose.
- The full suite passes in parallel with no serial-only tests.

## Phase 0 — safety and truth (P0)

- [x] **Strict JSON schemas.** Added shared `check_keys`/`check_object_keys` helpers and applied
  them across the definition loaders. Unknown keys fail with the definition label and key.
- [x] **Fix silent config drops.** `_load_combat` now loads the whole `defeat` map generically;
  `important_npc` works, with regression tests.
- [x] **Reconcile parity docs.** WWN `SPEC.md`, `ISSUE_TRACKER.md`, and the new `PARITY.md` now
  separate implemented, partial, and open items with evidence.
- [x] **Parity checklist.** `worldswithoutnumber/PARITY.md` plus `tests/test_parity_docs.py` pin
  the documented status.

## Phase 1 — seams and primitives

### 1.1 Direct tool binding

- [x] Add `"world_method"` bindings and validate the method and schema in the loader.
- [x] Migrate core, Cairn, and WWN tool JSON off the straightforward one-line wrappers:
  121 tool calls now bind directly. `rules/core.py` 358→181, Cairn 479→269, WWN 1452→1226.
- [x] Removed the last 12 wrappers through declarative `bind`/`rename` tool metadata and method
  defaults. `rules/` now has no `ctx.world.*` tool wrappers.
- [x] Add loader tests that refuse an unknown method and a schema mismatch.

Acceptance: `rules/` contains no one-line `ctx.world.*` wrappers; tool count and behavior are
unchanged; the WWN/Cairn tool tests still pass.

### 1.2 One roll-resolution primitive

- [x] Added `services/resolution.py` with immutable results and one entry point for
  saves/checks/morale/initiative/retreat.
- [x] Migrated `combat._save`, `combat.player_save`, `injuries._save`, `magic.roll_save`,
  `morale.check_morale`, `checks.py`, and the retreat/initiative paths.
- [x] Kept event types stable while routing every result through the primitive.

Acceptance: one implementation of roll-vs-score; all ruleset save/morale/check tests pass.

### 1.3 One recovery primitive

- [x] Added `services/recovery.py`; combat healing, rests, marketplace treatment, and
  `restore_stat` all use it.
- [x] Collapsed the three recovery events into `Recovered`; the Log renderer and tests use it.
- [x] Added tests for over-healing, zero, and restoring different stats.

### 1.4 One condition lifecycle

- [x] Every condition end goes through `conditions.end_condition`, which validates the trigger.
- [x] Rest trigger names now live in each `rest.json` (`ends`), not hardcoded `rest` /
  `extended_rest`.
- [x] Combat, rests, injuries/effects, and tests updated.

### 1.5 Prompt and view extraction

- [x] Moved prompt constants and builders into `services/prompts.py`; `campaigns.py` dropped
  from 2,051 to ~1,756 lines.
- [x] Moved world/statblock/status/map text into `services/views.py`; `world.py` dropped from
  4,463 to 3,717 lines.
- [ ] Keep `campaigns.py` as the turn orchestrator only (prompt text is out; context/turn code
  still lives there).

## Phase 2 — extension model

- [x] **Hook registry.** `Hooks` now carries a name→function map; new hook names need no core
  edit. Cairn's existing hooks are unchanged.
- [x] **Effect registry.** `services/effects.py` now holds the op validators/executors; unknown
  ops still fail at load time.
- [x] **Registered vocabularies.** Attack flags use descriptor behaviors, shop effects use
  handler functions, morale triggers are configured per rule set, rest triggers already come
  from each rest definition, and enum sources are a named value mapping.
- [ ] **Generic events.** Introduce generic payloads (`RollMade`, `StatChanged`,
  `ResourceChanged`, `ConditionChanged`) and migrate new or low-risk features first. Recovery
  is consolidated; the rest remain.
- [x] **Visibility metadata.** Hidden/visible policy is a class attribute; `HIDDEN_KINDS` is gone.

## Phase 3 — module shape

- [ ] Split `services/events.py` into state models, registry/persistence, prose rendering, and
  visibility.
- [x] Split `services/world.py`: views moved to `services/views.py`; mutation/query facade remains.
- [ ] Split `services/world.py` further by aggregate (places, people, travel) and
  `services/combat.py` into defense, attack, damage, and defeat/dying.
- [ ] Replace the union-like parts of `RuleSet`/`CombatRules` with optional capability objects
  (`combat`, `inventory`, `magic`, `progression`, `time`, `social`, `encounter`). Each capability
  owns its schema, loader, runtime, and tools.
  Progress: `RuleSet` dropped from 44 flat fields to 18, with `content`, `progression`, `time`,
  and `items` capability objects and read-only aliases for existing consumers. `CombatRules` is
  still a 40-field bag.
- [x] Remove dead wrapper code. All tool wrappers are gone; tools bind `WorldTools`
  operations directly or use declarative bind/rename metadata.

## Phase 4 — tests and parity

- [x] Turned `rolltest` into the neutral contract ruleset (stats, combat, checks, conditions,
  counters, features, templates) with `tests/test_contract_ruleset.py`.
- [x] Added shared `tests/factories.py` for campaign/character setup.
- [ ] Migrate existing core tests off `tests.test_combat` private helpers where practical.
  Progress: encounters, conditions, defense rules, and ruleset runtime tables now use the
  neutral fixture; `test_scars` still needs a non-broken migration.
- [ ] Stop importing private `_load_*` loader internals in most tests; prefer public load results.
  `test_scars` is the main remaining loader-internal test.
- [x] Suite stays parallel and under one minute (2,567 tests in ~37-41s).

### WWN parity backlog (track, then close)

- [x] Source-valid partial-class pairings, including Healer and Vowed.
- [x] Separate Readied and Stowed capacity. WWN uses a 0.5 x STR Readied pool and a 1 x STR
  Stowed pool; items join by size/default, equipment joins Readied, a bulky item cannot be
  readied, over-capacity is refused, and a stowed item cannot be used
  (`tests/test_inventory.py`, `tests/test_inventory_schema.py`, `tests/test_wwn_inventory.py`).
- [x] Armor-encumbrance penalties to Sneak/Exert. Armor features and shop items carry
  `encumbrance_checks`; `roll_check` subtracts the worn item's Encumbrance slots and records the
  penalty on the check event (`tests/test_wwn_combat.py`).
- [ ] Shield AC suspended after a shield bash until the next turn.
- [x] Effort stats only for eligible characters. Each Effort stat names its granting class or
  tradition templates in `requires_templates`; a Warrior has none and a Mage keeps only the
  chosen tradition's pool (`tests/test_wwn_effort.py`).
- [ ] Surprise, grappling, execution attacks, total defense, dual wielding, range bands.
- [x] Magic Workings, spell research, hirelings, projects, faction turns. Verified: the generic
  project tracker (`start_project`/`advance_project`/`abandon_project`/`list_projects`) and
  hireling tools are wired into WWN. Specific source costs and magic-item creation remain in
  `ISSUE_TRACKER.md` rather than being claimed as complete parity.
- [x] Structured bestiary multi-attacks instead of prose parsing. `attacks_per_action`
  is a validated field on statblocks, the WWN bestiary carries explicit counts, and the loader
  rejects prose that still says "N attacks per action".
- [ ] Machine-readable monster special abilities.
- [x] PC Morale/Instinct aligned with the source. WWN PCs no longer carry or display a
  Morale score; NPC and monster morale checks still work
  (`tests/test_wwn.py::test_a_wwn_player_character_sheet_has_no_morale_but_a_statblock_does`).
- [ ] Structured Developed Attribute, Specialist, and Spirit Familiar choices.

## Verification

Run after each wave:

```sh
uv run ruff check src tests
uv run pytest -n auto -q -p no:cacheprovider
```

UI/live check at the end of the cleanup:

- Fresh database.
- Create a campaign for each shipped ruleset.
- Create a character, run one scene, one encounter turn, one save/check, and one recovery.
- Confirm the Log, journal, and tool results agree.

**Last verification:** ruff clean; `2567 passed in 31.62s` with `-n auto`; live server on a fresh
throwaway SQLite database served the development login, listed Cairn 2e / Hogwarts RPG / Worlds
Without Number, and created a WWN Latter Earth campaign and character through the real form. The
remaining live steps (encounter turn, save/check, recovery) are covered by the automated suite.
