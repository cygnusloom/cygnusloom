# Hogwarts RPG ruleset and generic engine: spec and notes

This file is the working spec and log for the `hogwarts-rpg` ruleset and the engine refactor that
makes Cygnus Forge generic for any ruleset. It is kept here so the notes are not lost. The loader
reads only `*.json` files, so this file is not loaded as content.

Reference: `Hogwarts RPG Full Game.pdf` (Hogwarts: An RPG, v1.2) in this directory.

## Goal (from the user)

1. Implement the `hogwarts-rpg` ruleset from the PDF.
2. Refactor the engine so it is generic for any ruleset. Cairn-specific behaviour moves into
   `cairn2e`.
3. Test both `cairn2e` and `hogwarts-rpg`.
4. "Close the gaps": make the parts that were left as GM prose, or hardcoded, work through the
   engine.

## Decisions

- Scope: the full game, chosen by the user. Traits, 2d6 moves, Luck, Experience, Advancements,
  Conditions and the exit roll, Friends and Rivals, House Points, character creation, spells,
  potions, Quidditch, Narrator moves, and Serious Consequences.
- Engine style: "fat yet generic". Each ruleset's JSON sets as much as possible. Ruleset Python is
  an escape hatch. Hogwarts needs no Python.
- Cairn 2e behaviour must not change. Its tests were repointed only where they named a tool that
  moved to cairn2e (see Phase 2).
- Worlds Without Number has only its PDF, so it is out of scope. The loader skips ruleset
  directories that have no JSON, with a warning, so the app and tests still run.
- Morale and hirelings stay generic engine modules, enabled only when a ruleset's JSON turns them
  on. Needs (food, shelter, deprivation) were the Cairn-flavoured part, so they moved into cairn2e
  as hooks (see "Needs move"). The user chose this after "Any other gaps?".
- Friends and Rivals may only change at the end of a session, as the PDF says. The engine does not
  know when a session ends, so the `set_relation` tool description tells the GM to use it only then.
  This is GM-enforced.
- Favourite Subject is enforced: the Core/Elective split and the Year-based restriction are checked
  by the engine (see "Restrictions"). Spells are enforced too: blood status sets the pick count,
  and each pick must match the character's Year.

## Engine changes (generic primitives, set by JSON)

Phase 1 added these. Each is parameterized, not Cairn- or Hogwarts-specific.

- **Stats** may use `values`, a fixed pool of whole numbers shared by a `swap_group`. The stats in
  the group take the values in any order. A rolled `formula` still works. A stat's value may be
  `HIGHEST`, meaning the highest of the group (used by the exit roll).
- **Moves** (`type: move`): `dice`, then `stat` or `stat_choice` (the player picks a stat when
  rolling), then `bands` checked in order. A band has `label`, an optional `min`, `text`, and
  `effects`. The band with no `min` is the fallthrough. `needs_all_conditions` makes the move
  available only when the character holds every stored condition (the exit roll).
- **Counters** (`type: counter`): `min`, `max`, `initial` (defaults to `min`), and `full` effects.
  A counter the character lacks reads as its initial value.
- **Conditions** (`type: condition`): `ends` (triggers from the rule set's `condition_ends`, plus
  `gm`), `on_end` effects, `modifies` (stat id to amount, where `"*"` means all stats), `dying`
  and `fatal` flags, and `derived` (a condition the stats show, not one stored on the character).
- **Effect ops** (`EFFECT_OPS` in `content/library.py`): `note`, `set_max`, `condition`, `save`,
  `pick_stat`, `counter`, `end_condition`, `retire`, and `raise_stat`. `counter` and `end_condition`
  are move-only. `retire` records `CharacterRetired`, which the exit roll uses. `raise_stat` raises
  a trait's maximum by one, up to the ruleset's limit.
- **Template grants** (`templates.json`): `stats` (bonuses added to stats) and `counters` (amounts
  added to starting values), applied in `content/character.py` `resolve`.
- **`enum_from`** on a tool parameter: `"moves"`, `"stats"`, or a content list (such as
  `"relations"`) fills the enum from the rule set's own content.
- **`condition_ends`** on `rule_set.json` names the rule set's triggers. `ALWAYS_CONDITION_END`
  (`gm`) is always available. Cairn 2e uses `rest`, `extended_rest`, `aid`, and `time`.
- The loader skips a ruleset directory with no JSON and logs a warning.

Phase 2 genericized the coupling:

- Twelve Cairn-only `WorldTools` methods moved out of core into cairn2e. 38 tests that call them
  were repointed.
- Dying and fatal are condition flags, not hardcoded checks. The old global `CONDITION_ENDS` is
  now `ALWAYS_CONDITION_END` plus each rule set's `condition_ends`.
- Save and initiative are JSON-driven. `CombatRules.save_succeeds` and `initiative_order` read
  `combat.json`. A ruleset may still name a Python module to override them. Cairn 2e names none.
  The Cairn-specific save and initiative behaviour is tested by an oracle in `tests/test_combat.py`.
- `rules/cairn2e.py` contains only the Cairn tool wrappers.

Phase 3 (close the gaps) added:

- **Advancements** (`advancements.json`, `services/advancements.py`): the options a character can
  take, each with its effect, and a counter of Advancements earned. `take_advancement` spends one.
  Trait options use `raise_stat`. The other options are a second Favourite Subject, a spell from the
  character's Year, a magical object, and regaining Luck.
- **Periods** (`periods.json`, `services/periods.py`, `end_period` tool): a school year ends, and
  its effects run. Luck is regained, and an Advancement is earned. `period.ended` is recorded.
- **Tallies** (`tallies.json`, `services/tallies.py`, `adjust_tally` tool): named totals with
  entries, such as House Points by House. Each change is recorded as `tally.changed` with a cause.
  Tallies are totals only. They are not reset by period.
- **Relations** (`relations.json`, `services/relations.py`, `set_relation` tool): Friends (limit 2)
  and Rivals (limit 1), each a list of names. The engine rejects a duplicate, an over-limit add, and
  a removal of someone who is not a relation.
- **Restrictions** (`content/character.py`): a Favourite Subject group (Core or Elective), and a
  restriction on a field that depends on another field's value. Restricted fields resolve after
  unrestricted ones. Favourite Subject is Core only in 1st and 2nd Year. The creation form disables
  the options a restriction rules out, and clears a pick of one when its gate changes.
- **Spell picks** (`Template.choose`, `Feature.tag`, `check_choices`): a template can require
  `count` picks (1, 2, or 3 by blood status) from features whose `tag` matches a field's value
  (`tag_field`, here the Year). A pick must come from the chosen Year: spells from earlier Years
  are known already (`knows` on the Year field), so they are not picked. A pick with no Year is
  rejected, and so is a pick from any other Year. `resolve` keeps the given picks and rolls the
  rest from the chosen Year, skipping features the character already has. No spell is tagged
  Graduate, so a Graduate picks none.
- **Web creation form** (`web/character_form.py`, `creation.html`): spell picks are shown per
  template, grouped by Year, as `pick-<template>-<n>` selects. The options are filtered to the
  chosen Year (`creation.js`). The server rejects a pick from any other Year. Blank
  picks are rolled, and "Randomize spells" rolls the picks in the browser.
- Renamed event kinds (save compatibility is not needed): `CampFedSet` to `MealSet`,
  `LodgingSet` to `ShelterSet`, plus fields, marketplace effects, and prose. The needs move (below)
  later replaced `MealSet`, `ShelterSet`, and `NeedSet` with one generic `MarkSet`.

### Needs move

Cairn's needs logic left the engine and now runs through a `hooks` definition in JSON.

- **Hooks** (`services/hooks.py`): a JSON `hooks` definition names a `python_module` and functions.
  The engine calls only the functions it names. Cairn 2e names its functions in `cairn2e/need.json`.
  Hogwarts has no hooks, so none run there. The hook functions are in `rules/cairn2e.py`:
  - `new_day(hc, day)`: after each newly started day, a deprived character past `after_days`
    gains the penalty item.
  - `before_rest(hc, rest_id, start, hours_per_part)`: runs for a rest with `upkeep: true`. Feeds
    the party, or uses up a bed or a meal, and deprives anyone with no ration.
  - `held_back(hc, name) -> str`: the cause a character is held back by, or "". Used by rest
    recovery, `_clear` eligibility, the held-back terrain penalty, the `add_penalty` refusal, and
    status lines.
  - `after_consume(hc, item, used, short, at, day) -> str`: runs for a consume with `track: true`.
  - `lodge(hc, names)`: runs for a paid marketplace service whose `effect` is `"lodge"`. The engine no longer names shelter.
  - `status(hc, character) -> list[str]`: status lines.
- **Generic marks**: `CharacterState.marks` replaces `sheltered`, `meal_at`, `unmet_since`, and
  `unmet_cause`. `MarkSet(name, key, value, text)` sets or clears (`value` None) a mark. Cairn's
  marks are `deprived` ({since, cause}), `sheltered`, and `meal_at`.
- **Services**: `services/rests.py` (rests and rest history) replaces `services/needs.py`.
  `clock.advance_to` calls `new_day`, and `wilderness` and `market` call their hooks.
- **Renames in JSON**: `rest.json` `food` to `upkeep`, the `food` block to the hooks options
  (`feature`, resolved to `options.item`); `wilderness.json` `meal` to `track`;
  `deprived_terrain_steps` to `held_back_terrain_steps`.
- **Tools**: `set_condition` (Cairn) now lives in `rules/cairn2e.py` and records a `MarkSet`.
- **Tests**: 1704 pass. Cairn tests cover rests, feeding, shelter, meals, deprivation, the daily
  penalty, and `set_condition`. Hogwarts's hooks are `None`.
- **Regression fixed**: the moved `before_rest` hook fed and marked every character, NPCs
  included. The old code fed only PCs. Fixed, with a regression test
  (`test_a_night_rest_does_not_feed_or_deprive_an_npc`). Found in a live run.
- **Live check** (fresh DB, uvicorn on 8773, DeepSeek, Cairn 2e): a night rest ate one ration and
  left NPCs alone; a Room & Board purchase (`effect: "lodge"`, 10 gold) gave a bed, and the next
  rest used the bed and ate nothing. Deprivation's daily penalty is still unit-tested only.

## Hogwarts content (`src/cygnusforge/data/rulesets/hogwarts-rpg/`)

| File | Contents |
| --- | --- |
| `rule_set.json` | Name, description |
| `stats.json` | Five traits: Bravery, Cunning, Intellect, Loyalty, Magic. Values -1, 0, +1, +1, +2 in one swap group (`traits`), so any order |
| `counters.json` | Luck (0 to 3, starts at 3). Experience (0 to 4). At 4 Experience, a `full` effect clears it and notes the Advancement. Advancements (earned count) |
| `conditions.json` | Scared, Angry, Stressed, Jealous, Embarrassed (-2 to one trait each), Injured (-1 all), Hexed (-1 to a trait the Narrator picks), Unconscious (cannot act). Each has its ends and effects |
| `moves.json` | 14 moves: Stand in the Face of Danger, Help or Hinder, Hide & Sneak, Get What You Seek, Gain Knowledge, Approach a Magical Creature, Roll, Cast a Spell, Duel, Concoct a Potion, Use a Magical Object, Exit the Story, Play a Quidditch Match, Catch the Golden Snitch. A failed roll marks Experience |
| `tool_calls.json` | The GM's tools: `roll_move`, `adjust_counter` (Luck and Experience), `take_advancement`, `end_period`, `adjust_tally`, `set_relation` |
| `advancements.json` | The Advancement options (see Engine changes) |
| `periods.json` | The school year, with its end effects |
| `tallies.json` | House Points, by House |
| `relations.json` | Friend (limit 2) and Rival (limit 1) |
| `rules.json` | 12 rule entries the GM reads with `lookup_rule` (below) |
| `templates.json` | 4 houses (each +1 to one trait) and 3 blood statuses (spell count, starting Experience) |
| `tables.json` | Wand wood and ambition, Heritage, Favourite subject (with Core and Elective groups), Patronus, pet, look, and name tables |
| `character_fields.json` | The creation fields that use those tables, with restrictions |
| `spells.json` | 126 spell features, each with its Year as `tag`. Unforgivable Curses are excluded (below) |

These files were generated once from the PDF text by a script that is not kept in the repo. They
are now maintained by hand. Edit the JSON directly.

The world template `src/cygnusforge/data/worlds/hogwarts/` (locations and a start with hooks) gives
a campaign the Great Hall and the grounds.

### Rules the GM reads (`rules.json`)

The GM sees these by brief and reads the full text with `lookup_rule`. Each entry is a paraphrase
of the PDF. The key entries (Luck, Experience, Advancements, Friends and Rivals, Serious
Consequences, Unforgivable Curses) were spot-checked against the PDF text. The rest are not yet
checked line by line:

`narrator-moves`, `serious-consequences`, `house-points`, `luck`, `experience`, `advancements`,
`friends-and-rivals`, `exiting-the-story`, `unforgivable-curses`, `healing`, `pets`, `patronus`.

The `healing` entry carries the PDF's rules on a spell's effect matching the cause, defence spells,
and the hospital wing.

### Unforgivable Curses

The PDF says they cannot be learned at the start of play, by taking an Advancement, by completing a
school year, or from a class. When cast, the Narrator may play a Serious Consequence on the caster.
They are excluded from `spells.json`, so creation cannot hand them out. The `unforgivable-curses`
rule entry records the restriction.

## Coverage against the PDF

Encoded:

- Traits, with the values pool and swap-group assignment at creation.
- 2d6 moves with bands (10+, 7-9, 6-), the Cost choice, and Experience on a miss.
- Luck (spend and regain at school-year end) and Experience (limit 4, then an Advancement).
- Advancements: trait +1 (up to twice, max +3), second Favourite Subject, spell from Year, magical
  object, and Luck. Earned at 4 Experience or at school-year end. A spell Advancement names a spell
  of the character's current Year that they do not already know (`learn` in `advancements.json`,
  `FeatureLearned` event). It has no limit, as the PDF says.
- Spell knowledge: a character knows every spell from earlier Years (`knows` on the Year field).
  `describe` states it for the GM. Graduates know all Years.
- House Points reset at the end of each school year, with their final totals logged
  (`tallies` in `periods.json`, `TallyReset` event). This was the user's choice.
- Award keys: an optional `award` on `adjust_tally` is refused when reused on the same tally, which
  stops a repeat award from counting twice. Keys are cleared at a reset.
- Repeat-amount warning: a positive change whose amount an entry already gained this period is
  refused, with or without a key. The GM must pass `force: true` for a genuinely new award. Gains
  are recorded in `tally_gains` on `CampaignState` and cleared at a reset. Deductions are never
  refused.
- Conditions with their modifiers, and the exit roll (`needs_all_conditions`, `retire`).
- Houses, blood statuses, creation tables, spells per blood status, and Favourite Subject Core and
  Elective groups.
- Friends (limit 2) and Rivals (limit 1), changed through `set_relation`.
- House Points, as a tally with causes, including the Non-Player row.
- Quidditch moves, Narrator moves, Serious Consequences, pets, and healing, as rule text the GM
  reads.

Not encoded (rule text only, or left to the GM):

- The end-of-session timing for Friends and Rivals. The GM is told by the tool description.
- The House table's "last year's winner" bonus. This is a GM-side roll.
- Casting a spell the character does not know costs a Luck point and a roll. The engine does not
  enforce this. The GM reads it from the `describe` knowledge line.
- The "award key" is optional. An award without a key is still checked by the repeat-amount warning,
  but the GM can pass `force: true` to bypass either check. The GM chooses the key and the force
  flag, so a GM can still double-count if it judges a repeat to be a new award.
- Potion descriptions were inferred where the PDF's print layout was hard to read. Not re-checked
  against the PDF in this pass.
- Hair, complexion, and look groupings follow the print layout. Not re-checked in this pass.

Checked against the PDF in this pass:

- Patronus: the PDF rolls 2d6, where the first die picks the animal group and the second picks the
  patronus within it. Both dice are independent d6, so the 36 outcomes are equally likely, and the
  uniform 36-option `patronus` table is equivalent. Its 36 names and six groups match the PDF's grid
  in order.
- Exit roll: the PDF rolls "+your highest trait", so `stat: highest` is correct.
- Non-Player: the House Points tracker's Non-Player row is a tally entry (`tallies.json`).
- House Points have no floor in the PDF, so negative totals are allowed.

## Save and combat

Save and initiative are JSON-driven (`cairn2e/combat.json`). Hogwarts has no combat, so neither is
used there. The cairn2e code is tested through `tests/test_combat.py`, which holds the oracle.

## Validation

- Full suite: 1703 passed (`timeout 900 uv run pytest -n auto -q -p no:cacheprovider`), about 50 s.
- Spell tests do not assume which starting spells a character holds. Creation rolls two random
  5th Year spells for Harry, so the tests pick unheld spells from `HOGWARTS.character.features`
  (`_unheld_spells` in `tests/test_hogwarts_progress.py`). Fixed spell ids made the tests flaky.
- Lint: `uv run ruff format --check src tests` and `uv run ruff check src tests`: clean.
- Hogwarts tests: `tests/test_hogwarts.py`, `tests/test_hogwarts_creation.py`,
  `tests/test_hogwarts_progress.py` (advancements, periods, tallies, relations), and
  `tests/test_hogwarts_creation_form.py` (spell-pick form).
- Cairn 2e: `tests/test_combat.py` (save and initiative oracle), `tests/test_character*.py`.
- Template-grant rejection and `enum_from` rejection: `tests/test_content.py`.
- Event kinds: `tests/test_events.py` and `tests/test_hidden_events.py` require every kind to be in
  `HIDDEN_KINDS`.

### Live testing (real DeepSeek model, uvicorn)

Run against a live server (`uvicorn --factory cygnusforge.web.app:app_from_env`) and the real LLM.
Checks used the browser tools and the event log in the database.

- Creation through the web form: Wren Test, Gryffindor, pure-blood, 3rd Year. The spell pick
  (`anteoculatia`) was saved, and the other two spells were rolled.
- Quidditch win: `adjust_tally` raised Gryffindor House Points 0 to 50.
- Friend request mid-session: the GM refused it, citing the session-end rule. Its refusal of an
  Advancement at 1 Experience was also correct.
- Friend at session end: `set_relation` added Perpetua Vance as Friend (`character.relation_changed`).
- Exit roll: Hide & Sneak, a failed roll (5), raised Experience 1 to 2 (`counter.changed`).
- School-year end: `end_period` recorded `period.ended` and added 1 Advancement. Luck was already
  at 3 (its maximum), so the regain was not visible in this run. The unit tests cover it.
- House Points reset (live, second school-year end): `tally.reset` logged the final totals
  (Ravenclaw 50, others 0, Non-Player 0). The tally was then empty.
- Learn-spell (live): Eldred (6th Year, one Advancement) took Confundo, a 6th Year spell she did not
  hold. `character.feature_learned` was logged and the Advancement went 1 to 0.
- Advancement: `take_advancement` with trait Bravery raised its maximum 0 to 1 and spent the
  Advancement (`character.max_changed`).

Observed in live play (not engine faults):

- The GM double-counted the Quidditch win at session end (House Points 50 to 100), then corrected
  it with a -50 adjustment. The award key (`award` on `adjust_tally`) was added for this.
- Award-key gap, found in a live test: asked to repeat the same award "with the same key", the GM
  did not reuse the key. It sent a new one (`quidditch-ravenclaw-vs-gryffindor`), so the guard
  did not fire, and Ravenclaw went 50 to 100. The guard blocks only an exact repeat of a key. The
  GM chooses the key, so it can avoid the guard. Fixed by the repeat-amount warning above.
- Repeat-amount warning, live recheck (fresh database, uvicorn on 8766, DeepSeek):
  - Gryffindor +50 under key `quidditch-gryffindor-vs-ravenclaw-50`: 0 to 50. Passed.
  - Second +50 under a new key (`quidditch-gryffindor-vs-hufflepuff-50`): the engine refused it with
    "Gryffindor already gained 50 House Points this period. If this is a new award, call again with
    force=true". The GM then retried with `force: true`, and the total went to 100. The GM judged
    it a genuinely new match. This is the intended escape hatch, and it is the remaining limit.
  - The transcript shows the refusal in the tool result, as designed.
- Setup note: the login page had no "Log in as test user" button in this build. A throwaway account
  was registered through `/register` instead.
- DeepSeek timed out twice (`ConnectTimeout`), which left the GM turn "stopped". "Continue" retried
  the turn and worked. The connect timeout was 5 s, so it is now 30 s (`llm/openai_compat.py`).
  The connect timeout is the likely cause, but this is not confirmed by a live run.
- An earlier live run found and fixed two bugs with regression tests: a doubled full stop in
  `MoveMade`, and a `roll_move` enum that lacked Quidditch and exit moves (fixed with `enum_from`).
- Live testing through `TestClient` fails across requests ("Event loop is closed"). The shared
  async httpx client keeps connections from an earlier loop. This is a harness artifact. Use a real
  uvicorn server with a browser or HTTP client instead.

## Narration tense

- Present tense is a core rule (`narration` in `core/rule_set.json`, marked important), so every
  rule set gets it in full. The step prompt no longer repeats it. The journal stays past tense.
- Live check (DeepSeek, both rule sets): the Hogwarts intro and a player turn, and the Cairn intro,
  all read in present tense. Earlier events stay in the past tense.

## Open questions

- "Dragon Phoenix Unicorn" appears in the PDF text after the Quidditch subject. It is probably a
  wand-core list, but the text extraction does not confirm which table it belongs to.
- Whether a Worlds Without Number ruleset should be added later, once its JSON exists.
- Friends and Rivals session-end timing: decided to stay GM-enforced (the engine has no session
  end). Revisit only if the GM is seen to change them mid-session in play.

## Commands

- Tests: `timeout 900 uv run pytest -n auto -q -p no:cacheprovider` (serial runs are refused;
  `CYGNUSFORGE_SERIAL_TESTS=1` allows one).
- Lint: `uv run ruff format src tests`, then `uv run ruff check --fix src tests`.
- Server: `uv run --env-file .env uvicorn --factory cygnusforge.web.app:app_from_env`.

## Progress

- [x] Survey of the engine coupling.
- [x] Design approved (five phases).
- [x] Phase 1: generic primitives (values pools, moves, counters, condition modifiers, exit roll).
- [x] Phase 2: Cairn-only code moved to cairn2e. Twelve tools and 38 tests repointed.
- [x] Phase 3: Hogwarts creation data, spells, Quidditch moves, and the world template.
- [x] Phase 4: tests for both rulesets. Live smoke test.
- [x] Rules text for the GM (`rules.json`), spot-checked against the PDF.
- [x] Phase 5 (first pass): README updated, this SPEC written, commit `aef92f6`.
- [x] Close the gaps: advancements, periods, tallies, relations, restrictions, spell picks (engine
      and web form), save and initiative JSON, and the event renames.
- [x] Live checks: creation, House Points, relation refusal and acceptance, exit roll, school-year
      end, and advancement.
- [x] Gap fixes: House Points reset each school year, learn-spell with the Year rule, the
      spell-knowledge rule, the award key, and the connect timeout (30 s).
- [x] Live checks (second pass): award key, House Points reset at school-year end, learn-spell.
- [x] Award-key gap: the repeat-amount warning refuses a repeat, and `force: true` lets a genuinely
      new award through. The GM can still choose to use `force`, which is the accepted limit.
- [x] Text gaps against the PDF: comfort/support wording and the returning-character scar.
- [x] Needs move: Cairn needs now run through `cairn2e` hooks, and the engine uses generic marks.
      Lint clean. Live check run; it found the NPC regression above, now fixed.
- [x] Present-tense narration made a core rule (`narration`). Live check on both rule sets.
