# Academia Arcana — ruleset specification

A Powered by the Apocalypse magic-school ruleset for Cygnus Loom, played in the
blank `unwritten-academy` world. The GM forges the school at campaign start.

## Locked design

- **Dice:** 2d6 + stat. 10+ full success, 7–9 success with a cost, 6– GM move.
- **Stats:** Theory, Practice, Alchemy, Charm, Cunning. Starting pool −1, 0, 1, 1, 2.
- **Playbooks:** Prodigy, Scholar, Alchemist, Rebel, Socialite. Each grants +1 to one
  stat and a signature talent.
- **Years:** Players choose their starting Year (1–7); mixed-year parties are allowed.
- **Stress:** 0–5. Casting under pressure, exams, and setbacks mark Stress. At 5 the
  character suffers a Burnout: clear 3 Stress and take a Burnout condition until it is
  addressed in downtime.
- **Reputation:** Faculty Favor and Peer Standing, each −3 to +3, per character.
- **Colleges:** The GM invents 3–5 colleges at campaign start. Each has a name, a
  description, a trait, and cup points. The cup resets at the end of each school year.
- **Casting:** Casting is free. A 7–9 or a large working marks Stress; a 6– triggers a
  mishap or a GM move.
- **Year-end:** +1 stat (respecting the Year cap), 1 spell pick, 1 subject/area, and
  1 talent. No XP is tracked.
- **Classes:** Formal projects. The GM builds a challenge in a classroom, around the
  school, or on the grounds; completing the project grants the class reward.
- **Research:** Player proposes, GM frames a project and clock, completion produces a
  first-class custom spell/potion/enchantment feature.
- **Campaign options:** Time period, tone/seriousness, school type, magic level,
  academic focus, weirdness dial. Table-driven and fiction-steering.
- **World:** Blank canvas. The GM builds the academy, colleges, faculty, grounds,
  traditions, and current trouble from the setting tables.

## Year progression (linear)

| Year | Spells | Subjects | Stat cap | Training picks |
|------|--------|----------|----------|----------------|
| 1    | 2      | 1        | +2       | 0              |
| 2    | 3      | 1        | +2       | 0              |
| 3    | 4      | 2        | +2       | 1              |
| 4    | 5      | 2        | +3       | 1              |
| 5    | 6      | 3        | +3       | 2              |
| 6    | 7      | 3        | +3       | 2              |
| 7    | 8      | 4        | +4       | 3              |

Spells = Year + 1. Subjects = 1 + floor((Year − 1) / 2). Training picks raise a stat
within the Year cap. A Year 1 character who plays through Year 7 gains subjects at each
year end and may end with more subjects than a character who starts at Year 7.

## Magic library

- 40 spells, 15 potions, 15 enchantments, weighted toward utility and fun.
- Disciplines: Charms, Transfiguration, Warding, Divination, Healing, Illusion,
  Elemental. Forbidden magic exists but is not available at creation.
- Subject features grant +1 to rolls in their discipline and access to its magic.
- **Enchantments are spells placed on objects, not the objects themselves.** An
  enchantment feature names the working and the kinds of objects it can be placed
  on; the enchanted object is a separate thing with its own description, and it may
  be found, bought, stolen, or made. Placing an enchantment is the Forge an
  Enchantment move. Finding an enchanted object does not teach the working.
- Custom magic is a first-class feature: base effect + mods across potency, duration,
  range, targets, Stress cost, drawback, component, special effect, and casting time.
  An invented enchantment is the working; the object it is placed on is separate.

## School life

Dueling club, school sport, familiars/pets, signature spell, forbidden magic,
detention/curfew, college cup, friends/rivals, magical creatures, and festivals. These
use generic moves, projects, relations, counters, prompts, and tables rather than
ruleset-specific Python.

## Generic engine capabilities

1. **Custom features.** Create, update, and list character features that are not in
   the loaded content. Visible in feature lookups, the character sheet, and the
   compendium.
2. **Factions/colleges.** Create, update, and list campaign factions with a name,
   description, trait, and points. Year-end resets their points.
3. **`magicschool` genre.** Add it to the table genres so the new tables can be
   offered without leaking into other rulesets.

## Todo list

### Engine
- [x] Add `magicschool` to `TABLE_GENRES`.
- [x] Add generic custom-feature events, state, tools, lookups, sheet, and compendium.
- [x] Add generic factions/colleges events, state, tools, prompts, and compendium.
- [x] Add period support for resetting dynamic faction tallies.
- [x] Add feature `stat` effect cap support for Year training picks.
- [x] Add JSON feature reactions, including move band raises, uses, and period/clock resets.
- [x] Make Natural Gift and Signature Spell trigger automatically and reset each school week.

### Content
- [x] `rule_set.json`, `stats.json`, `character_fields.json`.
- [x] Seven Year templates and five playbook templates.
- [x] Seven subject features, fifteen training features, playbook talents.
- [x] 40 spells, 15 potions, 15 enchantment workings.
- [x] Moves, rules, conditions, counters, advancements, periods, relations, projects.
- [x] Tool calls: moves, advancement, periods, tallies, projects, custom features,
      factions.
- [x] GM prompts for classes, research, colleges, year end, and school life.
- [x] `magicschool` tables: campaign options, school/college/faculty/clique/lesson/
      exam/mishap/secret/festival/rival/creature/artifact/enchanted-object/research
      tables.
- [x] 60+ adventure hooks.
- [x] `unwritten-academy` world with a blank canvas and an academy-forge prompt.

### Verification
- [x] Loader tests for the new ruleset and world.
- [x] Character creation tests for Years, playbooks, subjects, and stats.
- [x] Custom feature and college tests.
- [x] Year-end advancement and cup reset tests.
- [x] Reaction trigger, use, and weekly reset tests.
- [x] Full suite, lint, and a live UI run.
