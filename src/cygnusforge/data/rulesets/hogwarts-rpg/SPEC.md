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

## Decisions

- Scope: the full game, chosen by the user. Traits, 2d6 moves, Luck, Experience, Advancements,
  Conditions and the exit roll, Friends and Rivals, House Points, character creation, spells,
  potions, Quidditch, Narrator moves, and Serious Consequences.
- Engine style: "fat yet generic". As much as possible is set in each ruleset's JSON. Ruleset
  Python is an escape hatch, used only where JSON cannot express a rule. Hogwarts needs no Python.
- Cairn 2e behaviour must not change. Its tests were repointed only where they named a tool that
  moved to cairn2e (see Phase 2).
- Worlds Without Number has only its PDF, so it is out of scope. The loader skips ruleset
  directories that have no JSON, with a warning, so the app and tests still run.
- Morale, needs, and hirelings were moved into cairn2e as a judgment call. See Open questions.

## Engine changes (generic primitives, set by JSON)

Phase 1 added these. Each is parameterized, not Cairn- or Hogwarts-specific.

- **Stats** may use `values`, a fixed pool of whole numbers shared by a `swap_group`. The stats in
  the group take the values in any order. A rolled `formula` still works.
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
  `pick_stat`, `counter`, `end_condition`, `retire`. `counter` and `end_condition` are move-only.
  `retire` records `CharacterRetired`, which the exit roll uses. There is no op that changes a
  stat (see Known gaps).
- **Template grants** (`templates.json`): `stats` (bonuses added to stats) and `counters` (amounts
  added to starting values), applied in `content/character.py` `resolve`.
- **`enum_from`** on a tool parameter: `"moves"` or `"stats"` fills the enum from the rule set's
  own content. The GM's `roll_move` uses it, so the choices cannot drift from the content.
- **`condition_ends`** on `rule_set.json` names the rule set's triggers. `ALWAYS_CONDITION_END`
  (`gm`) is always available. Cairn 2e uses `rest`, `extended_rest`, `aid`, and `time`.
- The loader skips a ruleset directory with no JSON and logs a warning.

Phase 2 genericized the coupling:

- Twelve Cairn-only `WorldTools` methods moved out of core into cairn2e. 38 tests that call them
  were repointed.
- Dying and fatal are condition flags, not hardcoded checks. The old global `CONDITION_ENDS` is
  now `ALWAYS_CONDITION_END` plus each rule set's `condition_ends`.
- Morale, needs, and hirelings remain engine modules (`services/morale.py`, `services/needs.py`,
  `services/hirelings.py`), enabled only when a rule set's JSON turns them on. Cairn 2e is the
  only rule set that does.

## Hogwarts content (`src/cygnusforge/data/rulesets/hogwarts-rpg/`)

| File | Contents |
| --- | --- |
| `rule_set.json` | Name, description |
| `stats.json` | Five traits: Bravery, Cunning, Intellect, Loyalty, Magic. Values -1, 0, +1, +1, +2 in one swap group (`traits`), so any order |
| `counters.json` | Luck (0 to 3, starts at 3). Experience (0 to 4). At 4 Experience, a `full` effect clears it and notes the Advancement |
| `conditions.json` | Scared, Angry, Stressed, Jealous, Embarrassed (-2 to one trait each), Injured (-1 all), Hexed (-1 to a trait the Narrator picks), Unconscious (cannot act). Each has its ends and effects |
| `moves.json` | 14 moves: Stand in the Face of Danger, Help or Hinder, Hide & Sneak, Get What You Seek, Gain Knowledge, Approach a Magical Creature, Roll, Cast a Spell, Duel, Concoct a Potion, Use a Magical Object, Exit the Story, Play a Quidditch Match, Catch the Golden Snitch |
| `tool_calls.json` | The GM's `roll_move` tool, with `enum_from` for `move` and `stat` |
| `rules.json` | 12 rule entries the GM reads with `lookup_rule` (below) |
| `templates.json` | 4 houses (each +1 to one trait) and 3 blood statuses (spells and starting Experience) |
| `tables.json` | Wand wood and ambition, Heritage, Favourite subject, Patronus, pet, look, and name tables |
| `character_fields.json` | The creation fields that use those tables |
| `spells.json` | 126 spell features. Unforgivable Curses are excluded (below) |

These files were generated once from the PDF text by a script that is not kept in the repo. They
are now maintained by hand. Edit the JSON directly.

The world template `src/cygnusforge/data/worlds/hogwarts/` (locations and a start with hooks) gives
a campaign the Great Hall and the grounds.

### Rules the GM reads (`rules.json`)

The GM sees these by brief and reads the full text with `lookup_rule`. Each entry is a paraphrase
of the PDF, checked against the PDF text in Phase 5:

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
- Luck and Experience counters, with the Experience limit of 4.
- Conditions with their modifiers, and the exit roll (`needs_all_conditions`, `retire`).
- Houses, blood statuses, creation tables, spells, and the Quidditch moves.
- Narrator moves, Serious Consequences, House Points, Friends and Rivals, pets, and healing, as
  rule text the GM reads.

Not encoded (rule text only, or left to the GM):

- Luck regained at the end of each school year. The counter has no reset, so the GM must apply it.
- House Points have no counter. The GM records them in its notes. The rule text has the guide.
- Friends and Rivals prompts and the session-end change. The rule text covers the limit (2 Friends,
  1 Rival), but the engine does not track who they are.
- House "Last Year's Winner" bonus.
- Core and Elective split of the Favourite Subjects table. The engine has one list.
- Spell counts per blood status, and Year-based spell learning.
- Patronus is a uniform 36-option table, not the PDF's 2d6 roll.
- The exit-roll stat choice is free, not strictly the highest trait.
- Advancements that raise a trait by 1 (see Known gaps).
- Potion descriptions were inferred where the PDF's print layout was hard to read.
- Hair, complexion, and look groupings follow the print layout.

Saves and `gain_condition` still depend on combat. Hogwarts has no combat, so they are unused
there, but they are not yet fully generic.

## Known gaps

- **Advancements that change a stat are not automated.** The engine has no op that changes a stat,
  so "upgrade a trait by +1" is recorded in the rule text, and the GM must apply it by hand. A
  generic stat-change op would fix this. It is not in this change.
- **Creation requires explicit choices.** The creation form needs a pick for pronouns, house, blood
  status, and each table field, or "Randomize all not chosen" to fill the rest. This is by design,
  so a player does not get an unintended random choice.
- **Morale, needs, and hirelings are still engine modules.** They are gated by JSON, but the code
  lives in `services/`. Moving them wholly into cairn2e is a larger refactor.

## Validation

- Full suite: 1632 passed (`uv run pytest -n auto`), about 56 s.
- Lint: `uv run ruff format --check src tests` and `uv run ruff check src tests`: clean.
- Hogwarts tests: `tests/test_hogwarts.py` (26) and `tests/test_hogwarts_creation.py`.
- Template-grant rejection and `enum_from` rejection: `tests/test_content.py`.

### Live testing (real DeepSeek model, uvicorn)

Run against a live server (`uvicorn --factory cygnusforge.web.app:app_from_env`) and the real LLM.
The browser tool could not reach the local server, so the checks went over HTTP.

- Creation of Harry Potter (Gryffindor, half-blood), and the GM opening at the Great Hall: works.
- A Stand in the Face of Danger roll ("rolls 5 for 5, Failure") took Experience from 2 to 3, and
  was narrated correctly.
- A Hogwarts GM turn asked about the House Points for a trivial good deed. The GM called
  `lookup_rule`, read the House Points guide, and answered 5 points. This confirms the rules
  content works end to end.
- Two bugs were found in the live run and fixed, each with a regression test:
  - The `MoveMade` render printed a doubled full stop ("as a result.."). Fixed in
    `services/events.py`.
  - The GM could not roll Quidditch or exit the story, because the `roll_move` enum had no such
    values. Fixed with `enum_from`.
- Live testing through `TestClient` fails across several requests ("Event loop is closed"). The
  shared async httpx client keeps connections from an earlier loop. This is a harness artifact, not
  an app bug. Use a real uvicorn server with an HTTP client instead.

Not yet live-tested: a Quidditch turn and a full exit-roll turn.

## Open questions

- Whether `morale`, `needs`, and `hirelings` should move wholly into cairn2e. They are gated by
  JSON now, and the design left this open.
- Whether a Worlds Without Number ruleset should be added later, once its JSON exists.

## Commands

- Tests: `uv run pytest -n auto` (serial runs are refused; `CYGNUSFORGE_SERIAL_TESTS=1` allows one).
- Lint: `uv run ruff format src tests`, then `uv run ruff check --fix src tests`.
- Server: `uv run --env-file .env uvicorn --factory cygnusforge.web.app:app_from_env`.

## Progress

- [x] Survey of the engine coupling.
- [x] Design approved (five phases).
- [x] Phase 1: generic primitives (values pools, moves, counters, condition modifiers, exit roll).
- [x] Phase 2: Cairn-only code moved to cairn2e. Twelve tools and 38 tests repointed.
- [x] Phase 3: Hogwarts creation data, spells, Quidditch moves, and the world template.
- [x] Phase 4: tests for both rulesets. Live smoke test over HTTP.
- [x] Rules text for the GM (`rules.json`), checked against the PDF.
- [x] Phase 5: README updated, this SPEC written, commit.
