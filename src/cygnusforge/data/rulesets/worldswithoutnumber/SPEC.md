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
- Advancement, the class tables, the casting tables, and the starting packages live in
  `src/cygnusforge/rules/wwn.py`. These are ruleset-specific arithmetic, not lookups the GM
  makes, so they are isolated Python data rather than JSON.
- Magic state (prepared spells and committed Effort) lives in character `marks`, the way Cairn
  keeps deprivation, so it replays from the log. Daily castings and Effort use harm on ordinary
  stats, so `current(castings)` and `current(effort_high)` are what is left.
- The tradition kind is optional (`optional_templates`), because only mage classes need one. The
  creation form shows a None choice and the randomizer never selects an optional kind.
- Morale is a 2d6 check in `rules/wwn.py` (`check_morale`), not the engine's Cairn morale module.
- Initiative uses `initiative_mode: roll_order`; the GM rolls side initiative (1d8 + the best Dex
  modifier) and sets the order. The generic side-initiative engine support exists and is tested
  with a synthetic combat, but is not enabled here because participants usually join after the
  encounter starts.

## Generic engine primitives added

All defaults preserve the old behaviour, so Cairn 2e and Hogwarts JSON need no changes.

| Area | JSON | Behaviour |
| --- | --- | --- |
| Attribute modifiers | `stat.modifier` | Bands of `{min, modifier}`; `Stat.modifier(score)`, `CharacterSpec.modifier` |
| Rolled stat plus modifier | `stat.plus_modifier` | `hp` adds the Constitution modifier to its roll |
| Derived stats | `stat.derived` | `base`, `plus`, `minus_counters`, `best_modifier`, `best_score`, `plus_skills` |
| Skills | `type: skill`, `template.skills` | Skill definitions and level grants; absent is untrained |
| Skill picks | `template.skill_picks` | `{id, count, table, exclude, max_level}`; `submitted["skill_picks"]` |
| Feature picks | `template.feat_picks` | `{id, count, tag, max_rank, exclude}`; `submitted["feature_picks"]` |
| Feature rank and effects | `feature.rank`, `feature.effects` | Spell/Focus rank; `stat`, `skill`, `counter`, `armor`, `note` at creation |
| Weapon data | `feature.skill`, `attributes`, `shock`, `shock_ac` | What a weapon rolls and what it does on a miss |
| Checks | `type: check` | `{dice, unskilled, default_difficulty, minimum_difficulty, maximum_difficulty}` |
| Hit and damage bonuses | `combat.hit_bonus_stat`, `damage_bonus_stat`, `attribute_damage` | Add a stat, the weapon's attribute, and the damage stat |
| Shock | `combat.shock_on_miss`, `minimum_shock_damage` | Shock on a miss, and a hit never below Shock |
| Defeat | `combat.defeat_at_zero` | A stat reaching 0 defeats the character; PCs may become dying |
| Monster Shock | statblock attack `shock`/`shock_ac` | Per-attack Shock in the bestiary |
| Unarmed | `combat.unarmed.attributes` | The attributes an unarmed strike may use |
| Flat dice | `rules/dice.py` | Formulas may carry flat bonuses and penalties, such as `1d10+2` |
| Optional kinds | `rule_set.optional_templates` | A template kind a character may leave unchosen |
| Inventory capacity | `inventory.capacity_stat`, `capacity_multiplier` | Capacity from a stat, e.g. Strength x2 |
| Side initiative | `combat.initiative` | `{die, bonus_stat, side, tie}`; `encounters.roll_side_initiative` |
| Armor mode | `shop_item.armor_mode` | `add`, `set`, or `max`, so armor need not stack |
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
| `rule_set.json` | Head, NPC HP tiers, optional `tradition` kind, encounter grid |
| `stats.json` | Six attributes with modifier bands; hp, attack bonus, castings, morale; derived strain, three Effort pools, four saves |
| `skills.json` | The 21 skills |
| `backgrounds.json` | 20 background templates with a free skill and two Learning picks |
| `classes.json` | Warrior, Expert, Mage, four Adventurer pairings, three traditions |
| `foci.json` | Every free-book Focus, level 1 and 2, with mechanical `effects` |
| `equipment.json` | Weapons, armor, gear, item sizes, inventory rules, marketplace, silver currency |
| `character_fields.json` | Look, Goal, Appearance, Biography |
| `tables.json` | Appearance, personality, accent, quirk, names, and 20 Growth/Learning table pairs |
| `checks.json` | The 2d6 skill check |
| `conditions.json` | Frail, prone, grappled, unconscious |
| `combat.json` | The WWN combat rules and the two module declarations |
| `counters.json` | Level, XP, skill points, Focus picks |
| `spells.json` | High Magic, Necromancer, and Elementalist spells as ranked features |
| `arts.json` | The three traditions' arts as features |
| `monsters.json` | The free-book bestiary with AC, Shock, saves, and Morale |
| `rules.json` | Paraphrased GM rules, with the core ones marked important |
| `dice_tables.json` | World-generation tables for terrain, nations, rulers, history, and tags |
| `world_terrain.json` | Ground kinds for a Latter Earth map |
| `prompts.json` | Four WWN-specific GM prompt fragments |
| `tool_calls.json` | 18 GM tools for checks, morale, advancement, gear, and magic |

The world template `src/cygnusforge/data/worlds/latter-earth/world.json` is deliberately empty: it
names the rule set and gives a blurb, with no locations, NPCs, or start. The live GM uses the dice
tables to create the first location and its people.

## Creation

- Attributes roll 3d6 in order; the form allows the rolled values to be reassigned within the
  group (the book's clement option) and one swap, as the engine's swap rule allows.
- Backgrounds grant the free skill at level 0 and two picks from the Learning table (the quick
  skills are also in that table, so the quick option is covered by the same picks).
- Classes grant their level-1 HP constant, attack bonus, skill, and Focus picks. The tradition is
  optional and grants Magic plus one art pick.
- Foci, starting spells, and arts are `feature_picks`; each Focus's fixed bonuses are applied at
  creation through `effects`.
- `resolve` caps creation skills at level 1 and computes derived stats last.

## Combat

- Attack roll: d20 + attack bonus + weapon skill (or -2 untrained) + best weapon attribute.
- Damage: weapon dice + best attribute + `damage_bonus_stat` + Punch for unarmed.
- Shock: on a miss, if AC is at most `shock_ac`, the target takes Shock + attribute + damage
  bonus; a hit does at least that much.
- AC is the `armor` value. `set_ac` computes 10 + Dex modifier + worn armor; the marketplace can
  set or keep the higher armor with `armor_mode`.
- A hit that takes hp to 0 defeats the target. An NPC is dead; a player character is mortally
  wounded and starts a one-hour dying clock. The clock stands in for the book's six rounds
  because the engine's clock counts hours; the `mortal-wounds` rule says so.
- Saves are d20 at or above the derived save score (16 - level - the best pair modifier; Luck is
  16 - level). Monsters use their statblock save values.

## Advancement and magic

- `gain_xp` adds to `xp` (fast or slow track) and tells the GM when a level is due. `level_up`
  raises the level, HP, attack bonus, skill points, Focus picks, and casting limits.
- `spend_skill_point` raises a skill to at most level 4. `learn_focus`, `learn_spell`,
  `grant_package`, and `set_ac` are the other advancement/gear tools.
- `prepare_spells` replaces the prepared list, limited by the class/level casting table.
  `cast_spell` spends one `castings` point. `restore_spell_slots` clears them.
- `commit_effort` harms the tradition's Effort stat and records the duration in the
  `effort_committed` mark; `recover_effort` refunds the matching duration only. `add_strain` and
  `heal_strain` use the `strain` stat, and `first_aid` heals 1d6 + Heal for 1 Strain.
- `recover_after_rest` restores HP by level, 1 Strain, castings, and scene Effort.

## Known simplifications

- A dual-mage Adventurer has one tradition, because the form has one optional tradition kind. The
  dual-mage casting table is used for the class, but both partial traditions are the same one.
- Skill points are one per level (two for Quick Learner), and one point buys one skill level. The
  book's escalating 3-point costs are not modelled.
- The dying clock is one hour instead of six rounds (the engine's clock is hourly).
- Readied and Stowed are approximated by item slots and a Strength x2 total capacity; the
  armor-encumbrance penalty on Sneak/Exert is rule text the GM applies as a check modifier.
- Side initiative is supported by the engine but not enabled; the GM rolls 1d8 + best Dex for
  each side and sets the order.
- Morale, surprise, grappling, execution attacks, and many combat actions are rule text plus the
  relevant check/attack tools, not dedicated engine states.
- Magic Workings, spell research, and magic item creation are rule text; there is no crafting
  subsystem.

## Validation

- Full suite: `timeout 900 uv run pytest -n auto -q -p no:cacheprovider`: 1934 passed at the time
  of writing (before the last test additions); the WWN files add 74 tests.
- Lint: `uv run ruff format src tests` and `uv run ruff check src tests`: clean.
- WWN tests: `tests/test_wwn.py` (content, modifiers, derived stats, creation),
  `tests/test_wwn_combat.py` (AC, Shock, hit/damage bonuses, defeat, outside harm),
  `tests/test_wwn_advancement.py` (XP, levels, skills, Foci, spells, AC), and
  `tests/test_wwn_magic.py` (preparation, castings, Effort, Strain, first aid),
  `tests/test_wwn_refactor.py` (capacity, side initiative, enums, armor modes).
- Live run (real DeepSeek, fresh SQLite database, uvicorn on port 8903): created a Warrior from
  the creation form, the GM used the world-generation tables to build Greywater Ford with two
  NPCs, moved the character there, and narrated the opening. A second turn rolled a real 2d6
  Notice check (`check.made`) and narrated the failure. The character sheet showed the six
  attributes, HP, attack bonus, castings, Strain, three Effort pools, four saves, skills, and
  Foci.

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
