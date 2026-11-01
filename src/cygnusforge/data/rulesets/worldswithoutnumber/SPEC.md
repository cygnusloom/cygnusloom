# Worlds Without Number ruleset: spec and notes

This file is the working spec and log for the `worldswithoutnumber` ruleset and the generic engine
primitives it needed. It is kept here so the decisions are not lost. The loader reads only
`*.json` files, so this file is not loaded as content.

Reference: `WorldsWithoutNumber_FreePDF_040221.pdf` and `WWN_Character_Sheet_031521.pdf` in this
directory. The rules text here and in `rules.json` is paraphrased; the books are not copied.

## Goal (from the user)

1. Implement the whole free Worlds Without Number book, not a subset: attributes, skills,
   backgrounds, classes, Foci, equipment, checks, combat, saves, System Strain, Effort, the three
   spellcasting traditions, advancement, the bestiary, the GM rules, and the world-generation
   tables.
2. Use the world-generation material to aid the AI GM. The bundled `latter-earth` world is an
   empty template: it has no locations, NPCs, or start.
3. While doing so, find engine code shaped specifically for Cairn 2e or Hogwarts and move that
   behaviour into ruleset JSON or an isolated ruleset module, so the engine stays generic. The
   refactor is a side effect of WWN, not a rewrite.

## Decisions

- Attributes are stored as scores (3-18), not modifiers. A stat may declare `modifier` bands, so
  a score can be read as a modifier wherever a rule needs one. The engine treats a stat with no
  bands as its own modifier, which keeps Cairn and Hogwarts unchanged.
- Skills are a first-class character option: a skill definition, a per-character level (absent
  means untrained, -1), template grants that add levels, and creation-time picks from a table.
- Derived stats, such as the four saves, System Strain, and Effort, are computed from scores,
  counters, and skills by `DerivedStat`. They are stored in the character sheet so saves and
  limits work through the existing stat machinery.
- HP is a rolled stat (`1d6`) with `plus_modifier: "con"`; the class constant is a template bonus.
  Level-ups raise the HP maximum and the attack bonus through the ruleset module.
- Combat uses `hit_mode: roll_vs_defense`, where `armor` is Armor Class, not damage reduction.
  The engine gained a hit-bonus stat, attribute damage, Shock on a miss, a minimum-Shock hit, and
  defeat at zero HP. Cairn's armor-as-absorption and Hogwarts' no-combat setup are unchanged.
- Foci, starting spells, and arts are picked with `feature_picks`; each feature may carry `rank`
  (spell level or Focus level) and creation `effects` (stat, skill, counter, armor, note).
- Advancement arithmetic and starting packages live in the isolated
  [WWN module](../../../rules/wwn.py). Class, experience, casting, art, and skill-prerequisite
  charts live in [progression.json](./progression.json) and are validated for each loaded ruleset.
- Magic state (prepared spells and committed Effort) lives in character `marks`, the way Cairn
  keeps deprivation, so it replays from the log. Daily castings and Effort use harm on ordinary
  stats, so `current(castings)` and `current(effort_high)` are what is left.
- The tradition kind is required, but each tradition template names the classes that may take it
  through the generic `compatible_with` template restriction.
  A Mage's tradition is the full template, an Adventurer mage takes the partial template, and
  Warriors, Experts, and non-mage Adventurers take `no_tradition`. The form hides templates the
  chosen class cannot take, and the server refuses them. The full and partial templates carry
  the book's different starting art and spell counts.
- Morale is a 2d6 check in `rules/wwn.py` (`check_morale`); Instinct is 1d10 against the score
  in a statblock (`instinct_check`). Player characters make neither check.
- Initiative is the book's side roll: `combat.initiative` has 1d8 + the best Dexterity modifier,
  with ties to the party. Adding the first participant of the second side rolls it automatically,
  and `roll_initiative` exposes it to the GM. No player rolls individual initiative. Settling the
  sides clears any turn opened while the participants were still joining, so the first turn always
  follows the settled order.
- The player rolls their own attacks and saves. `player_attack` asks for a d20 and uses that
  natural roll for hit, miss, and natural 1/20; the engine rolls damage and Shock. `player_roll_save`
  asks for the d20 for any save the player's character makes, in or out of an encounter.
  `roll_save` remains the engine roll for NPCs. The engine never takes the player's turn. Every
  turn starts when the player presses Start, including their own; the player ends only their own
  character's turn with End your turn. An NPC's turn ends automatically when the GM's reply
  finishes, and the next actor waits for Start. The GM's `end_npc_turn` tool is not offered.

## Generic engine primitives added

The 2.0 ruleset format is intentionally breaking; the bundled rulesets are migrated while preserving
their player-facing behaviour.

| Area | JSON | Behaviour |
| --- | --- | --- |
| Attribute modifiers | `stat.modifier` | Bands of `{min, modifier}`; `Stat.modifier(score)`, `CharacterSpec.modifier` |
| Rolled stat plus modifier | `stat.plus_modifier` | `hp` adds the Constitution modifier to its roll |
| Derived stats | `stat.derived` | `base`, `plus`, `minus_counters`, `best_modifier`, `minus_best_modifier`, `best_score`, `plus_skills`, `minus_templates`, `minimum` |
| Skills | `type: skill`, `template.skills`, `rule_set.skill_progression` | Skill definitions, level grants, and configured untrained, first, creation maximum, and advancement maximum levels |
| Skill picks | `template.skill_picks` | `{id, count, table, exclude, max_level}`; repeated skills respect the cap, and blank picks are rolled |
| Feature picks | `template.feat_picks` | `{id, count, tag, tags, feature_type, max_rank, exclude}`; `submitted["feature_picks"]` |
| Template compatibility | `template.compatible_with` | Allowed choices in any other template kind |
| Required selections | `template.template_requirements`, `template.choice_key` | Required and distinct choices across template kinds |
| Ruleset data | `type: rule_data` | Validated structured data passed to ruleset tools without global state |
| Feature rank and effects | `feature.rank`, `feature.effects` | Spell/Focus rank; `stat`, `skill`, `counter`, `armor`, `note` at creation |
| Weapon data | `feature.skill`, `attributes`, `shock`, `shock_ac`, `flags` | What a weapon rolls, what it does on a miss, and engine flags such as `no_unskilled_penalty` |
| Checks | `type: check` | `{dice, unskilled, default_difficulty, minimum_difficulty, maximum_difficulty}` |
| Projects | `project.started`, `project.advanced`, `project.abandoned` | Generic progress tracker for projects, research, magic workings, and faction turns |
| Hit and damage bonuses | `combat.hit_bonus_stat`, `damage_bonus_stat`, `attribute_damage`, `level_damage` | Add a stat and the class's half-level damage (Killing Blow) |
| Shock | `combat.shock_on_miss`, `minimum_shock_damage` | Shock on a miss, and a hit never below Shock |
| Defeat | `combat.defeat_at_zero` | A stat reaching 0 defeats the character; PCs may become dying |
| Injuries | `combat.injuries` | Configured harm trigger, affected target kinds, and injury table |
| Monster Shock | statblock attack `shock`/`shock_ac` | Per-attack Shock in the bestiary |
| Unarmed | `combat.unarmed.attributes` | The attributes an unarmed strike may use |
| Flat dice | `rules/dice.py` | Formulas may carry flat bonuses and penalties, such as `1d10+2` |
| Optional kinds | `rule_set.optional_templates` | A template kind a character may leave unchosen (kept for other rule sets) |
| Inventory capacity | `inventory.capacity_stat`, `capacity_multiplier` | Capacity from a stat; WWN uses 1.5 x Strength (Readied half plus Stowed full) |
| Side initiative | `combat.initiative` | `{die, bonus_stat, side, tie}`; `encounters.roll_side_initiative` |
| Armor mode | `shop_item.armor_mode` | `add`, `set`, `max`, or `shield`, so armor need not stack |
| Base defense | `combat.defense_base`, `defense_modifier` | Unarmored AC, such as 10 + Dexterity |
| Natural hits | `combat.natural_one_misses`, `natural_max_hits` | A natural 1 always misses and a natural 20 always hits |
| Filtered enums | `enum_from` object form | `{source, feature_type, tag, max_rank}`, plus `skills` and `checks` |

## Refactors of Cairn/Hogwarts assumptions

- `rules/dice.py` accepted only dice terms. WWN bestiary lines such as `1d10+2` and `1d6-1` needed
  flat modifiers; they now parse and roll, while `plus: "highest"` still keeps the highest dice
  total and adds the flat bonus.
- `check_arguments` used the raw die bounds for a stat. A stat whose roll adds another stat's
  modifier now widens its schema bounds (`CharacterSpec.stat_bounds`), so hp above 6 is legal.
- The creation form assumed every stat was set by the player. Derived stats are skipped in
  `roll_stats`, `fixed_stats`, `check_swaps`, `character_schema`, `creation_view`, and the page's
  re-roll check.
- The page assumed every template kind must be chosen. `optional_templates` and a None radio make
  a kind optional; `submission_from_form` omits it.
- Skill and feature pick fields were not namespaced by template, so hidden templates' values
  collided when a player changed class. Fields are now `pick-skill-<template>-<pick>-<n>` and
  `pick-feature-<template>-<pick>-<n>`; the old names are still read as a fallback.
- Pick validation looked up the first pick with an id across all templates. It now validates the
  picks of the chosen templates only.
- The randomizer chose optional kinds and filled hidden templates' picks. It skips optional panels
  and only fills visible ones.
- `harm_character` knew only Cairn's overflow stat. It now also uses `defeat_at_zero`: a PC is
  mortally wounded and starts the dying clock, while an NPC is dead.
- `encounters.start_encounter` is now given the combat rules, so side initiative can be detected.
- `WorldTools` gained `checks`, `roll_check`, `raise_skill`, `restore_stat`, `set_stat_max`,
  `set_debug_stat`, and `_weapon_feature`, and passes the character spec and weapon feature into
  attacks and moves.
- Events gained `Character.skills`, `Character.attack_shock`, `CheckMade`, `SkillChanged`, and
  `StatRestored`; `Struck` records Shock and defeat.

## Ruleset content

| File | Contents |
| --- | --- |
| `rule_set.json` | Head, NPC HP tiers, encounter grid |
| `stats.json` | Six attributes with modifier bands; hp, attack bonus, castings, morale; derived strain, three Effort pools, four saves |
| `skills.json` | The 21 skills |
| `backgrounds.json` | 20 background templates with a free skill and two Learning picks |
| `classes.json` | Warrior, Expert, Mage, four Adventurer pairings, and full and partial traditions with class gating |
| `foci.json` | Every free-book Focus, level 1 and 2, plus the nine nonhuman origin Foci |
| `equipment.json` | Weapons, armor, adventuring gear, item sizes, inventory rules, marketplace, silver currency |
| `character_fields.json` | Look, Goal, Appearance, Biography |
| `tables.json` | Appearance, personality, accent, quirk, names, 20 Growth/Learning table pairs, and the all-skills table |
| `checks.json` | The 2d6 skill check |
| `conditions.json` | Frail, prone, grappled, unconscious |
| `combat.json` | The WWN combat rules and the two module declarations |
| `counters.json` | Level, XP, skill points, Focus picks, art picks, attribute boosts |
| `spells.json` | High Magic, Necromancer, and Elementalist spells as ranked features |
| `arts.json` | The three traditions' arts as features |
| `monsters.json` | The free-book bestiary with AC, Shock, saves, and Morale |
| `rules.json` | 52 paraphrased GM rules, with the core ones marked important |
| `dice_tables.json` | Terrain, nation, ruler, government, history, and tag tables |
| `worldgen_tables.json` | Reaction, culture, religion, community, site, ruin, event, and monster tables |
| `world_terrain.json` | Ground kinds for a Latter Earth map |
| `prompts.json` | Six WWN-specific GM prompt fragments, including encounter setup and player rolls |
| `tool_calls.json` | Checks, morale, advancement, gear, and magic tools |
| `adventure_tools.json` | Combat, encounter, map, save, reaction, Instinct, rest, travel, and market tools |
| `rest.json`, `clock.json` | A night or a few moments of rest and the four parts of a day |

The world template `src/cygnusforge/data/worlds/latter-earth/world.json` is deliberately empty: it
names the rule set and gives a blurb, with no locations, NPCs, or start. The live GM uses the dice
tables to create the first location and its people.

## Creation

- Attributes roll 3d6 in order; the form allows the rolled values to be reassigned within the
  group (the book's clement option) and one swap, as the engine's swap rule allows.
- Backgrounds grant the free skill at level 0 and two picks from the Learning table (the quick
  skills are also in that table, so the quick option is covered by the same picks).
- Classes grant their level-1 HP constant, attack bonus, one free skill pick from all 21 skills,
  and Focus picks. The tradition is required and class-gated: a full mage takes the full
  tradition template (two High Magic arts, four spells; three Elementalist arts, four spells),
  and a partial mage takes the partial template (one High or Necromancer art and two spells;
  three Elementalist arts and two spells). Non-casters take `no_tradition`.
- Foci, starting spells, and arts are `feature_picks`; each Focus's fixed bonuses are applied at
  creation through `effects`. A starting equipment package or 3d6 x 10 silver may follow.
- `resolve` caps creation skills at level 1, keeps HP at 1 or more, and computes derived stats
  last. A partial mage's Effort is one lower, never below 1.

## Combat

- Attack roll: d20 + attack bonus + weapon skill (or -2 untrained) + best weapon attribute. A
  natural 1 always misses and a natural 20 always hits. Crossbows ignore the unskilled penalty.
- Damage: weapon dice + best attribute + `damage_bonus_stat` + Punch for unarmed, with a
  Warrior's half level (rounded up) added as Killing Blow to damage and Shock.
- Shock: on a miss, if AC is at most `shock_ac`, the target takes Shock + attribute + damage
  bonus; a hit does at least that much.
- AC is the `armor` value: 10 + Dex modifier + one suit of armor + a shield. Creation records it,
  `set_ac` recomputes it, and purchases use `armor_mode` (`max` for body armor, `shield` for
  shields) so suits do not stack.
- A hit that takes hp to 0 defeats the target. An NPC is dead; a player character is mortally
  wounded and starts the book's six-round clock. `encounters.end_turn` ticks it whenever a round
  turns over, and the character dies at zero. A one-hour hourly clock remains as the fallback
  between fights, so a dying character outside an encounter still has a clock.
- Encounters are button-driven. The GM sets the encounter up without asking, but never advances
  the order after a reply: the player presses Start for every turn, and End your turn only for
  their own character. An NPC's turn ends by itself when the GM's reply finishes.
- Saves are d20 at or above the derived save score (16 - level - the best pair modifier; Luck is
  16 - level), so a better attribute lowers the target number. Monsters use their statblock saves.
- Side initiative is 1d8 + the best Dexterity modifier, ties to the party. It rolls on its own
  once both sides have joined the encounter.

## Advancement and magic

- `gain_xp` adds to `xp` on the fast or slow track and tells the GM when a level is due.
  `level_up` rerolls the class hit dice for the whole new level (old +1 if no better), raises the
  attack bonus, grants three skill points (four for Quick Learner), Focus and art picks, and
  casting limits.
- `spend_skill_point` charges 1, 2, 3, 4, or 5 points for levels 0 to 4 and enforces the minimum
  character levels 1, 1, 3, 6, and 9. `improve_attribute` buys the five attribute boosts at 1 to
  5 points.
- `learn_focus` also accepts a nonhuman origin Focus; `learn_art` spends an art pick on the
  character's tradition; `learn_spell` lets Necromancers and Elementalists learn High Magic.
- `prepare_spells` replaces the prepared list, limited by the class/level casting table.
  `cast_spell` spends one `castings` point. `restore_spell_slots` clears them.
- `commit_effort` harms the tradition's Effort stat and records the duration in the
  `effort_committed` mark; `recover_effort` refunds the matching duration only. `add_strain` and
  `heal_strain` use the `strain` stat, and `first_aid` heals 1d6 + Heal for 1 Strain, but not at
  maximum Strain.
- `recover_after_rest` restores HP by level, 1 Strain, castings, scene and day Effort; Frail and
  interrupted rests recover no HP or Strain.

## Known simplifications

- A dual-mage Adventurer picks two partial traditions, each with its own art and spell picks and
  its own Effort pool. The Healer and Vowed are offered as full classes, although the book
  presents them as partial classes, because the class-pairing system does not yet combine two
  arbitrary partial classes.
- Readied and Stowed are approximated by one inventory pool of 1.5 x Strength (half Readied plus
  full Stowed); the armor-encumbrance penalty on Sneak/Exert is rule text the GM applies.
- Surprise, grappling, execution attacks, total defense, dual wielding, range bands, and many
  combat actions are rule text plus the relevant check/attack tools, not dedicated states.
- Magic Workings, spell research, magic item creation, hirelings, projects, and faction turns are
  rule text; the engine has no subsystem for them.
- Bestiary multi-attacks are parsed from the statblock text (`N attacks per action`), so one
  `attack` call resolves every attack the creature makes.
- Monster special abilities are prose in the statblocks; they are not machine-readable rules.
- PCs have a Morale stat for uniformity, but the Morale and Instinct tools refuse PCs.

## Validation

- Full suite: `timeout 900 uv run pytest -n auto -q -p no:cacheprovider`: 1965 passed in about
  41 seconds, with the Worlds Without Number tests across `test_wwn.py`,
  `test_wwn_combat.py`, `test_wwn_advancement.py`, `test_wwn_magic.py`, and
  `test_wwn_refactor.py`.
- Lint: `uv run ruff format src tests` and `uv run ruff check src tests`: clean.
- WWN tests: `tests/test_wwn.py` (content, modifiers, derived stats, creation, spell picks),
  `tests/test_wwn_combat.py` (AC, Shock, hit/damage bonuses, defeat, outside harm, natural
  1/20, side initiative, player-rolled attacks and saves, button-driven NPC turns, the six-round
  dying clock), `tests/test_wwn_advancement.py` (XP tracks, levels, skills,
  attributes, Foci, origins, arts, spells, AC, Frail rest), and `tests/test_wwn_magic.py`
  (preparation, castings, Effort, Strain, first aid), `tests/test_wwn_refactor.py` (capacity,
  side initiative, enums, armor modes).
- Live run 1 (real DeepSeek, SQLite on port 8903): created a Warrior from the creation form,
  the GM used the world-generation tables to build Greywater Ford with two NPCs, moved the
  character there, and narrated the opening. A second turn rolled a real 2d6 Notice check
  (`check.made`).
- Live run 2 (real DeepSeek, SQLite on port 8910): created a partial Elementalist Adventurer,
  generated Vetch's Ford from the tables, then ran a real encounter: `create_encounter_map`,
  `start_encounter`, `add_participant`, automatic side initiative, `place_participant` moves,
  the PC's unarmed attack (1 damage), and the hound's bite (4 damage). The run exposed that
  spell picks were offering High Magic arts; the pick now filters by `feature_type: "spell"`,
  with a test.
- Live run 3 (real DeepSeek, SQLite on port 8913): the randomizer picked the new Healer class;
  the opening generated Ammat's Door with NPCs. A hound attack started an encounter, added both
  sides, and settled side initiative. The player pressed Start Jora's turn, End your turn, then
  Start Alley Hound's turn; the hound acted and its turn ended automatically, leaving Start
  Jora's turn for round two.

## Commands

- Tests: `timeout 900 uv run pytest -n auto -q -p no:cacheprovider`.
- Lint: `uv run ruff format src tests`, then `uv run ruff check --fix src tests`.
- Server: `uv run --env-file .env uvicorn --factory cygnusforge.web.app:app_from_env`.

## Progress

- [x] Survey the engine and the free book; capture the mechanics in `.wwn_work/spec_*.md`.
- [x] Generic primitives: modifier bands, derived stats, skills, picks, checks, weapon/Shock data,
      combat fields, optional kinds, flat dice.
- [x] Refactors: schema bounds, derived stats in creation, optional templates, pick namespacing
      and validation, randomizer, outside-combat defeat, encounter wiring.
- [x] Content: attributes, skills, 20 backgrounds, 7 classes and 3 traditions, Foci, equipment,
      spells, arts, bestiary, rules, world-generation tables.
- [x] Ruleset modules: `rules/wwn.py` and `rules/wwn_magic.py`.
- [x] Empty Latter Earth world template.
- [x] Tests for creation, checks, combat, advancement, magic, and the refactors.
- [x] Live DeepSeek run: creation, world generation, narration, and a skill check.
- [x] Fidelity pass: corrected saving throws, AC, natural 1/20, Killing Blow, skill and
      attribute costs, HP rerolls, slow XP, arts, starting spells, first aid, Frail rest,
      tradition gating, origin Foci, gear, reaction/Instinct, map and combat tools, and 47
      world-generation tables. Found and fixed the spell-pick filter in a live run.
- [x] Parity pass 2: Healer and Vowed classes and arts, dual-mage second traditions, readied and
      stowed items, shield Shock, multi-attack statblocks, named NPC mortal wounds, generic
      `heal_character` and `lookup_condition`, explicit Start/End turn flow with automatic NPC
      turn endings, project/research/faction tracking, Die Hard and Impervious Defense, and a
      faster suite.
