# Token and context optimization audit

**Branch:** `4.0-ruleset-refactor`  
**Date:** 2026-11-11

This audit measures where a Cygnus Loom turn spends tokens, separates the
cheap wins from the risky ones, and chooses an implementation order. The goal
is lower token cost and latency without losing the game world's behavior or the
source material's rules.

## Implemented in this pass

- `ContextUsage` now counts the fixed prompt, the tool definitions, and the
  chat separately. The composer meter shows the real state-step estimate and
  its title gives the split.
- `wire_tool_specs` provides four wire modes: `full`, `compact`, `summary`,
  and `brief`. The engine keeps the authored schema for validation and errors;
  the wire copy collapses the repeated `character` object, drops numeric
  bounds and long choice lists, and in `brief` mode shortens tool descriptions
  and omits parameter descriptions.
- `system_parts` provides three rule modes: `full`, `brief`, and `index`. The
  index lists rule ids and tells the GM to read a rule with `lookup_rule`
  before relying on its wording or numbers.
- The defaults are `brief` tools and `brief` rules. `CYGNUSLOOM_TOOL_SCHEMA_MODE`
  and `CYGNUSLOOM_RULE_INFO_MODE` select another mode for comparison.

Live browser validation used a fresh Cairn/Twin Lakes campaign and a
long-context imported campaign (353 events, 80 seeded turns):

- With the old compact default, a fresh campaign meter read **32,435 / 32,768**
  (7,682 fixed, 24,416 tools, 337 messages). With the brief default it read
  **17,038 / 32,768** (7,676 fixed, 9,023 tools, 339 messages), a 63% cut in
  tool tokens and enough headroom for summary history.
- The long-context campaign meter read **40,565** before the turn. Compression
  reduced it to **21,313**, and the answer still recalled Mara's brass lantern,
  the Red Hand's third-day collector, Coll's forty-silver debt, and the armor
  rule.
- The same UI played a social turn, an out-of-character critical-damage rules
  question, and a combat turn: the encounter started, initiative was rolled,
  the hammer dealt 3 harm, and the narration matched the recorded result.
- With `CYGNUSLOOM_RULE_INFO_MODE=index`, the UI meter read **19,451** and the
  critical-damage answer remained source-accurate; the GM used the rule index
  and lookup path rather than inventing a number.
- The full automated suite passed: **3,238 tests**. Formatting and lint are
  clean.

## Decision

Make **tool schemas the primary target**. In a normal turn, 67–81% of the
input is the JSON describing the tools, not the story, rules, or history. The
current context meter hides this: it counts only system prompt sections and
chat messages, so it can show 4–5k tokens while the actual state request is
29–56k. A two-round Worlds Without Number turn measured about **247k tokens**
against a configured 32k context budget.

The order is:

1. Measure the real request, including tools and step instructions.
2. Compact the tool schemas: remove duplicated character schemas and strip
   nonessential nested metadata. This alone cuts tool tokens by roughly half.
3. Gate tools by phase and scene, with a small `load_tool` fallback, and give
   the review step only the tools it may use to correct the record.
4. Gate ruleset prompt fragments and trim repeated instruction text.
5. Make review conditional and add deterministic pre-checks.
6. Only then retune summaries and history budgets.

Do **not** start by compressing history. It is not the dominant cost, and
summarizing a campaign harder while every tool call still carries 21–47k
tokens of schema would trade continuity for very little savings.

## How the measurements were taken

A temporary test built a fresh campaign for each of the three shipped pairings,
recorded every provider call, and measured compact JSON for messages and tools.
Token counts below use the project's four-characters-per-token estimate; they
are close enough for relative comparison, not a substitute for provider usage.

- `academia-arcana` / `unwritten-academy`: 77 tools.
- `cairn2e` / `twin-lakes`: 104 tools.
- `worldswithoutnumber` / `unwritten-world`: 137 tools.

A normal turn with no state-changing tool round makes three model calls:
state, narration, and journal-plus-review. A real turn usually makes at least
one tool round first, and each tool round adds another state call with the full
tool list.

## Where the tokens go

### Whole turns

| Pairing | Tools | State call tools | Review call tools | Normal turn | One tool round | Two tool rounds |
|---|---:|---:|---:|---:|---:|---:|
| Academia Arcana | 77 | ~21k | ~21k | ~64k | ~94k | not measured |
| Cairn 2e | 104 | ~31k | ~31k | ~81k | ~119k | not measured |
| Worlds Without Number | 137 | ~47k | ~47k | ~116k | ~172k | **~247k measured** |

The "one tool round" column is an estimate: one more state call with the same
tool schemas and a larger message history, roughly 30k for AA, 38k for Cairn,
and 56k for WWN. The two-round WWN figure is measured. Tool schemas are 67% of
the AA turn, 77% of Cairn, and 81% of WWN.

This is also a context-overflow risk, not just a cost problem. With the default
`CYGNUSLOOM_LLM_CONTEXT_TOKENS=32768`, a two-round WWN turn is far beyond the
configured budget. Whether it succeeds depends on the provider's real window,
not on the app's budget.

### Tool schema composition

| Pairing | Compact tool chars | Tool tokens | Descriptions | Parameters | Enum values |
|---|---:|---:|---:|---:|---:|
| Academia Arcana | 82.5k | ~20.6k | ~6.7k | ~12.9k | 570 |
| Cairn 2e | 120.6k | ~30.2k | ~9.7k | ~19.0k | 334 |
| Worlds Without Number | 183.9k | ~46.0k | ~10.9k | ~33.2k | 2,391 |

The largest single waste is the repeated `character` object:

- AA: two 6,003-character copies (`create_npc`, `update_npc`).
- Cairn: three 7,910-character copies (`create_npc`, `update_npc`,
  `hire_hireling`).
- WWN: three 24,250-character copies. Two copies beyond the first are
  **48,500 characters, about 12k tokens**, sent on every state and review call.

The same `character` schema contains long enum lists for backgrounds, skills,
foci, spells, arts, and other options. The engine already validates those ids,
so the model does not need the full lists in every schema.

A simulated compact wire format — keeping tool names, top-level descriptions,
types, and required fields, but dropping deep nested descriptions, numeric
ranges, and long enums, and collapsing the repeated `character` object — cut
tool tokens by:

- AA: ~47%
- Cairn: ~49%
- WWN: ~60%

That is before any phase gating.

### Fixed prompt cost

| Component | Tokens |
|---|---:|
| Base system prompt | 191 |
| State instruction, before ruleset fragments | 1,461 |
| State instruction with fragments | 3.2k Cairn, 3.8k AA, 4.3k WWN |
| Narration instruction with fragments | 1.9k Cairn, 2.7k AA, 2.0k WWN |
| Turn review instruction | 621 |
| Journal instruction | 340 |
| Narration revision note | 116 |
| Rules list with briefs, actual turn | 3.2k Cairn, 3.7k AA, 4.3k WWN |
| World state in the test fixtures | 160–200 |

The ruleset `state` and `all` prompt fragments are 1.8–2.8k tokens on every
state call. Most of that text is only relevant when the scene involves combat,
magic, maps, travel, advancement, or factions. The `local_maps` fragment alone
is about 450 tokens and is currently sent on every turn.

The summary can take up to 25% of the window, about 8k tokens, and the newest
10% of history, about 3.2k tokens, is kept verbatim. Those are reasonable caps
for a long campaign, but they are secondary until the tool surface is fixed.

### Why the current meter is misleading

`context_usage` in `services/campaigns.py` sums:

- the `system_parts` sections (base prompt, rules list, world state, setting,
  present rules, summary); and
- the unsummarized chat messages.

It does not count:

- tool definitions, which are sent in the provider's separate `tools` field;
- the state instruction appended after the context is assembled;
- the narration, review, journal, speech, opening, or out-of-character
  instructions;
- the assistant/tool-result messages added by earlier rounds of the same turn.

`turns._begin` uses the same incomplete count for the compression trigger at
50% of the context window. In the measured WWN turn, the UI's formula sees
roughly 4–5k tokens while the state request is about 56k. The app therefore
believes it has plenty of room while the request is already several times the
configured budget. Adding tools to the trigger without shrinking them would
make every turn summarize history needlessly, so tool reduction must come
first.

The provider already reports exact usage through `TokenUsage`
(`input_tokens`, `cached_tokens`, `output_tokens`, `reasoning_tokens`), and the
credits ledger records it. That is the right source for a real meter.

## Brainstorm: the full option space

### A. Tool surface

1. **Deduplicate shared schemas.** Define one character-options shape and use
   it once, or replace it with a compact object validated by the engine.
2. **Strip wire-only metadata.** Nested descriptions, `minimum`/`maximum`, and
   long `enum` lists are guidance, not enforcement. The engine still validates.
3. **Move guidance into errors.** Keep one-line tool descriptions; return a
   precise error when an argument is invalid. The model pays for the rule only
   when it needs it.
4. **Merge near-duplicate tools.** `create_npc`/`update_npc`/`hire_hireling`,
   `create_location`/`update_location`, `add_condition`/`end_condition`,
   `give_item`/`take_object`/`drop_item`, and similar pairs can become one tool
   with a mode or action. This is a larger behavioral change, not the first
   step.
5. **Phase-gated toolsets.** Keep a small core always available; add groups for
   combat, maps/travel, magic, advancement, factions/projects, and inventory
   only when the scene or action needs them.
6. **`load_tool` fallback.** If the model needs a gated tool, it calls a tiny
   meta-tool; the next round includes that tool. This preserves capability
   without paying for all 137 schemas every round.
7. **Narrow review toolset.** The review only needs `undo_narration` plus the
   tools that can reverse or correct a recorded action. It does not need the
   full state-step surface.
8. **Lazy schema expansion.** Send names and one-line descriptions in a compact
   catalog; load full schemas on demand.
9. **Generic dispatcher.** One `run_tool` with a name and JSON arguments would
   be tiny, but it loses argument guidance and encourages invalid calls.
   Defer unless gated tools prove insufficient.
10. **`$ref`/`$defs` spike.** If the provider supports JSON Schema references,
    the shared character object can be defined once. Do not depend on it
    without a provider test.
11. **Tool-result compaction.** Lookup results and tool outputs are re-sent in
    every later round of the turn. Bound them, trim fields the model does not
    need, and use targeted lookups instead of full dumps.

### B. Prompt and context

12. **Gate ruleset fragments.** Include combat fragments during encounters,
    map fragments when mapping, magic fragments when casting or preparing, and
    so on. Keep a short index so the model knows what exists; load full text
    with `lookup_rule` or a procedure tool.
13. **Trim instruction duplication.** State, narration, and review repeat some
    anti-invention and coverage guidance. Keep one policy per step; remove
    examples and wording that do not change behavior.
14. **Compact narration brief.** The narration call needs the turn log, queued
    dialogue, scene, PC, and relevant rules; it does not need every GM-only
    record. A structured beat sheet could replace some prose context.
15. **Scene-local world state.** The current world state is already mostly
    local to the player character. Keep it that way; load factions, maps, and
    distant places lazily in long campaigns.
16. **Rules index instead of all briefs.** Show a compact rule-id/category
    index plus the rules attached to present places, NPCs, and items; let
    `lookup_rule` fetch the rest.
17. **Structured summary.** Summarize as facts (places, people, promises,
    debts, open threads, recent decisions) with a hard token cap, and avoid
    repeating what the world state already records.
18. **Split budgets.** Compute a fixed-cost budget (system + step instruction +
    tools) and a separate history budget. Compress history against its own
    budget; never summarize history to compensate for oversized tools.
19. **Prompt caching.** Keep the stable base prompt, rules list, and core tool
    schemas in a stable prefix. Measure `cached_tokens` before and after;
    DeepSeek-compatible providers can price cached input much lower.
20. **Output caps.** Set explicit output limits for narration, review, and
    journal steps. Output is smaller than input here, but it affects latency
    and runaway reviews.
21. **Telegraphic prompt style.** Dropping articles and connective words can
    save tokens, but it risks comprehension and narration quality. Reject for
    player-facing instructions.

### C. Call graph and models

22. **Conditional review.** If the state step changed nothing and the
    narration passes deterministic checks (coverage, tense, formatting,
    queued-dialogue matching), skip the review call.
23. **Deterministic pre-checks.** Check log-line coverage, forbidden markdown,
    invented dialogue when none is queued, and past-tense slips before asking a
    model to review.
24. **Cap state rounds.** The state loop currently has no explicit round cap.
    Add a small cap with a clear recovery path; every extra round costs another
    full schema plus growing results.
25. **Trim retry budgets.** `NARRATION_ATTEMPTS=2`, `NARRATION_REVIEWS=2`, and
    `REVIEW_ROUNDS=3` are reasonable safety nets, but each retry is expensive.
    Measure fault rates before lowering them.
26. **Cheaper reviewer.** Use a smaller model for the review or journal step,
    or a deterministic checker plus a model only for uncertain cases.
27. **Journal cadence.** The journal is already combined with review, so it
    adds little; updating it only on day boundaries or major events could save
    the journal instruction and tool round on routine turns.
28. **Two-stage planner.** A cheap deterministic or small-model router selects
    tool and fragment groups from the player message and state, then the main
    call gets only that loadout. A deterministic selector should come first.
29. **Fact ledger / state deltas.** Send a compact delta since the last turn
    plus a scene digest instead of a full state snapshot. This is a larger
    architecture change and should wait for the simpler wins.

### D. Deferred or rejected

- **Compress history first.** Low impact while tools dominate, and it hurts
  continuity.
- **Remove the review entirely.** It protects fidelity; make it conditional or
  narrower instead.
- **Generic tool dispatcher now.** High risk of invalid calls and weaker tool
  selection.
- **Aggressively shorten narration policy.** This is where source fidelity
  lives; trim wording only with golden-scenario tests.
- **Smaller state model.** State decisions need the most reasoning; test only
  after the schema and prompt work is done.

## Chosen plan

### P0 — Make the budget honest

Add a per-step cost helper that counts:

- system prompt sections;
- the step instruction;
- compact tool-schema JSON;
- history and player message;
- the assistant/tool messages already added in this turn.

Use it for the debug/context display and for a new fixed-versus-history
budget. Record the provider's exact `input_tokens` and `cached_tokens` per call
so the UI can show real usage for the last turn. Do not change compression
behavior in this step beyond reporting.

**Effort:** small. **Risk:** low. **Savings:** none directly, but it makes
every later change measurable.

### P1 — Compact the wire schemas

Introduce a wire-schema projection used only for the provider:

- keep tool name, top-level description, parameter types, required fields, and
  top-level parameter descriptions;
- drop deep nested descriptions, numeric ranges, and long enum lists;
- collapse the repeated `character` object to a small optional object validated
  by the engine;
- keep short enums that are cheap and materially improve tool use;
- make validation errors name the lookup tool that lists valid values.

This preserves the engine's full schemas and all mechanics. It changes only
what the model is shown.

**Measured target:** 47–60% fewer tool tokens with the simulated projection.
**Effort:** medium. **Risk:** low to medium; tool-call error rates must be
watched.

### P1 — Gate the tool surface

Define tool groups as data, not Python branches:

- **core:** movement, locations, NPCs, objects, notes, storylines, lookups,
  speak/ooc, rolls, inventory basics;
- **combat:** encounter, initiative, attacks, saves, conditions, morale;
- **maps/travel:** map creation and editing, travel, weather, regions;
- **magic:** casting, preparation, effort, spells, arts, items;
- **advancement:** XP, level-up, projects, training;
- **factions:** faction turns, assets, projects;
- **market:** buy, sell, services, currency.

The state step gets core plus the groups selected from the current state and
player action. A tiny `load_tool` meta-tool covers an unexpected need. The
review step gets `undo_narration` plus a correction subset, and journal tools
only in the combined journal-plus-review step.

**Expected effect:** the WWN state and review calls go from ~47k tool tokens to
roughly 7–12k; AA and Cairn fall proportionally. This is the largest single
reduce-cost change.

**Effort:** medium to large. **Risk:** medium; the fallback and group tests are
essential.

### P2 — Gate prompt fragments and trim instructions

- Select `state`/`all` fragments by scene and action, with a short index of the
  rest.
- Include only rules attached to present places, NPCs, and items, plus a
  compact rule index; keep `lookup_rule`.
- Trim repeated wording in the state and narration instructions without
  removing a policy.
- Replace the narration's broad context with a compact beat sheet if testing
  shows it preserves coverage.

**Expected effect:** 1–3k tokens per call, with the largest gain on rules-heavy
turns.

**Effort:** medium. **Risk:** medium; run golden scenarios for combat, magic,
travel, maps, and advancement.

### P2 — Reduce review calls

- Add deterministic checks for log coverage, queued dialogue, tense, and
  formatting.
- Skip the review when the state step made no record change and the
  deterministic checks pass.
- Keep the full review for turns with state changes, rolls, combat, or
  dialogue.
- Cap state rounds and review rounds after measuring fault rates.

**Expected effect:** 20–40% of the cost of routine turns, plus lower latency.

**Effort:** medium. **Risk:** medium; start behind a setting and compare undo
and fault rates.

### P3 — Long-campaign history and summaries

- Replace the prose summary with a structured fact summary and a hard cap.
- Deduplicate summary facts against world state.
- Use a fixed-versus-history budget rather than a flat 50% trigger.
- Lower `SUMMARY_SHARE` only after long-campaign playtests.

**Effort:** medium. **Risk:** low to medium. **Savings:** 1–6k per call in long
campaigns, larger once tool and prompt costs are small.

## Expected outcome

These are planning estimates, not measured results:

| Stage | AA normal turn | Cairn normal turn | WWN normal turn | WWN two-round turn |
|---|---:|---:|---:|---:|
| Today | ~64k | ~81k | ~116k | ~247k |
| After compact schemas | ~42–45k | ~50–55k | ~60–70k | ~130–150k |
| After gated tools and narrow review | ~20–28k | ~25–35k | ~32–45k | ~60–90k |
| After prompt and call reductions | ~15–22k | ~20–28k | ~25–35k | ~45–70k |

A normal turn at 15–35k tokens would be a 60–75% reduction, and the worst
two-round WWN turn would fall by roughly 70–80%. Prompt caching can reduce the
effective price further, especially if the core prefix stays stable.

## Fidelity guardrails

- Keep every tool name, argument contract, engine check, event, and ruleset
  mechanic. Compact only the wire presentation.
- Keep the ruleset content and fragments; gating selects a view, never deletes
  the source. Provide `lookup_rule`, `character_options`, `lookup_feature`, and
  `load_tool` fallbacks.
- Keep the narration and review policies. Trim wording, not coverage, tense, or
  player-agency rules.
- Validate with the full test suite, `cygnusloom content validate --strict`,
  the three-pairing live matrix, and deep playtests for combat, magic,
  advancement, travel, maps, factions, and long-campaign compression.
- Track invalid tool calls, review undo rates, narration coverage, and cache
  hit rates before and after each stage.

## First implementation slice

1. Add the real per-step cost helper and exact-usage display; add snapshot
   tests for each ruleset.
2. Add the wire-schema projection and collapse the repeated character schema;
   assert the new token budget per ruleset.
3. Add data-driven tool groups and the `load_tool` fallback; gate the state and
   review steps; test every group.
4. Gate prompt fragments and trim duplicated instruction text; run golden
   scenarios.
5. Pilot conditional review and deterministic checks behind a setting.
6. Retune summaries and the fixed-versus-history budget last.

## Status after this pass

Done: honest accounting, the wire-schema projection with four modes, the rule
detail modes, brief defaults, and browser validation of a fresh campaign, a
long-context campaign, rules lookup, and combat.

A second pass trimmed the map prompt to stop ordinary conversations from
creating maps and placed the review's read-only lookup catalog behind a small
verification set. The Cairn review tools fell from 104 to 85, with about 1.3k
fewer tool tokens per review call.

### Natural-play browser audit

A real DeepSeek Cairn/Twin Lakes campaign was played through the browser for
ten natural turns covering the market, the Butcher Shop, the Stuffed Inn, a
rest, an overland journey, the Deadmill, saves, monster creation, dungeon and
wilderness cycles, journal updates, storylines, notes, and speaking with NPCs.

- **Distinct state tools exercised:** 34 of the 104 Cairn tools.
- **Provider calls:** 103 (including state rounds, narration, journal, and
  review); **2.2M input tokens**, of which 1.51M were cached; 55.6k output.
- **Observed behavior:** locations and NPCs moved correctly, the tally slip and
  debt facts persisted, services charged currency, rest and weather advanced
  the clock, saves resolved, and the journal and storylines updated.
- **Observed gap:** a simple social turn no longer created a map, while a
  room search still made one, as intended. The session did not naturally reach
  an attack encounter, advancement, factions, or every ruleset-specific tool;
  those remain covered by the automated suite and the earlier browser combat
  test rather than this single playthrough.

The 103 calls for ten turns show why schema compaction alone is not enough.
State rounds and review rounds still repeat the tool list; phase-gated toolsets
and a narrower correction review remain the next high-value change.

Next, in order: gate the state tool surface, then gate ruleset fragments, then
pilot conditional review and a structured summary. The current default already
fits a fresh Cairn campaign comfortably; the remaining work is what keeps
Worlds Without Number and long campaigns comfortably inside the window as
history and rules grow.
