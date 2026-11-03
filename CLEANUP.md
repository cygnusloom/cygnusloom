# CLEANUP — engine consolidation for 2.0.x

Status: **in progress**
Version: remains `2.0.0`; breaking internal changes are allowed.

This is the working checklist for the architecture cleanup. It turns the audit findings into
concrete work. Mark items done as they land. Keep the "Definition of done" honest.

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
- [ ] Delete the remaining wrappers. 27 remain because they are context-aware or their JSON
  parameter names/signatures do not match the `WorldTools` method (15 core, 8 Cairn, 4 WWN).
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
- [ ] Move world/statblock/status text out of `WorldTools` into a view module.
- [ ] Keep `campaigns.py` as the turn orchestrator only (prompt text is out; context/turn code
  still lives there).

## Phase 2 — extension model

- [ ] **Hook registry.** Replace the six-field `Hooks` dataclass and `HOOK_FIELDS` tuple with a
  name → function map, so a ruleset can add a hook point without editing core. Keep the existing
  Cairn hooks working.
- [ ] **Effect registry.** Replace `EFFECT_OPS`/`FEATURE_EFFECT_OPS` tuples plus the big
  validation/execution switches with registered validators/executors. Unknown ops still fail at
  load time.
- [ ] **Registered vocabularies.** Attack flags, shop effects, morale triggers, rest triggers,
  and enum sources should be registered handlers/config, not closed tuples.
- [ ] **Generic events.** Introduce generic payloads (`RollMade`, `StatChanged`,
  `ResourceChanged`, `ConditionChanged`) and migrate new or low-risk features first. Recovery
  is consolidated; the rest remain.
- [x] **Visibility metadata.** Hidden/visible policy is a class attribute; `HIDDEN_KINDS` is gone.

## Phase 3 — module shape

- [ ] Split `services/events.py` into state models, registry/persistence, prose rendering, and
  visibility.
- [ ] Split `services/world.py` by aggregate: places, people/NPCs, travel, views, and a thin
  `WorldTools` facade.
- [ ] Split `services/combat.py` into defense, attack, damage, and defeat/dying.
- [ ] Replace the union-like parts of `RuleSet`/`CombatRules` with optional capability objects
  (`combat`, `inventory`, `magic`, `progression`, `time`, `social`, `encounter`). Each capability
  owns its schema, loader, runtime, and tools.
- [ ] Remove dead wrapper code (`end_npc_turn` adapters and any orphaned functions).

## Phase 4 — tests and parity

- [x] Turned `rolltest` into the neutral contract ruleset (stats, combat, checks, conditions,
  counters, features, templates) with `tests/test_contract_ruleset.py`.
- [x] Added shared `tests/factories.py` for campaign/character setup.
- [ ] Migrate existing core tests off `tests.test_combat` private helpers where practical.
- [ ] Stop importing private `_load_*` loader internals in most tests; prefer public load results.
- [x] Suite stays parallel and under one minute (2,461 tests in ~51s).

### WWN parity backlog (track, then close)

- [ ] Source-valid partial-class pairings, including Healer and Vowed.
- [ ] Separate Readied and Stowed capacity; armor-encumbrance penalties to Sneak/Exert.
- [ ] Shield AC suspended after a shield bash until the next turn.
- [ ] Effort stats only for eligible characters.
- [ ] Surprise, grappling, execution attacks, total defense, dual wielding, range bands.
- [ ] Magic Workings, spell research, magic item creation, hirelings, projects, faction turns
  (verify what is actually missing before implementing).
- [ ] Structured bestiary multi-attacks instead of prose parsing.
- [ ] Machine-readable monster special abilities.
- [ ] PC Morale/Instinct aligned with the source.
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
