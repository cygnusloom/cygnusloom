# D&D 5.5e (SRD 5.2) playtest

This is the test plan and record for the bundled `dnd-srd-2024` rule set. The source of
truth is the shipped `SRD_CC_v5.2.1.pdf`; all content is regenerated with
`scripts/fetch_srd2024.py` and `scripts/build_srd2024.py`.

## Layers

1. **Content validation** checks that every definition parses, every reference resolves,
   templates pick real features, and generated spell/monster/equipment counts match the
   source.
2. **Automated rule tests** use a real campaign, encounter, and event log. They cover
   creation, advancement, combat math, criticals, Extra Attack, spell attacks, hazards,
   rests, death saves, healing, and bestiary data.
3. **Live game tests** start a throwaway server with DeepSeek and drive the browser:
   create a campaign and character, play the opening, take a combat turn, roll a spell
   attack, award experience, and use the level-up page.
4. **PDF audits** compare the generated content and runtime rules against sections of
   `/tmp/srd2024.txt` (the extracted SRD text), rather than trusting the Open5e API
   shape alone.

## Automated commands

```bash
PYTHONPATH=$PWD/src /workspaces/cygnusloom/.venv/bin/python -m cygnusloom.cli content validate
PYTHONPATH=$PWD/src /workspaces/cygnusloom/.venv/bin/python scripts/test.py \
  tests/test_dnd_srd.py tests/test_dnd_srd_play.py -q
PYTHONPATH=$PWD/src /workspaces/cygnusloom/.venv/bin/python scripts/test.py -q
```

The current full suite is **3821 passed**; the two D&D files are **58 passed**; content
validation reports **0 warnings**; `ruff check` is clean on the changed source and test
files.

## Live matrix

| Area | What to do | Expected |
| --- | --- | --- |
| Campaign creation | Choose D&D, a world, Standard experience, and a Fighter | Character is created with a class kit, background skills, Soldier ability scores, and Fighting Style |
| Sheet | Open Character | HP, AC, level, skills, features, and resources are visible; monster-only save stats stay hidden |
| Advancement | Award 300 XP from the debug panel | Level-up page opens; Fighter 2 gains +8 HP and Action Surge; the sheet updates |
| Initiative | Start an encounter with one PC and one monster | `roll_initiative` records every combatant and opens the first turn; the order is fixed across rounds |
| Weapon combat | Move into reach and attack | The player rolls the d20; the engine adds attack and damage modifiers, logs the strike, and spends the attack action |
| Spellcasting | Create a Cleric, pick spells/cantrips on the advancement page, then cast Guiding Bolt | Prepared list and slots show on the sheet; a spell attack prompts for the d20, logs the roll against AC, and spends the slot |
| Recovery | Harm a PC, stabilize, then heal | Healing above 0 clears the defeated state; a stabilized character is no longer dying |

The live checks used a throwaway SQLite database, `CYGNUSLOOM_TEST_LOGIN=1`,
`CYGNUSLOOM_TEST_LOGIN_ADMIN=1`, `CYGNUSLOOM_DEBUG_PANEL=1`, and
`CYGNUSLOOM_DISABLE_MUSIC=1`. The server was run from the worktree with the main
`.venv` and `PYTHONPATH`, never with `uv sync`.

## Fixes made in this pass

- Show tracked AC on player and hireling sheets.
- Preserve D&D's rolled individual initiative order across rounds.
- Log spell attacks visibly and count them as an attack action.
- Keep MonsterTemplate skills, challenge rating, experience, and printed initiative.
- Use the printed monster initiative bonus when rolling.
- Add D&D critical hits, Extra Attack, Action Surge budget, healing recovery, hazard
  DCs, and fixed class saves.
- Require no shield for Monk Unarmored Defense.
- Generate correct Fighter/Monk saving-throw descriptions from SRD data.
- Route first-level prepared casters to their spell/cantrip picks after creation.

## Closed in the SRD gap pass

These were previously listed as gaps and now have data, engine support, and tests:

- Background ability scores offer the SRD's +2/+1 and +1/+1/+1 modes.
- Backgrounds and classes offer their lettered equipment packages, including the 50 GP
  background alternative and the class B/C gold options.
- Origin feats: Magic Initiate chooses two cantrips, a level-1 spell, and its casting
  ability and can cast that spell once without a slot per long rest; Alert adds the
  proficiency bonus to Initiative; Savage Attacker rolls weapon damage dice twice.
- Species: Human Skillful/Versatile, Dwarf toughness and poison resistance, Dragonborn
  ancestry and damage resistance, Tiefling legacy and resistance, and the Elf, Gnome,
  and Goliath lineage choices are structured creation choices.
- Species spell lineages now learn their cantrips at creation and their level-3 and
  level-5 spells at the right character levels. They are always prepared and can be
  cast once without a slot per long rest. Goliath ancestries spend their
  Proficiency-Bonus-scaled Giant Ancestry resource through `use_feature`.
- Versatile weapons use their larger die in a two-handed grip; Heavy weapons impose
  disadvantage when the wielder's Strength or Dexterity is below 13.
- Conditions apply their roll modes and numeric modifiers, including poisoned and
  exhaustion.
- Armor categories and class armor training are enforced, including shields.
- Turn resources expose Actions, Bonus Actions, and Reactions; attacks consume the
  action budget, actions and Bonus Actions are spent on the creature's own turn, and a
  Reaction is spent on any creature's turn, counted once per creature each round, and
  reset when the round ends. Class/species features declare and spend their own
  resource and turn costs through `use_feature`, and a refused reaction leaves the
  feature's use unspent.
- Class always-prepared spells appear as learned features and do not count against the
  prepared limit; Circle of the Land spells are limited to the chosen land.
- New class-feature choices at level-up are shown on the Advancement page and recorded
  with the level, instead of being silently auto-filled.
- Weapon Mastery is a real weapon choice: the SRD weapon properties are generated, the
  chosen kinds unlock their mastery, Graze resolves damage on a miss, Sap and Slow apply
  their conditions, Topple rolls its Constitution save, Vex grants advantage on the next
  attack, and Improved Critical lowers the critical range to 19.
- Rage spends its resource and Bonus Action, sets the raging mark, adds its level-scaled
  damage, and grants physical resistance while the mark is active.
- Reactions are tracked per creature and per round: a Goliath's Stone's Endurance, a
  Bard's Cutting Words, or an opportunity attack can be spent on another creature's
  turn, and each creature gets its own reaction back when the round turns over.
- Monster attack logs name the attack beside its printed bonus (`+6 Greatclub`) rather
  than labeling the statblock's whole attack bonus as a proficiency bonus.
- An unconscious or dead character cannot use features, class resources, or cast spells
  through the GM tools until healing revives it.

## Remaining limits

Multiclassing is deliberately out of scope. Everything else in the SRD character
options, spells, equipment, conditions, bestiary, and advancement paths is represented
by the rule set and uses the generic engine tools.

None of these block the core play loop: create a character, explore, start a fight,
roll attacks and saves, cast spells, take damage, heal, rest, and level up.
