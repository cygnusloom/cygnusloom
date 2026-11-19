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

The current full suite is **3793 passed**; the two D&D files are **30 passed**; content
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

## Known gaps / next work

These are real SRD options that are not yet fully mechanical. They are documented so a
later content pass can add them without re-discovering the gaps:

- Background ability scores offer three +1s; the SRD's +2/+1 mode is not selectable.
- Origin feats are mostly prose. Magic Initiate does not yet choose cantrips, a level-1
  spell, or a casting ability; Alert and Savage Attacker have no engine effect.
- Starting equipment only grants the class A kit. The SRD's B/C gold alternatives and
  background equipment packages are not offerable.
- Species choices (Human Skillful/Versatile, Elf lineage, Dragonborn ancestry, and
  similar) are text-only and do not grant skills, feats, cantrips, or resistances.
- Versatile weapons always use their one-handed damage; there is no two-handed grip.
- Conditions are recorded and shown but do not yet apply advantage/disadvantage or the
  other numeric effects of blinded, frightened, poisoned, prone, restrained, and
  exhaustion.
- Armor training is not enforced, and the action/bonus-action/reaction split is
  narrative rather than fully validated.
- Weapon mastery, multiclassing, and some subclass-level 3 choices remain out of scope.

None of these block the core play loop: create a character, explore, start a fight,
roll attacks and saves, cast spells, take damage, heal, rest, and level up.
