# Worlds Without Number playtest plan

This is the long-form QA plan for the Worlds Without Number (WWN) rule set in the Unwritten
World campaign frame. It covers scripted checks, live AI-GM sessions, and freeform play. The
goal is not only that the fiction reads well, but that the **engine performs every mechanical
step the source requires**, so the AI GM only chooses what happens and never has to invent a
modifier, a clock, a supply cost, or a map update.

## Test environments

| Layer | Command | What it covers |
| --- | --- | --- |
| Automated | `PYTHONPATH=<worktree>/src .venv/bin/python scripts/test.py -q` | Rules, tools, event log, maps, content, contracts |
| Scripted LLM | same, with `CYGNUSLOOM_LLM_PROVIDERS=fake/scripted` | GM prompt/state/tool protocol without network variance |
| Live | uvicorn on a private SQLite DB + browser at the container IP | Real DeepSeek GM, real turn review, real streaming UI |

Never run `uv sync` inside a worktree that shares the main virtual environment. Use a separate
database per thread (`session1`, `session2`, ... ) and a separate port (8771, 8772, ...). Two
threads may run at once against different databases.

## Plan A: long-form campaign arc

Run one continuous campaign for at least ten in-game days and one level-up. Each session should
advance a real storyline, not a checklist; record the player messages and the tool events.

1. **Opening and social contract.** Create a character, meet a patron, negotiate a job, and
   record the agreement. Pass when the patron, terms, and location are all in the event log and
   the journal.
2. **Investigation.** Ask three NPCs about the same rumor. Pass when knowledge updates and
   notes are consistent, and no NPC invents facts that contradict earlier events.
3. **Travel.** Take a journey of two or more days, including a night camp. Pass when `travel`
   is used, the clock advances by the terrain-day rule, supplies are spent, and the arrival
   scene is in the right place.
4. **Site exploration.** Enter a barrow, ruin, or shrine. Explore at least twelve ten-minute
   turns. Pass when the clock advances one hour per six turns, wandering checks are rolled,
   light is spent, and the site map gains and moves tokens.
5. **Combat.** Fight at least three enemies, including one with Shock and one with a morale
   score. Pass when initiative, turns, attacks, Shock, morale, conditions, dying, stabilization,
   and the end-of-encounter state are all tool-backed.
6. **Downtime and advancement.** Rest, heal, train, level up, and start a project or faction
   action. Pass when experience, skills, spells, HP, saves, and project clocks all change
   through events.
7. **Return and consequence.** Go back to the patron. Pass when the earlier agreement, the
   bodies, the loot, and the changed locations are still true.

## Plan B: AI GM / engine division of labor

For every player message, inspect the turn's tool events and answer:

- Did the GM roll or decide something the engine could have rolled or decided?
- Did the narration claim an item, harm, condition, time passage, or location change with no
  corresponding event?
- Did the turn review catch the mismatch? If not, why not?
- Did the GM ask the player to make a choice that the rules already answer (for example, which
  modifier applies, or whether a check succeeds)?

Any "yes" is a candidate engine gap. Prefer a small engine or content fix over a longer prompt.

Every tool that refuses an action for a rule also exposes a `force` parameter. The GM sets it
only when the story needs the exception, and the tool result or narration should make that
override visible. The normal path stays binding; `force` is the escape hatch, not the routine.

## Plan C: maps and tokens

The original failure was a fight whose final cells never reached the visible local map, leaving
the map stale and the actors apparently frozen.

1. Start an encounter in a mapped place. Place the PC and at least two enemies.
2. Move every participant at least once.
3. End the encounter by defeating all enemies.
4. Switch between Encounter map and Local map.
5. Leave and re-enter the place.

Pass when tokens exist on the visible map, reflect the final cells, and survive the encounter
end. Also check that a body, a removed NPC, and a placed object do not overwrite a living token.
Watch `dist/maps.json` (or the map endpoint) across turns to confirm it changes when actors move.

## Plan D: clock, light, food, water

1. **Night rest at night.** Rest at the night part. Pass when the clock reaches the next
   morning, not a day later. (A regression test covers this.)
2. **Night rest in the morning.** Rest in the morning. Pass when it stops at dusk rather than
   running a full day.
3. **Torch.** Light a torch. Pass when one torch is removed, the light lasts six turns, and the
   GM does not light a second torch for the same period.
4. **Lantern.** Fill and light a lantern. Pass when one pint of oil is spent and the light lasts
   twenty-four turns.
5. **Rations and water.** Rest with and without supplies. Pass when a day's food and water are
   spent, and privation adds the source's System Strain and holds back recovery.
6. **Forced march.** Travel two days without a rest. Pass when the GM spends the extra days of
   food and water or records privation.
7. **Site turns.** Explore six turns. Pass when exactly one hour passes and a wandering check
   is rolled at the site's alertness.

## Plan E: combat, morale, conditions, death

1. **Initiative and sides.** Start a fight with allies and enemies. Pass when every combatant
   is on a side, initiative is rolled once, and no finished encounter is restarted.
2. **Attack and Shock.** Use a weapon with Shock against low and high AC. Pass when Shock is
   applied on a miss only when AC is low enough, thrown and ranged attacks never Shock, and a
   shield absorbs the first Shock of the round.
3. **Morale.** Drop the first NPC ally of a group and half of an enemy group. Pass when a
   morale check is rolled for each group, including allied NPCs.
4. **Conditions.** Inflict prone, grappled, unconscious, and Frail. Pass when each is an event,
   its modifier is applied, and its ending trigger is correct.
5. **Mortal wounds.** Drop a PC and a named NPC to 0 HP. Pass when both become dying on the
   six-round clock, not outright dead, and a Heal check is required before stabilization.
6. **Stabilization and recovery.** Fail a Heal check, then succeed. Pass when failure leaves the
   patient dying, success stops the clock, healing brings them to 1 HP, and `recover_character`
   clears the defeated tag on the map.
7. **Execution and nonlethal.** Try a prepared execution and a declared nonlethal attack. Pass
   when the engine enforces the setup and the resulting condition.

## Plan F: advancement, projects, factions, hirelings

1. Award experience to the next threshold and level up. Pass when the class hit die, attack
   bonus, saves, skill points, focus pick, art pick, and castings all change by the class table.
2. Learn a spell and cast it. Pass when the spell pick, castings, and Effort are correct.
3. Start and advance a project. Pass when the clock advances only through the project's own
   roll or downtime.
4. Recruit a hireling and pay wages. Pass when the hireling has a level, wage, and morale, and
   wages are charged at the right time.
5. Take a faction action. Pass when the asset, faction stats, and clock change through events.

## Freeform prompts

Use these as improvisation seeds, not scripts. After each, check the tool log against the
narration.

- "I ask the oldest person in the village what they remember about the barrow road."
- "I buy a week of food, a torch, and a pint of oil, then count my coin."
- "I walk the ridge at dusk and look for tracks."
- "I try to slip past the guard without being seen."
- "I shove the raider off the walkway."
- "I offer the walker a chance to surrender."
- "I bind the prisoner's hands and question him."
- "I camp in the open with no fire."
- "I go back to the patron and report what I found."
- "I spend the afternoon copying the damaged page."
- "I ask the smith to repair my shield."
- "I try to recall what I know about the Standing Flame."
- "I search the body, then give the letter to the warden."
- "I take the north passage and listen at the door."
- "I run for the entrance when the flame goes out."

## Pass criteria

- Every mechanical change has an event the UI can render.
- The clock, supplies, light, maps, HP, Strain, Effort, and conditions agree with the narration.
- The AI GM never asks the player to supply a modifier or a target number the engine knows.
- The turn review catches at least the state/narration mismatches that a careful human would.
- A reload of the page preserves the same state and map.

## Issue log from this playtest

| Area | Finding | Status |
| --- | --- | --- |
| Maps | Final encounter cells were not carried to the visible local map; tokens missing after a fight | Fixed: final cells copied to the local and encounter maps; placement and leaving sync tokens |
| Clock | A night rest begun in the night part jumped to the next day's morning instead of the same morning | Fixed: rest rounds to the next morning boundary without forcing an extra part |
| Combat | Thrown and ranged attacks applied Shock; total defense did not stop it | Fixed in the attack path and tests |
| Combat | Killing Blow was offered to every class | Fixed: Warrior only |
| Combat | Saves had no situational modifier | Fixed: `roll_save` accepts -20..20 |
| Rest | A night rest restored HP and Strain without food or water | Fixed: the engine spends a day's supply and records privation |
| Light | Lighting a torch or lantern did not spend the item | Fixed in data and prompt; torches are one use, oil is one pint per fill |
| Travel | Overland `move_actor` bypassed the journey tool | Fixed: WWN refuses an overland move and directs the GM to `travel` |
| Stabilization | The GM could stabilize without the Heal check | Fixed: WWN refuses the tool without a successful Heal check in the same turn |
| Named NPCs | A named NPC died outright at 0 HP because `important` was not exposed | Fixed: `create_npc` exposes `important`, and the WWN prompt requires it for named statblocks |
| Purchases | The GM narrated buying food and oil but only moved coin | Prompt now requires item events for every purchase or gift |
| Morale | An allied NPC was not checked when the PC fell | Prompt now requires a check for every NPC side, including allies |
| Site turns | A six-turn search advanced one part (six hours) instead of one hour | Prompt now forbids `parts` for site turns and states the cost explicitly |
| Refusals | New engine refusals had no story override | `move_actor`, `set_map_cells`, `place_participant`, and `stabilize` now take `force`; the GM is told to use it only when the story requires it |
