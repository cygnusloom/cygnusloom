# Creativity, oracles, and human-written modules

Status: implemented. The audit, the design, and the module system below are in the code base.
The audit tables record the state **before** the change; the implementation notes at the end of
each section describe what actually shipped and where it differs from the proposal. The automated
suite covers the loader, runtime tools, prompts, UI, export/import, Twin Lakes, and a sample mini
module.

## What shipped

- Creative tables have a `role` (`oracle`, `setting`, or `module`), `roll_creative` defaults to
  `oracle`, and a core state prompt tells the GM when to roll. `setting_forge` asks for `setting`.
- `data/modules/` loads mini and campaign modules, with embedded or synthetic settings, inline
  entries, tag filtering, compatibility checks, and validation.
- Campaigns record their starting module in `Campaign.module`; attached campaign modules and used
  mini modules are event-sourced (`module.attached`, `module.detached`, `module.used`).
- The GM has `list_modules`, `lookup_module`, and `use_module`. The state/opening prompt carries a
  compact module brief; narration/review do not.
- New Campaign has a campaign-module picker and makes the world optional when the module brings a
  setting. Settings has a Story modules panel. The compendium labels module settings and lists
  mini modules.
- Twin Lakes is a campaign module whose embedded setting still resolves as `world_template:
  "twin-lakes"` for old campaigns and exports.
- `the-hermit-of-ash-hollow` is a sample mini module using inline entries.
- Export/import carries `module`; old exports infer it from the embedded world.

## Summary

Cygnus Loom already has three kinds of creative input:

1. **Creative tables** (`roll_creative`) for world generation and fresh ideas.
2. **Storylines** for the live, player-facing plot threads the GM keeps current.
3. **Authored world content**: locations, NPCs, monsters, rules, and hooks in a world template.

The audit found a sharp split:

- Creative tables are used at the opening of an **unwritten world**, and almost nowhere else.
  One 125-turn campaign recorded exactly one creative roll, at world generation, and no creative
  rolls after it.
- An **authored world** such as Twin Lakes gets no creative-table prompt at all. A live Twin Lakes
  campaign with three deliberately directionless turns made zero creative rolls, while still
  creating and updating storylines correctly.
- The default `brief` tool-schema mode cuts `roll_creative`'s description to its first sentence:
  "Roll on the creative tables for a fresh idea." The "use it when the player is directionless"
  guidance never reaches the model.
- `roll_creative` with no table list now rolls **every** creative table, including the setting
  tables. In the live prompt-only experiment the GM did exactly that in a town scene and got a
  world premise, region, faction, conflict, mystery, and hook mixed into a local stall.
- Storylines are the system that actually carries long-form continuity. One campaign recorded
  3 storyline creations, 80 updates, and 1 resolution across 125 turns. Human-written modules
  should feed and use that system, not replace it.

The design adds two module kinds:

- **Mini modules**: tagged, self-contained story ingredients (one NPC in a hut, a side job, a
  mystery, a small arc). The campaign's creative tags choose the pool, and the AI GM may select
  one whenever the story needs a new thread.
- **Campaign modules**: full adventures. One may be the campaign's starting point, acting like a
  world does today; the owner may also attach one in Settings later. They are never loaded
  automatically.

Modules are JSON content definitions, can be portable or tied to a world/rule set, and are exposed
to the AI GM as compact cards plus on-demand `lookup_module` text. The engine records which
modules are attached or used for bookkeeping, but **does not track plot progress**: storylines and
notes remain the live record. Players see module entries in the compendium like world entries;
in play, only the AI GM sees the module material.

Twin Lakes should become the first campaign module. Its existing world becomes the module's
embedded setting, and its outcome rules become module endings/fronts. Existing campaigns and
exports that name `world_template: twin-lakes` must keep working.

## Current state

> This section is the pre-implementation audit snapshot. It explains why the change was made;
> the implementation notes at the end of each design section describe the shipped behavior.

### Creative tables

- `src/cygnusloom/services/dice_tables.py` has `roll_tables` and `roll_creative`. `roll_creative`
  takes an optional list of table ids; with no list it rolls every table whose `creative` flag is
  true.
- Creative tables live in rule set and world content. Shipped examples:
  - `src/cygnusloom/data/rulesets/core/creative_tables.json`: the five Ironsworn-derived oracle
    tables `action`, `theme`, `plot_twist`, `location`, and `location_descriptor`.
  - `src/cygnusloom/data/rulesets/core/worldgen_tables.json`: the six setting tables
    `setting_premise`, `region_identity`, `faction`, `conflict`, `mystery`, and `adventure_hook`.
  - `src/cygnusloom/data/rulesets/academia-arcana/magicschool_tables.json`: the academy tables.
  - Worlds and rule sets can add their own `creative_table` definitions.
- `DiceTable` carries `creative` and `genres`. `list_dice_tables` shows id, name, dice, and the
  creative flag, but not the genre or the purpose of the table.
- `roll_creative` is a core tool, available in every campaign and not gated behind a tool group.
  Its full description says to use it when the player is directionless, but the default
  `tool_schema_mode` is `brief`, and `brief()` keeps only the first sentence. The model sees the
  first sentence only.

### Where the GM is told to use creativity

- Core prompt `setting_forge` (`unwritten_only: true`) is used only for a world with no authored
  setting. It says, among other things, "Roll every creative table with roll_creative to get a
  premise, a region, a faction, a conflict, a mystery, and a hook."
- Core prompt `setting_continuity` keeps an invented setting consistent. It does not mention
  creative tables.
- Academia Arcana has its own academy-forge prompt that supersedes `setting_forge` and names its
  tables.
- No state-step prompt tells the GM to use creative tables when a scene stalls.
- The core checklist (`src/cygnusloom/data/rulesets/core/checklist.json`) has areas for
  characters, cast, summaries, movement, notes, storylines, objects, time, rules, advancement,
  encounters, factions, magic, maps, and market. It has no area for creative momentum.
- Authored worlds use `opening_prompt` + the world's `start.hooks`; no creative tables are
  involved.

### Storylines

- Storylines are event-sourced (`storyline.created`, `storyline.updated`, `storyline.focused`,
  `storyline.resolved`, `storyline.deactivated`).
- A storyline has a player-facing name and summary plus a private plot, characters, locations,
  objects, notes, and a resolution. The focused storyline is the main plot of the moment; at most
  three are active.
- The Story tab shows only the player-facing fields and the resolution.
- Storylines are already the de facto long-form plot tracker. They are the right place for module
  threads to live.

### Authored worlds as adventures

Twin Lakes is already a full adventure encoded as a world:

- 35 locations, 14 NPCs, 5 monsters, 8 features, 21 routes, 12 regions, and 16 rules.
- Its `world.json` has a `start` location and two hooks.
- Its `rules.json` carries adventure-specific outcomes such as "If Vuk is Reborn" and "If the
  Gate has Opened", plus location and monster rules.
- The campaign seeds every authored location and NPC at creation (`seed_campaign`), and the GM
  reads the rules when the attached entity is present.

That works, but it has no structure for scenes, clues, fronts, endings, or a module-level brief.
The outcome rules are prose attached to world entities, not a reusable adventure format.

### No module type

There is no `module` definition, no module loader, no module picker, no module runtime state, and
no module tools. The closest concepts are world-authored `rules` and runtime `storylines`.

## Audit

### Method

- **Static review** of the prompt assembly, tool schema, content loader, creative tables,
  storyline tools, checklist, and world content.
- **Historical audit** of the campaign exports in `dist/`. All exports are forks or copies of one
  long campaign lineage, so they are not independent samples. The cleanest one is
  `dist/known-good.json`.
- **Live audit** with DeepSeek (`deepseek/deepseek-flash`) on a throwaway database, current code:
  - Twin Lakes + Cairn 2e: opening plus three directionless turns.
  - Unwritten World + Worlds Without Number: opening plus the same three turns.
  - The messages were deliberately stall-shaped: sit somewhere warm and see what happens; ask
    around for rumours; wait and watch the road.
- **Prompt experiments** on a temporary content copy, no code changes:
  - Prompt-only: add a `creative_stall` state prompt to Cairn 2e.
  - Prompt + checklist: also add a `creative` checklist area to the core checklist.
  - Same Twin Lakes scenario and messages.

### Results

Historical, `dist/known-good.json`:

| Measure | Value |
| --- | --- |
| Turns | 125 |
| Events | 3,698 |
| GM replies | 128 |
| Creative rolls | 1 (seq 17, opening, all 11 creative tables) |
| Creative rolls after the opening | 0 |
| Storyline creations | 3 |
| Storyline updates | 80 |
| Storyline resolutions | 1 |

Other table rolls in that campaign were procedural (`dungeon_event`), not creative rolls.

Live, current code, three directionless turns:

| Scenario | Opening creative rolls | Mid-play creative rolls | Storyline activity |
| --- | --- | --- | --- |
| Twin Lakes + Cairn 2e | 0 | 0 | 2 created, 1 updated |
| Unwritten World + WWN | 1 roll of the 6 setting tables | 0 | 3 created, 1 updated |

The Twin Lakes GM improvised a coherent town and hooks but never reached for the creative system.
The Unwritten World GM rolled exactly the six setting tables named by `setting_forge`; it did not
roll the five core oracle tables. Older exports rolled all eleven at the opening, so current
prompt interpretation has narrowed what "every creative table" means.

Prompt experiments on Twin Lakes, same three turns:

| Scenario | Mid-play creative rolls | What happened |
| --- | --- | --- |
| Baseline | 0 | No creative input |
| Prompt-only | 2 | One no-argument roll (all 11 tables) and one named `action, theme, plot_twist` roll; the narration introduced a returning trapper and dead animals around a black spring |
| Prompt + checklist | 3 | Named `action`/`plot_twist` rolls on all three turns; the narration introduced a returning logger and a wire-snare cart trap |

The prompt-only experiment is enough to revive mid-play use. The checklist makes the GM consider
creativity every turn, which produced a roll on every turn, including a quiet social scene; the
sample also had more latency, though model variance is high. It is not the recommended default.

### Findings

1. **The creative subsystem is a worldgen tool in practice.** It fires once when the setting is
   invented and then goes dormant. It is not part of the GM's normal play loop.
2. **Authored worlds have no mid-play creativity reminder.** The only prompt that mentions
   `roll_creative` is `unwritten_only`, so published worlds get nothing.
3. **The default tool schema hides the usage guidance.** The model sees "Roll on the creative
   tables for a fresh idea", not "use it when the player is directionless."
4. **The no-argument roll is too broad.** It now includes setting tables, academy tables, and
   world-specific tables. In a local scene it can inject a region, faction, conflict, and mystery
   that contradict the established world.
5. **The tool description and the data disagree.** The description names five core tables, but
   `roll_creative` with no arguments rolls every creative table, and the worldgen tables are not
   mentioned.
6. **The `setting_forge` wording is ambiguous.** "Roll every creative table" plus a list of six
   setting outputs led the live model to roll only those six, while historical runs rolled all
   eleven. The intent should be explicit.
7. **There is no phase or role metadata.** The GM cannot tell an oracle table from a setting
   table from a module-specific table.
8. **Storylines work and are used heavily.** They are the correct live record for module threads.
9. **Twin Lakes is a module in everything but name.** Its content already includes a start, cast,
   places, regional map, monsters, and outcomes; it only lacks the module structure.
10. **The checklist should not be the first creativity fix.** A prompt is sufficient in the
    experiment; a checklist item risks over-triggering and latency.

## Goals

- Make the existing creative tables useful **during play**, not only at world generation.
- Make it obvious which tables are for worldgen, which are for scene oracles, and which belong to
  a module.
- Add a JSON format for human-written story material that scales from one NPC in a hut to a full
  campaign module.
- Let a campaign start from a campaign module instead of a world, or from a module plus a world.
- Let the campaign owner attach a campaign module later, and let the AI GM select tagged mini
  modules whenever the story needs one.
- Keep the AI GM's context small by showing module cards and reading module sections on demand.
- Keep plot tracking in the existing storyline and note systems. The engine tracks only module
  attachment and one-shot use, for bookkeeping and export.
- Keep module material out of the players' in-game view; expose module entries in the compendium
  as the equivalent of world entries.
- Convert Twin Lakes into the first campaign module without breaking old campaigns or exports.
- Make module content generic and data-driven; no Python in modules.

## Non-goals

- A separate scene-progress tracker, chapter UI, or module completion percentage. Storylines and
  notes are the progress record.
- A GUI module editor or a user-uploaded module marketplace. Modules are bundled JSON content for
  now.
- Markdown module bodies. Authors write JSON; prose lives in JSON string fields.
- Automatic selection of a full campaign module by the AI GM. Only the owner attaches campaign
  modules; the GM may attach or use mini modules.
- Automatic scene ordering, gated clues, or forced endings. Modules are material, not a script.
- Replacing the existing world format. A world remains a setting; a campaign module may embed one.
- Importing arbitrary third-party modules with Python hooks. World and rule set content can
  import Python; modules must not.

## Decisions

### Two module kinds

- **Mini module**: a tagged, self-contained story ingredient. It can be as small as one NPC and
  one location or as large as a short side quest. Mini modules are not loaded at campaign start;
  the AI GM may select one from the campaign's tagged pool at any time.
- **Campaign module**: a full adventure with a premise, start, cast, places, plot material, and
  optional endings. It can be chosen as the campaign's starting point, or attached later by the
  campaign owner in Settings. It is never loaded automatically.
- Both use the same JSON definitions and the same module tools. `kind` controls the default
  behavior and the UI: mini modules are in the GM's pool; campaign modules require an owner
  action.
- Modules can be any size. "Mini" and "campaign" describe how they are selected, not a hard
  content limit.

### Modules are material, not a state machine

- A module contains **story cards**: scenes, cast, places, clues, fronts, and endings.
- The GM decides when to use a card, whether to reorder or skip it, and what the outcome is.
- A module can suggest `next` scenes and `if_ignored` consequences, but the engine does not
  enforce them.
- The only runtime module state is bookkeeping:
  - Which campaign modules are attached.
  - Which mini modules have been used, so a one-shot mini module is not offered twice.
  - That is all. Plot progress stays in `storyline.*` events and notes.
- When a module thread becomes something the players are following, the GM creates a runtime
  storyline from the module's seed. The GM updates and resolves it like any other storyline.

### Who chooses what

- **Player, at campaign creation**: the rule set, the starting campaign module (or none), the
  optional world, and the creative tags that select available mini modules.
- **Owner, in Settings**: attach or detach campaign modules; change the creative tags; see which
  mini modules have been used. The owner cannot force the GM to use a module card.
- **AI GM**: select and use mini modules from the tagged pool; read module sections; create
  storylines, NPCs, locations, notes, and other state from the material. The GM cannot attach a
  campaign module.
- **Compendium**: shows module entries like world entries. No in-game module tab.

### Campaign module as the starting point

- A campaign module may embed a world definition (Twin Lakes does). In that case it can be
  selected alone; it acts like a world for seeding, opening, content lookups, and the campaign
  header.
- A campaign module with no embedded world is portable and needs an authored world selected at
  campaign creation. To use a portable module in an unwritten world, play the setting-forge
  opening first and attach the module later, when its places can be chosen in the invented
  setting.
- A world may also be selected alongside a campaign module. The selected world is the base
  setting; the module's embedded world and definitions layer on top. This supports a module that
  adds a region or a situation to an existing setting.
- At least one of the two must provide a setting (locations, NPCs, regions, or definitions) when
  the campaign starts. The New Campaign form enforces it.
- If a campaign module has a `start`, it supplies the opening location and hooks instead of the
  world's `start`; otherwise the world's start is used. The opening prompt also gets the module's
  opening card, but not its later cards.

### Compatibility and merging

- A module declares:
  - `rule_set` or `recommended_rule_sets`: the rules it is built for, if any.
  - `world` or `recommended_worlds`: the world it is built for, if any.
  - No declaration means portable; it must then avoid statblocks that assume a rule set.
- If the module directory contains a `world` definition with the id named in `world`, that world
  is embedded in the module. Otherwise `world` names an external world template. A module may
  have an embedded world and no `world` field; the embedded world's id is then its setting.
- Module content is merged into the campaign's world content. Precedence is:
  rule set < selected world < module. A module entry with the same id replaces the world entry.
  The `replaces` list is parsed for future validation but is not enforced in the shipped merge;
  the merge is id-based and deliberate.
- Duplicate names with different ids are allowed but reported by `content validate` as a warning,
  because two places or people with the same name confuse the GM.
- A module that includes statblocks is validated against every rule set it declares. A portable
  module with statblocks must declare at least one rule set.
- A campaign module embedded world is registered under its world id, so old references such as
  `world_template: "twin-lakes"` keep working. The registered world carries the module id, so
  the compendium can label it as a campaign module and deduplicate it.
- If a module defines a `world` but its `world` field names a different id, validation fails;
  that is almost always a copy/paste mistake.

### Tags and selection

- Reuse the campaign's existing creative-tag selection (`table_genres`) as the coarse filter for
  both creative tables and mini modules. The UI label can become "Creative tags" while the stored
  field stays `table_genres` for backwards compatibility.
- A mini module declares `genres` (and optional finer `tags`) matching the same vocabulary.
- `list_modules` filters the pool by:
  - the selected creative tags (no selection means all),
  - rule set compatibility,
  - world compatibility,
  - one-shot use,
  - optional `tags` from the GM.
- A campaign module is never filtered by tags; the player/owner explicitly chooses it.
- The tag vocabulary should be content-defined (`data/tags.json` or an equivalent module-level
  block), not a hard-coded Python tuple, so authors can add genres, tones, and setting tags
  without an engine change. The existing `TABLE_GENRES` order can seed the first vocabulary.

A vocabulary file:

```json
{
  "type": "tag_vocabulary",
  "genres": ["generic", "fantasy", "horror", "mystery", "science-fiction", "modern"],
  "settings": ["urban", "wilderness", "dungeon", "sea", "court", "frontier", "academy", "road"],
  "tones": ["dark", "hopeful", "comic", "tragic", "romantic", "pulp", "grim", "whimsical"],
  "lengths": ["moment", "scene", "side-quest", "adventure", "campaign"],
  "threats": ["beasts", "undead", "human", "fae", "cosmic", "environmental", "political"],
  "themes": ["revenge", "redemption", "discovery", "survival", "loyalty", "betrayal"]
}
```

The campaign-level selection is a subset of `genres`, stored in the existing `table_genres` field.
`list_modules` also accepts the finer `settings`, `tones`, `lengths`, `threats`, and `themes` tags
for on-demand filtering.

> Implementation note: the shipped version keeps the vocabulary in `TABLE_GENRES` and extends it
> with `mystery`. `genres` is validated at load time; the other tag categories are free-form and
> are matched case-insensitively against the same category or any category. A separate
> `data/tags.json` vocabulary is still future work.

### Visibility

- **Compendium**: modules are listed like worlds. Show the module name, blurb, source/credits,
  tags, and the same public entries a world shows: locations, NPCs, monsters, features, items,
  and rules where the world compendium would show them. Do not show `premise`, `truth`,
  `gm_notes`, scenes, clues, fronts, or endings.
- **In play**: no module tab, no module name in the campaign header, no module progress. The GM
  sees the module brief and cards in the state step; the narration step does not. Module
  storylines appear on the Story tab only after the GM creates them.
- **Spoilers**: the compendium choice is intentional; it matches today's world behavior, where
  authored locations and NPCs are listed before they are discovered. GM-only module text stays
  out of both the compendium and the narration.

### Context budget

- The state step carries a compact module brief per active campaign module:
  - name, kind, tags, one-paragraph premise, tone, and a one-line "how to bring it in";
  - indexes of scenes, cast, places, clues, fronts, and endings, with ids and short names;
  - the opening card in full at campaign start;
  - a note to call `lookup_module` for anything else.
- Mini module cards are not in the prompt every turn. The prompt says how many are available and
  that `list_modules` will show them. `use_module` returns the chosen mini module's brief and
  material.
- `lookup_module` returns one section at a time, with a word cap. `all` is allowed only for a
  small mini module.
- The module block is built only for the **state and opening steps**. The narration and review
  steps get the ordinary world state and turn log, without module cards or GM-only module text.
  This is both a token saving and a spoiler protection.

## Content model

Modules live beside worlds and rule sets:

```
src/cygnusloom/data/
  modules/
    twin-lakes/
      module.json
      world.json          # optional embedded setting
      locations.json
      npcs.json
      monsters.json
      features.json
      rules.json
      tables.json
      creative_tables.json
      scenes.json
      clues.json
      fronts.json
      endings.json
      hooks.json
    the-hermit-of-ash-hollow/
      module.json         # a mini module can put small entries inline
```

A module directory is read like a world directory: every `*.json` file contains one definition or
a list of definitions, dispatched by `type`. The module loader consumes the module-only types and
feeds the world-like types into an embedded or merged world content. A small module may put its
definitions in an optional `entries` array in `module.json`; each entry is treated exactly like a
definition in a separate file. Large modules should split them out.

### Definition types

| Type | Purpose |
| --- | --- |
| `module` | The manifest: id, kind, name, blurb, tags, compatibility, premise, truth, GM notes, start, links |
| `module_scene` | One story card: a situation, its goal, cast, place, clues, complications, if ignored, outcomes, suggested next cards |
| `module_clue` | A fact, object, or testimony that can be found; what it reveals and where it leads |
| `module_front` | A threat or faction plan with segments and effects; the GM tracks it with notes |
| `module_ending` | A suggested conclusion: when it applies, what happens, rewards, consequences, next module |
| `module_hook` | A way to bring the module, scene, or thread into play |
| `module_storyline` | A seed for a runtime storyline: player-facing name and summary plus the private plot |
| `world` | Optional embedded setting, with the normal world fields |
| `location`, `npc`, `region`, `route` | Normal world content, usable by the module |
| `monster`, `feature`, `rule`, `table`, `creative_table` | Normal content, validated against the rule set |

The normal definitions keep their existing schemas. Module-only metadata about them lives in the
module manifest's link tables, so a module can say "Flavia is the patron, knows the poisoned-river
clue, and appears in these scenes" without changing the NPC schema.

### Module manifest

A campaign module manifest:

```json
{
  "type": "module",
  "id": "twin-lakes",
  "kind": "campaign",
  "name": "Twin Lakes",
  "blurb": "A mystery around the Twin Lakes: a butcher has vanished, and a tithingman is overdue with the tithe.",
  "icon": "water",
  "tags": {
    "genres": ["fantasy", "mystery"],
    "settings": ["frontier", "wilderness"],
    "tones": ["grim"],
    "lengths": ["campaign"]
  },
  "rule_set": "cairn2e",
  "world": "twin-lakes",
  "third_party": true,
  "source": "Twin Lakes, by ... (see CREDITS.md)",
  "premise": "Two disappearances in a fading lake town lead into a drowned temple, a sect, and a gate under the Stone Forest.",
  "truth": "Reeve Trellis is hiding the tithe record; Vuk's cult is trying to reopen the Gate under Deadmill.",
  "gm_notes": "This is a sandbox mystery. Let the players choose either hook or both. The region is dangerous at night. Keep the Tideborn and the Order of Nine distinct.",
  "tone": "Cold water, old debts, and a town that has learned not to ask.",
  "themes": ["obligation", "drowning", "old power"],
  "start": {
    "location": "isthmus-town",
    "hooks": "- The Tideborn have agreed to pay ...\n- The Cities are offering ..."
  },
  "cast": [
    {"npc": "flavia", "role": "patron", "knows": ["river-black"], "appears_in": ["arrival", "the-butcher"]},
    {"npc": "reeve-trellis", "role": "obstacle", "knows": ["tithe-record"], "appears_in": ["arrival", "the-stuffed-inn"]}
  ],
  "places": [
    {"location": "isthmus-town", "role": "hub", "appears_in": ["arrival"]},
    {"location": "deadmill", "role": "dungeon", "appears_in": ["the-lock"]}
  ],
  "storylines": ["aldra", "the-tithe"],
  "scenes": ["arrival", "the-stuffed-inn", "the-butcher", "up-the-river", "the-lock"],
  "clues": ["river-black", "tithe-record", "gate-mark"],
  "fronts": ["vuk-reborn", "gate-opened"],
  "endings": ["vuk-stopped", "vuk-reborn", "gate-opened", "both"],
  "replaces": []
}
```

A mini module can be one file:

```json
{
  "type": "module",
  "id": "the-hermit-of-ash-hollow",
  "kind": "mini",
  "name": "The Hermit of Ash Hollow",
  "blurb": "A reclusive herbwife in a burnt-out hut knows what happened at the mill.",
  "icon": "campfire",
  "tags": {
    "genres": ["mystery", "fantasy"],
    "settings": ["wilderness"],
    "tones": ["grim"],
    "lengths": ["scene"]
  },
  "repeatable": false,
  "premise": "Mara Ash saw the miller's son set the fire and has been paid to keep quiet.",
  "gm_notes": "Mara is not a villain. She is protecting a grandchild. She will trade the truth for a promise of safety.",
  "cast": [{"npc": "mara-ash", "role": "witness"}],
  "places": [{"location": "ash-hollow-hut", "role": "scene"}],
  "scenes": ["the-bargain"],
  "clues": ["the-millers-purse"],
  "entries": [
    {
      "type": "npc",
      "id": "mara-ash",
      "name": "Mara Ash",
      "location": "ash-hollow-hut",
      "description": "A herbwife in her sixties, burned across one hand. She saw the fire and took the miller's money.",
      "personality": "patient and guarded",
      "story_only": true
    },
    {
      "type": "location",
      "id": "ash-hollow-hut",
      "name": "Ash Hollow Hut",
      "summary": "A low turf hut with a crooked chimney, set in a black ring of burnt stumps.",
      "description": "The hut smells of peat smoke and dried yarrow. A child's wooden boat sits on the sill."
    },
    {
      "type": "module_clue",
      "id": "the-millers-purse",
      "name": "The Miller's Purse",
      "summary": "A purse of hush-money with the miller's mark on the clasp.",
      "reveals": "The miller paid Mara to keep quiet."
    },
    {
      "type": "module_scene",
      "id": "the-bargain",
      "name": "The Bargain",
      "summary": "Mara will name the arsonist if the party can get her grandchild out of the miller's service."
    }
  ]
}
```

The optional `entries` array is convenience for small modules; it is never mixed with the link
lists (`cast`, `places`, `scenes`, `clues`, `fronts`, `endings`, `storylines`), which name ids of
definitions in `entries` or separate files.

### Scene cards

A scene is optional structure. It is not a gate, and every field except `id`, `name`, and
`summary` is optional:

```json
{
  "type": "module_scene",
  "id": "the-bargain",
  "name": "The Bargain",
  "summary": "Mara will name the arsonist if the party can get her grandchild out of the miller's service.",
  "location": "ash-hollow-hut",
  "cast": ["mara-ash"],
  "goal": "Trade safety for the truth.",
  "setup": "Mara has already denied seeing anything. The child is in the miller's kitchen.",
  "clues": ["the-millers-purse"],
  "complications": [
    "The miller's men arrive while the bargain is being struck.",
    "Mara's grandchild does not want to leave."
  ],
  "if_ignored": "The miller moves the child out of the region, and Mara stops speaking to strangers.",
  "outcomes": [
    "Mara names the son; the party gains a witness.",
    "Mara keeps silent; the purse is the only proof."
  ],
  "next": ["the-millers-house"],
  "tags": ["social", "investigation"]
}
```

### Fronts and endings

A front is a suggested consequence clock, not engine state. The GM can record it with notes and
use `advance_time`/storyline updates as usual:

```json
{
  "type": "module_front",
  "id": "gate-opened",
  "name": "The Gate Opens",
  "summary": "If no one stops Annafranax, the Gate under Deadmill opens two days after the adventure begins.",
  "segments": [
    "The water in East Lake turns black.",
    "The Deadmill grove begins to sink.",
    "A sinkhole opens; creatures from the Roots come through."
  ],
  "advance_on": ["two days pass", "the party ignores the mill", "the ritual is completed"],
  "effects": ["The region becomes a frontier; Isthmus Town becomes a military outpost."]
}
```

An ending is a suggested conclusion:

```json
{
  "type": "module_ending",
  "id": "vuk-stopped",
  "name": "Vuk Is Stopped",
  "summary": "Vuk is prevented from returning, and the Gate remains closed.",
  "when": "The party stops the rebirth and leaves the Gate sealed.",
  "outcome": "Isthmus Town recovers; the Stone Forest's secrets draw explorers.",
  "rewards": ["The Tideborn's gratitude", "The Order of Nine's attention"],
  "next_module": null
}
```

### Module storyline seeds

A module storyline seed gives the GM a ready-made player-facing name and summary plus the private
plot, so it can create a runtime storyline without inventing one:

```json
{
  "type": "module_storyline",
  "id": "aldra",
  "name": "Aldra's Whereabouts",
  "summary": "The Tideborn will pay to learn what became of Aldra, and more if his body is returned for burial.",
  "plot": "Aldra was investigating the poisoned river. Flavia suspects the Tideborn's own rites are being disturbed.",
  "characters": ["Flavia", "Aldra"],
  "locations": ["The Butcher", "West Lake"],
  "objects": ["Aldra's lantern"],
  "notes": "Flavia pays only for real news, not guesses.",
  "focus": true,
  "scenes": ["arrival", "the-butcher", "west-lake"]
}
```

The GM still chooses when to create the runtime storyline. At a campaign start, the opening prompt
tells it to create storylines from the module's hooks, as it does for worlds today.

### Authoring guidance

Modules are written for an AI game master, not read aloud to players. Write facts the GM can use:

- Give every scene a **goal**, **two or three complications**, and **what happens if it is
  ignored**. Do not write boxed text or a scripted conversation.
- Give every NPC a **want**, a **secret**, and a **way to be found again**. The existing NPC
  description is a good place for the secret; the link table's `role` and `knows` fields help the
  GM cast them.
- Give every clue a **source** and a **lead**. A clue that only says "the butler did it" is less
  useful than one that points at a place, a person, or the next scene.
- Keep player-facing fields (`name`, `summary`, `blurb`) free of secrets. The compendium and the
  Story tab can show them.
- Use **links**, not duplicates. A scene should reference a location or NPC id, not repeat the
  location's description. The location/NPC record stays the single source of truth.
- Write **outcomes as possibilities**, not requirements. "If the players bargain, Mara names the
  son; if they threaten her, she lies" is more useful than a single fixed ending.
- Include a short **GM note** with the module's tone, its content warnings, and any table rules
  the GM must respect. Keep it to a few sentences; the GM reads it every turn the module is
  active.
- Do not include Python, network calls, or external files. A module is data.

## Runtime design

### Campaign fields

Add to `Campaign`:

- `module`: the id of the starting campaign module, or empty. Immutable after creation, like
  `world_template`.
- `world_template`: remains the base setting world. It is the selected world if one was chosen,
  otherwise the starting module's embedded world. A portable module with no selected world is
  invalid at campaign creation. The module's own world content layers on top of the base.
- The mini-module pool uses the existing `table_genres` selection in the first version; no new
  `module_tags` field is required. If authors later need finer selection than genres, add a
  separate field without changing the module format.
- No `modules` column: attached campaign modules are read from the event fold, which is the
  source of truth. `Campaign.module` is only the creation-time choice.

Export/import keys: `module` plus the module events. Old exports without `module` infer it from
an embedded module world, so `world_template: "twin-lakes"` keeps working.

### Events

The shipped internal events are:

- `module.attached`: a campaign module was attached by the owner. Payload: module id and role
  (default `campaign`). The starting module is **not** recorded as an attach event; it lives in
  `Campaign.module`.
- `module.detached`: a campaign module was detached. No world content is deleted; the GM's
  notes/storylines remain.
- `module.used`: the GM brought a mini module into play, or began using an attached campaign
  module. Payload: module id and reason. A one-shot mini module is not offered again.

Fold `CampaignState` with:

- `modules: dict[str, str]` for modules attached after creation, by module id.
- `used_modules: set[str]`.
- The module content itself is not copied into state; it is loaded from the content library by
  id.

### Starting a campaign from a module

1. The New Campaign form selects a rule set, an optional campaign module, an optional world, and
   creative tags.
2. The app resolves the setting:
   - module with an embedded world, no selected world: use the embedded world;
   - module with an embedded world plus a selected world: merge the selected world and the
     module's world, module entries win on id collisions;
   - module without an embedded world: require a selected world and merge the module's content
     on top;
   - no module: today's world-only behavior.
3. The app seeds the merged world content as today (`seed_campaign`). The module's hook/opening
   card is part of the opening prompt; the module's later cards are not.
4. The campaign records the selected module id in `Campaign.module`. No `module.attached` event
   is recorded for the starting module.
5. The opening prompt:
   - uses the module's `start` location and hooks when it has them, otherwise the world's;
   - includes the module's opening card (one scene);
   - tells the GM to create storylines from the hooks and to keep the module's later material
     private;
   - does not include the module's later scenes, clues, or endings.

### Attaching a campaign module later

1. The owner opens Settings -> Story modules, chooses a compatible campaign module, and attaches
   it.
2. `module.attached` is recorded. The module's setting is merged into the campaign world for
   lookups (module entries win on id collisions). The engine does **not** seed its locations or
   NPCs as events at attach time; the GM creates them from the module's cards when the story
   reaches them, which keeps a later attach from adding a whole region to the world unasked.
3. The next state step's module brief includes the new module, its premise, and its card index.
   The prompt says to bring it in when it fits, not to derail the focused storyline.
4. The GM can call `lookup_module` for the module's scenes, cast, places, clues, fronts, and
   endings.
5. Detaching stops the module brief and card offers. Content the GM already created remains in
   the campaign as ordinary world content.

### Using a mini module

1. The GM decides the scene needs a new thread, place, person, or complication.
2. It calls `list_modules(tags?, setting?, random?)`. The result is a short list of cards that
   fit the campaign's tags, rule set, world, and unused one-shot modules.
3. It calls `use_module(module, reason?)`. For a mini module the result is its brief and all its
   material (capped): premise, GM notes, cast entries, places, scene cards, clues, and suggested
   outcomes. The engine records `module.used`; a one-shot module leaves the pool. For an attached
   campaign module, `use_module` returns only the brief and marks it used; its individual cards
   are read with `lookup_module`.
4. The GM brings the material in with the existing tools: `create_location`, `create_npc`,
   `add_note`, `create_storyline`, `place_object`, and so on. It does not seed the whole module
   automatically.
5. If the players engage, the GM creates a storyline from the module's storyline seed and keeps
   it current. If they walk away, the module is still marked used; the material may return later
   as a consequence.

### The module block in the state prompt

A compact block, one per active campaign module:

```
Module: Twin Lakes (campaign)
Premise: Two disappearances in a fading lake town lead into a drowned temple,
a sect, and a gate under the Stone Forest.
Truth: Reeve Trellis is hiding the tithe record; Vuk's cult is trying to reopen the Gate.
Tone: Cold water, old debts, and a town that has learned not to ask.
Themes: obligation, drowning, old power
GM notes: This is a sandbox mystery. Let the players choose either hook or both. ...
Opening hooks: - The Tideborn have agreed to pay ...
Scenes: arrival (Arrival in Isthmus Town); the-two-offers (The Two Offers); ...
Clues: poisoned-river (The Poisoned River); ...
Cast: flavia (Flavia); reeve-trellis (Reeve Trellis); ...
Places: isthmus-town (Isthmus Town); ...
Fronts: vuk-reborn (Vuk Is Reborn); gate-opened (The Gate Opens)
Endings: vuk-stopped (Vuk Is Stopped); ...
Storyline seeds: aldra (Aldra's Whereabouts); the-tithe (The Tithingman's Tithe)
Use lookup_module("twin-lakes", "scene:<id>") for one card, ...
```

For a mini module after `use_module`, the state prompt carries a one-line "in play" note and the
material is in the tool result; it is not repeated every turn. The GM can `lookup_module` again
by name.

The module block belongs to the state and opening steps. The narration and review steps get the
ordinary world state and turn log, not the module brief; `lookup_module` itself is a state-step
tool. This keeps module secrets out of the prose pipeline.

### Tools

All module tools are core and available in every campaign; no tool group is needed.

- `list_modules(kind?, tags?, setting?, random?)`
  - Returns cards. For mini modules, filters by campaign tags, compatibility, and use.
  - `random=true` draws a weighted small set, the long-form analogue of a creative-table roll.
  - Does not include a module's GM-only text.
- `lookup_module(module, section?)`
  - Sections: `brief` (default), `scene:<id>`, `cast:<id>`, `place:<id>`, `clue:<id>`,
    `front:<id>`, `ending:<id>`, `storyline:<id>`, `all` (small mini modules only).
  - Returns one section, capped. The result is GM material and must not be quoted to the player.
- `use_module(module, reason?)`
  - Brings a mini module into play; returns its brief and material. Records `module.used`.
  - For an attached campaign module, marks it as "in play" and returns its brief; does not seed
    content again.
- The owner attaches/detaches campaign modules through the Settings route, not a GM tool.

`roll_creative` and `list_dice_tables` are updated with table roles, described below.

### Prompt fragments

Add core prompt fragments:

`creative_oracle` (state):

> When a scene has run its course or the player is unsure what to do next, and no active storyline
> or module card gives you the next beat, roll on the oracle tables with `roll_creative`: name
> `action`, `theme`, and `plot_twist`, and `location` or `location_descriptor` when the scene is
> about a place. Read each result as a prompt, not a command: keep what fits the established
> story, drop what clashes, and make it yours. Do not roll on consecutive turns, do not roll when
> the player has a clear goal, and never roll in the middle of an encounter. If a mini module fits
> better than a table roll, use the module instead.

`modules` (state):

> A module is a human-written story pack. The Module block in your world state names the active
> campaign modules and the story cards they offer. Treat it as material, not a script: bring its
> cards in when they fit what the player is doing, let the player's choices change the outcome,
> and skip or change anything that no longer fits. Use `lookup_module` for the one card you need
> and do not read the whole module into the narration. Never mention the module, its structure, or
> that the story was prepared. When a module thread becomes one the players are following, create
> a storyline with `create_storyline` and keep it current with `update_storyline`; the module does
> not track its own progress. Campaign modules are attached by the campaign owner. Mini modules
> are available for you to use when the story needs a new thread: call `list_modules` to see cards
> that fit the campaign's tags, then `use_module`. Do not use a mini module in the middle of an
> encounter unless it fits the encounter.

> Implementation note: the opening text below is appended by `opening_prompt()` when a starting
> campaign module is present; it is not a separate `prompts.json` fragment.

`module_opening` (opening, when applicable):

> This campaign begins with a campaign module. Use its start location and hooks as the adventure's
> opening, and its opening card as the situation the player character walks into. Create
> storylines from the hooks as usual. The module's other cards are material for later; do not
> reveal them in the introduction.

Update `setting_forge` to roll the setting role and to keep the oracle tables for play:

> Roll the setting tables with `roll_creative`, passing role `"setting"`, to get a premise, a
> region, a faction, a conflict, a mystery, and a hook. Use `list_dice_tables` to see the
> structured tables the selected genres offer, then roll the ones you need with `roll_tables`.
> Do not roll the scene oracle tables (`action`, `theme`, `plot_twist`, `location`,
> `location_descriptor`) to invent the setting; they are for play.

Naming a role rather than the six table ids keeps the prompt valid for a rule set whose genre
selection hides those tables; the GM rolls whichever setting tables the campaign actually offers.

### Creative table roles

Add `role` to `DiceTable` and `creative_table` JSON:

- `setting`: world-generation tables. Rolled by `setting_forge`; not the default for mid-play.
- `oracle`: scene oracles. Rolled by the mid-play prompt; the default for a no-argument
  `roll_creative`.
- `module`: a module's own tables. Available while the module is active; named explicitly or
  listed in the module block.

Ship roles:

- Core `creative_tables.json`: `oracle`.
- Core `worldgen_tables.json`: `setting`.
- Academia Arcana `magicschool_tables.json`: `setting`.
- World creative tables: `oracle` unless they define setting material.
- Module creative tables: `module`.

Update `roll_creative`:

- Add a `role` argument with `oracle`, `setting`, and `module` values. With no `role`, it is
  `oracle`.
- With no `tables`, roll only the tables of that role. This is how the setting forge asks for
  setting tables without naming ids a genre choice might hide.
- With `tables`, allow any creative table the campaign offers; explicit ids win over the role.
- The tool description's first sentence carries the trigger, because `brief` mode keeps only the
  first sentence:
  - "Roll on the oracle tables when the scene stalls or the player is unsure what to do next."
  - The rest explains roles, named tables, and "results are prompts, not commands."
- `list_dice_tables` returns `role` and `genres`, so the GM can choose.

This is a behavior change for authors who relied on a no-argument roll covering every creative
table. Document it in the content authoring notes; the `setting_forge` prompt now names the
setting tables, so shipped content is unaffected.

### The checklist

Do not add a core creative checklist item by default. The prompt-only experiment was enough to
produce mid-play rolls; the checklist experiment rolled every turn and added latency. If later
live tests show the prompt is ignored, add a narrowly worded checklist item such as "scene
momentum: whether the scene has stalled and whether an oracle or mini module is needed" and test
it against the prompt-only version. Do not make "roll a table" the checklist answer; the answer
can be "no change."

### Player-facing compendium

- Shipped: a campaign module's embedded setting appears in the compendium world picker and is
  labelled "Campaign module". Mini modules are listed as cards in a "Story modules" section.
- A module's setting shows the same public entries a world shows: locations, NPCs, monsters,
  features, items, and rules. It does not show `premise`, `truth`, `gm_notes`, scenes, clues,
  fronts, or endings. Mini module cards show the name, blurb, and genres only.
- A module's `blurb`, `source`, tags, and icon are player-facing.
- The in-play header stays the world/rule set. No module link, progress, or module name is
  shown in the play UI; the compendium is the out-of-game reference surface.
- Twin Lakes appears once, as a campaign module, not twice as a world and a module. If embedded
  worlds are registered as world templates for compatibility, the compendium presentation must
  deduplicate them and label the entry "Campaign module."

### Settings

Shipped Settings behavior for the campaign owner:

- The **Story modules** panel attaches and detaches compatible campaign modules. The starting
  module is always attached and cannot be removed.
- The **Creative and lore tables** panel under Game changes the `table_genres` selection that
  both filters creative tables and selects the mini-module pool.
- The panel lists the mini modules used so far.
- No module progress editing; the Story tab and journal remain the player-facing record.

Attaching a campaign module mid-turn is refused while a turn or encounter is running, like other
campaign settings. The change applies from the next reply.

### Twin Lakes conversion

1. Move `src/cygnusloom/data/worlds/twin-lakes/` to `src/cygnusloom/data/modules/twin-lakes/`.
2. Keep `world.json` in the module directory as the embedded setting. The loader builds a
   `WorldTemplate` from it and registers it under `twin-lakes`, so old references work.
3. Add `module.json`:
   - `kind: "campaign"`;
   - `rule_set: "cairn2e"`;
   - `world: "twin-lakes"` (embedded);
   - the two hooks from `world.json` `start` (or keep the embedded world's start and have the
     module reference it);
   - `premise`, `truth`, `gm_notes`, `tone`, `themes`;
   - `cast`/`places` link tables for the adventure's key people and places (all NPCs and
     locations remain in the embedded world's own `npcs.json`/`locations.json`);
   - `scenes` for eight key beats: arrival, the two offers, the butcher, the Stuffed Inn, the
     river road, Deadmill, the White Tower, and the temple;
   - `clues` for the poisoned river, Aldra's lantern, the tithe record, the gate mark, and Vuk's
     vessel;
   - `fronts` for Vuk's rebirth and the Gate opening, and four endings;
   - `storylines` seeds for the two hooks.
4. Convert the existing `rules.json` outcome text into `module_front`/`module_ending` entries.
   Keep the location, monster, and NPC rules where they are; they are already useful attached
   rules.
5. Add scenes that reference existing locations/NPCs rather than duplicating their descriptions.
   The scene cards carry the situation, goal, clues, complications, and suggested next beats; the
   location/NPC records keep the details.
6. Update the New Campaign form and compendium to show Twin Lakes as a campaign module. Keep
   `world_template: "twin-lakes"` resolving for old campaigns and exports.
7. Add module pair coverage to content validation and tests while keeping the world/rule set
   pair working for compatibility. The `twin_lakes=True` test fixture still finds the
   NPCs/places; `test_distribution.py` includes the new module root files.
8. Do not change the default campaign (`worldswithoutnumber` + `unwritten-world`) unless the team
   asks.

## How the AI GM uses creativity

The intended decision loop in the state step:

1. Look up what the action depends on.
2. If the action follows a focused storyline, a module card, or an obvious consequence, play it.
3. If the scene is complete or the player is unsure and nothing obvious follows:
   - if an available mini module fits, `list_modules` -> `use_module`;
   - otherwise `roll_creative` with the oracle tables;
   - if the player is mid-encounter, do not roll; use the encounter's own logic.
4. Use the result as a prompt, not a command; keep what fits, drop what clashes.
5. Record the changes, update storylines/notes, and finish the state step.

This makes creative tables and modules the **injection** layer and storylines/notes the
**continuity** layer. The GM never has to invent a whole new thread from nothing, and it never has
to follow a prepackaged script.

## Implementation status

| Area | Status |
| --- | --- |
| Creative table roles, oracle default, `setting` forge, `creative_oracle` prompt | Shipped |
| Module loader, inline entries, embedded/synthetic settings, tag and compatibility checks | Shipped |
| `Campaign.module`, module events, state fold, export/import | Shipped |
| `list_modules`, `lookup_module`, `use_module`, state/opening brief | Shipped |
| New Campaign picker, Settings story modules, compendium module worlds/mini cards | Shipped |
| Twin Lakes conversion and old `world_template` compatibility | Shipped |
| Sample mini module (`the-hermit-of-ash-hollow`) | Shipped |
| Content-defined tag vocabulary (`data/tags.json`) | Not shipped; `TABLE_GENRES` extended with `mystery` |
| `module.completed`, module progress UI | Not shipped; storylines and notes are the record |
| Seeding a later-attached module's locations/NPCs as events | Not shipped; merged content is available and the GM creates entries when needed |
| `replaces` enforcement | Not shipped; id-based module precedence is the merge rule |
| Full module authoring guide (`docs/MODULES.md`) | This document plus the sample module serve as the guide |
| Live tuning across models and rulesets | Automated coverage ships; live tuning remains a follow-up |

### Deferred work

- A content-defined tag vocabulary and a `content validate` module report.
- Authoring docs split out of this design document.
- Seeding attached campaign module content as events at attach time.
- A `module.completed` event or any module progress UI.
- Encounter maps/templates inside modules.
- User-imported module files with embedded snapshots.

## Testing plan

## Testing plan

The 3,745-test suite passes with the module work. `tests/test_modules.py` is the focused
coverage; the creative-table, worldgen, campaign, turn, compendium, content, and export tests were
updated for roles, module worlds, and the migration head.

A live DeepSeek smoke test on a throwaway database confirmed the plumbing:

- A Twin Lakes campaign opening used the module's start and hooks, created both module storyline
  seeds ("Aldra's Whereabouts" and "The Tithingman's Tithe"), and never printed the word
  "module" in the player-facing reply.
- A follow-up turn at the Butcher called `lookup_module("twin-lakes", "scene:the-butcher")` and
  `lookup_module("twin-lakes", "cast:flavia")`, then used the scene's ledger, lantern, and
  Flavia's condition for returning Aldra's body in the narration.
- A directionless road-wait turn followed the existing Dark Drum thread and did not force a mini
  module. That is the intended behavior: the module pool is for a story that needs a new thread,
  not a compulsion to inject one every quiet turn.

### Automated

- Creative tables: core roles, no-arg oracle roll, named setting/module roll, genre filtering,
  `list_dice_tables` fields, prompt text, tool description first sentence.
- Module loader: valid mini/campaign modules, inline entries, embedded world, external world,
  compatibility, tags, missing references, duplicate ids, undeclared replacement, statblock
  validation, `content validate` coverage.
- Runtime: attached/detached/used events, state fold, one-shot filtering, starting module fields,
  export/import round trip, old export compatibility.
- Prompt placement: module brief in state/opening only; absent from narration/review; opening
  hooks and opening card; no later cards in the opening.
- UI: new campaign form, settings panel, compendium public fields, no in-play module UI, mobile.
- Twin Lakes: loader, compendium, old `world_template` resolution, hooks, `twin_lakes` fixture.

### Live follow-up

These checks were designed but not all were run against a live model in this iteration. The
automated tests use the fake provider and assert prompt/tool plumbing; they do not prove the
model's choices.

- Twin Lakes campaign module start with DeepSeek: opening uses the module hooks; two storylines;
  `lookup_module` for a scene/cast; no module name in play; no spoilers.
- Mid-campaign attach: owner attaches a module; the next state step sees the brief; the GM
  introduces it only when it fits; the focused storyline remains intact.
- Mini module use: a directionless scene -> `list_modules` -> `use_module` -> the GM creates the
  NPC/place and a storyline when appropriate; the module is marked used.
- Creative oracle: a directionless scene -> `roll_creative` -> narration uses the result; no
  setting tables; no roll in an encounter; no consecutive rolls.
- Export/import: a campaign with a starting module, an attached module, and a used mini module
  round-trips; an old export with `world_template: twin-lakes` still imports.

## Open questions

Resolved in the shipped implementation: `table_genres` is reused; a later attach does not seed
content; `module.completed` is omitted; mini modules are one-shot by default; the compendium does
not show module scene/clue handouts; user-imported modules are not supported.

Still open:

- Should the campaign's coarse selection be the existing `table_genres` (one list for tables and
  mini modules) or a separate `module_tags` list? This document recommends reusing
  `table_genres` for now and adding a separate field only if authors need finer selection.
- Should a mid-campaign campaign module seed all its locations/NPCs at attach, or only mark them
  available for the GM to create? This document recommends seeding authored content at attach,
  like a world expansion, while mini modules stay lazy.
- Should a module be able to replace a world entry implicitly? This document recommends explicit
  `replaces` declarations and a validation error otherwise.
- Should `module.completed` exist? The first version can omit it; the owner detaches and the GM
  resolves the module's storylines.
- Should mini modules default to one-shot or repeatable? This document recommends one-shot by
  default, with `repeatable: true` for encounter-style modules.
- Should the compendium show module scene/clue handouts? The first version does not; only
  world-like entries. Player-facing handouts can be added later as a separate definition.
- Should user-imported modules be supported? Not now; when they are, campaign exports must embed
  the module snapshot so the campaign remains playable without the original file.
- Should modules be allowed to add encounter maps/templates? The first version references the
  rule set's existing templates; a later phase can extend the module content types.

## Appendix: audit commands and data

The historical audit loaded each `dist/*.json`, counted `dice.tables_rolled`, and matched table
ids against the shipped creative tables. All exports are forks of one campaign lineage, so the
counts describe one long game, not independent trials.

The live audit used a throwaway SQLite database per campaign, `CYGNUSLOOM_TEST_LOGIN=1`, the real
DeepSeek provider from the environment, and `tests.factories` to create the campaign and
character. The same three messages were used in every run. The prompt experiments used temporary
copies of the bundled content with the extra prompt/checklist entries; no repository files were
changed.

The prompt-only experiment added this state fragment to Cairn 2e:

> When a scene has run its course or the player seems unsure what to do next, roll on a creative
> table with `roll_creative` before you narrate: name action, theme, plot_twist, location, or
> location_descriptor when one fits, or leave the argument out. Read the result as a prompt, not a
> command: work what fits into the place, the people, or the trouble already in play, and drop
> what clashes. Do not roll every turn, and do not roll in the middle of an encounter.

The prompt + checklist experiment also added this core checklist item:

> `creative`: whether the scene has stalled or needs a fresh direction, and whether a
> creative-table roll would help.

The proposed `creative_oracle` prompt above is a refinement of the experiment's wording: it names
`action`, `theme`, and `plot_twist` explicitly so a no-argument roll is not needed.

The audit's headline numbers:

- 125 turns, 3,698 events, 3 storylines created, 80 updated, 1 resolved, 1 creative roll at the
  opening, 0 creative rolls after it.
- Twin Lakes current code, 3 directionless turns: 0 creative rolls.
- Unwritten World current code, 3 directionless turns: 6 setting tables at the opening, 0
  mid-play rolls.
- Twin Lakes prompt-only experiment: 2 mid-play creative rolls, one of which rolled all 11
  tables because no table list was given.
- Twin Lakes prompt + checklist experiment: 3 mid-play creative rolls, one per turn.

These are small samples. The next implementation phase should repeat the live test across more
worlds, rule sets, and models before treating the prompt wording as final.
