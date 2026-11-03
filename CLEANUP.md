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

- [ ] **Strict JSON schemas.** Add a shared `check_keys`/`check_object` helper and apply it to
  every definition loader. Unknown keys must raise `ContentError` with the label and key.
  Keep deliberate escape hatches explicit (for example `metadata`) rather than silent.
- [ ] **Fix silent config drops.** `worldswithoutnumber/combat.json` sets `important_npc`, but
  `_load_combat` only reads a fixed trio. Load the whole `defeat` map generically. Add a WWN
  regression test proving an important NPC is mortally wounded, not dead.
- [ ] **Reconcile parity docs.** Update `worldswithoutnumber/SPEC.md` and `ISSUE_TRACKER.md` so
  every listed simplification is either real and tracked, or removed. Projects, hirelings, and
  faction-turn tracking are already exposed in `adventure_tools.json`.
- [ ] **Parity checklist.** Add a structured checklist/test per ruleset so docs cannot drift from
  the engine again.

## Phase 1 — seams and primitives

### 1.1 Direct tool binding

- [ ] Add a tool binding that points at an engine operation, for example
  `"world_method": "create_location"` or a fully qualified `"handler"`.
- [ ] Resolve bindings in the loader, validate the method exists, and check the JSON schema
  against its signature.
- [ ] Migrate core, Cairn, and WWN tool JSON off one-line wrapper functions.
- [ ] Delete the world-adapter functions from `rules/core.py`, `rules/cairn2e.py`, and
  `rules/wwn.py`. Keep `speak`, `ooc`, lookups, and genuinely ruleset-specific algorithms.
- [ ] Add a loader test that refuses a binding to an unknown method.

Acceptance: `rules/` contains no one-line `ctx.world.*` wrappers; tool count and behavior are
unchanged; the WWN/Cairn tool tests still pass.

### 1.2 One roll-resolution primitive

- [ ] Add `services/resolution.py` with immutable results and one entry point for
  saves/checks/morale/initiative/retreat. It owns dice, stat comparison, `roll_under`/`roll_over`,
  automatic success/failure, modifiers, skill levels, and difficulty bounds.
- [ ] Migrate `combat._save`, `combat.player_save`, `injuries._save`, `magic.roll_save`,
  `morale.check_morale`, `checks.py`, and the retreat roll.
- [ ] Keep the existing event types for now if UI/tests depend on them, but compute every result
  through the primitive.

Acceptance: one implementation of roll-vs-score; all ruleset save/morale/check tests pass.

### 1.3 One recovery primitive

- [ ] Add a clamped harm-recovery helper used by combat healing, rests, marketplace treatment,
  and `restore_stat`.
- [ ] Collapse `CharacterHealed`, `StatRestored`, and `RestRecovered` into one recovery event
  (or one event plus a small reason payload); update the Log renderer and tests.
- [ ] Add tests for over-healing, zero, and restoring different stats.

### 1.4 One condition lifecycle

- [ ] Make every condition end go through one function that validates the trigger against the
  condition's `ends` list.
- [ ] Move rest triggers into the rest definition's JSON instead of hardcoded
  `rest`/`extended_rest`.
- [ ] Update combat, rests, injuries/effects, and tests.

### 1.5 Prompt and view extraction

- [ ] Move the generic prompt constants and builders out of `services/campaigns.py` into
  `services/prompts.py`.
- [ ] Move world/statblock/status text out of `WorldTools` into a view module if the split is
  mechanical and test-covered.
- [ ] Keep `campaigns.py` as the turn orchestrator only.

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
  `ResourceChanged`, `ConditionChanged`) and migrate new or low-risk features first. Keep
  specialized events only where the Log/UI needs them.
- [ ] **Visibility metadata.** Put hidden/visible policy on the event class, not a central
  `HIDDEN_KINDS` list that must be updated by hand.

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

- [ ] Turn `rolltest` into the neutral contract ruleset: it must exercise generic saves, checks,
  effects, recovery, conditions, and tool binding without Cairn/WWN content.
- [ ] Add shared `tests/factories.py` for campaign/character setup. Migrate core tests off
  `tests.test_combat` private helpers where practical.
- [ ] Stop importing private `_load_*` loader internals in most tests; prefer public load results.
- [ ] Keep the suite parallel, under roughly one minute.

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
