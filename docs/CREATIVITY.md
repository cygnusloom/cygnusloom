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
- There are no module-specific scene/clue/front/ending/hook/storyline types. A campaign module
  is its world; a mini module adds a prose `body`.
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

A module is JSON: a manifest, ordinary world content, and optional prose `body`. A campaign
module's material is its embedded world, so the AI GM uses the same location, NPC, and rule
lookups it already has. The engine records which modules are attached or used for bookkeeping, but
**does not track plot progress**: storylines and notes remain the live record. Players see module
entries in the compendium like world entries; in play, only the AI GM sees the module material.

Twin Lakes is the first campaign module. Its existing world is the module's setting; its existing
outcome rules are the endings. Existing campaigns and exports that name `world_template:
twin-lakes` keep working.

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
- Keep the AI GM's context small by showing a compact brief and reading module prose only on demand.
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
- **Campaign module**: a full adventure represented by an embedded world, with a short manifest
  for metadata and GM notes. It can be chosen as the campaign's starting point, or attached later
  by the campaign owner in Settings. It is never loaded automatically.
- Both use the same JSON definitions and the same module tools. `kind` controls the default
  behavior and the UI: mini modules are in the GM's pool; campaign modules require an owner
  action.
- Modules can be any size. "Mini" and "campaign" describe how they are selected, not a hard
  content limit.

### Modules are material, not a state machine

- A campaign module is its world: places, people, rules, and tables, exactly as a human GM
  reads a published adventure.
- A mini module adds a short `body` of prose and, optionally, a few normal world entries.
- The GM decides when to bring module material in and what the outcome is.
- The only runtime module state is bookkeeping:
  - Which campaign modules are attached.
  - Which mini modules have been used, so a one-shot mini module is not offered twice.
  - That is all. Plot progress stays in `storyline.*` events and notes.
- When a module thread becomes something the players are following, the GM creates a runtime
  storyline and updates it like any other. There is no prefilled module progress.

### Who chooses what

- **Player, at campaign creation**: the rule set, the starting campaign module (or none), the
  optional world, and the creative tags that select available mini modules.
- **Owner, in Settings**: attach or detach campaign modules; change the creative tags; see which
  mini modules have been used. The owner cannot force the GM to use a module.
- **AI GM**: select and use mini modules from the tagged pool; read module prose; create
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
  world's `start`; otherwise the world's start is used.

### Compatibility and merging

- A module declares:
  - `rule_set` or `recommended_rule_sets`: the rules it is built for, if any.
  - `world` or `recommended_worlds`: the world it is built for, if any.
  - No declaration means portable; it must then avoid statblocks that assume a rule set.
- If the module directory contains a `world` definition with the id named in `world`, that world
  is embedded in the module. Otherwise `world` names an external world template. A module may
  have an embedded world and no `world` field; the embedded world's id is then its setting.
- Module content is merged into the campaign's world content. Precedence is:
  rule set < selected world < module. A module entry with the same id replaces the world entry;
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
  and rules where the world compendium would show them. Do not show `gm_notes` or `body`.
- **In play**: no module tab, no module name in the campaign header, no module progress. The GM
  sees the module brief in the state step; the narration step does not. Module storylines appear
  on the Story tab only after the GM creates them.
- **Spoilers**: the compendium choice is intentional; it matches today's world behavior, where
  authored locations and NPCs are listed before they are discovered. GM-only module text stays
  out of both the compendium and the narration.

### Context budget

- The state step carries a compact brief per active campaign module: name, kind, description,
  GM notes, and opening hooks.
- Mini modules are not in the prompt every turn. The prompt says how many are available and that
  `list_modules` will show them. `use_module` returns the chosen module's brief and body.
- `lookup_module` returns the brief and body, capped.
- The module block is built only for the **state and opening steps**. The narration and review
  steps get the ordinary world state and turn log, without module text. This is both a token
  saving and a spoiler protection.

## Content model

A module is a manifest plus the same content a world already uses. There are no module-specific
scene, clue, front, ending, hook, or storyline records.

```
src/cygnusloom/data/modules/
  twin-lakes/
    module.json       # manifest and optional prose
    world.json        # the setting
    locations.json
    npcs.json
    rules.json
    ...               # any normal world files
  the-hermit-of-ash-hollow/
    module.json       # a mini module, with small entries inline
```

- A **campaign module** is mostly its embedded `world`: locations, people, rules, tables, and the
  start hooks carry the adventure, the way a published module does. `module.json` adds only what
  has no world entry: a description, GM notes, tags, compatibility, and optional `body` prose.
- A **mini module** gets a synthetic world from its inline `entries`, and `body` is its adventure
  text. It can also use ordinary `location`/`npc` entries.
- `entries` is a convenience for small modules; larger ones put the same definitions in separate
  files. Both are ordinary world definitions.

### Module manifest

```json
{
  "type": "module",
  "id": "the-hermit-of-ash-hollow",
  "kind": "mini",
  "name": "The Hermit of Ash Hollow",
  "description": "A guarded herbwife, a burnt hut, and a purse of hush-money.",
  "blurb": "A reclusive herbwife knows what happened at the mill.",
  "icon": "campfire",
  "tags": {"genres": ["fantasy", "mystery"], "settings": ["wilderness"]},
  "gm_notes": "Mara is frightened, not villainous. She will trade the truth for her grandchild's safety.",
  "body": "Mara Ash saw the miller's son set the fire. The miller paid her to keep quiet...",
  "entries": [
    {"type": "npc", "id": "mara-ash", "name": "Mara Ash", ...},
    {"type": "location", "id": "ash-hollow-hut", "name": "Ash Hollow Hut", ...}
  ]
}
```

A campaign module adds `rule_set`, `world`, `third_party`, and `source`, and usually has no
`body`: its material is the world. `start` is optional when the embedded world already has one.

### Runtime reading

- `module_brief` gives the GM the name, description, GM notes, and opening hooks.
- `lookup_module` returns the brief plus `body` when there is one.
- A campaign module's places, people, and rules are read with the normal world and NPC tools.
- Plot progress stays in storylines and notes.

### Authoring guidance

Write the module the way a human GM would read it:

- Put locations, NPCs, monsters, and rules in the normal world files. Do not repeat them in the
  module manifest.
- Use `gm_notes` for what the GM must understand but the players must not read: the truth, the
  moving parts, and the tone.
- Use `body` for prose that has no natural entry, such as a mini module's situation. Paragraphs
  and ordinary prose are enough; no card schema.
- Keep player-facing `description`/`blurb` free of secrets.
- Write outcomes as possibilities. The existing outcome rules in `rules.json` are a good model.
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
   - module plus a selected world: merge the selected world and the module's world, module
     entries win on id collisions;
   - module without an embedded world: require a selected world;
   - no module: today's world-only behavior.
3. The app seeds the merged world content as today (`seed_campaign`).
4. The campaign records the selected module id in `Campaign.module`. No `module.attached` event
   is recorded for the starting module.
5. The opening prompt uses the module's `start` location and hooks when it has them, otherwise
   the world's. It tells the GM the module's places, people, and rules are ordinary world entries
   and to create storylines from the hooks as usual.

### Attaching a campaign module later

1. The owner opens Settings -> Story modules, chooses a compatible campaign module, and attaches
   it.
2. `module.attached` is recorded. The module's setting is merged into the campaign world for
   lookups (module entries win on id collisions). The engine does not seed its locations or NPCs
   as events at attach time; the GM creates them with the normal tools when the story reaches
   them.
3. The next state step's module brief includes the new module's description, GM notes, and
   opening hooks. The prompt says to bring it in when it fits, not to derail the focused
   storyline.
4. `lookup_module` returns the brief and any module `body`; its places and people are read with
   the normal world tools.
5. Detaching stops the module brief. Content the GM already created remains in the campaign as
   ordinary world content.

### Using a mini module

1. The GM decides the scene needs a new thread, place, person, or complication.
2. It calls `list_modules(tags?, setting?, random?)`. The result is a short list of cards that
   fit the campaign's tags, rule set, world, and unused one-shot modules.
3. It calls `use_module(module, reason?)`. The result is the module's brief and `body`, capped.
   The engine records `module.used`; a one-shot module leaves the pool.
4. The GM brings the material in with the existing tools: `create_location`, `create_npc`,
   `add_note`, `create_storyline`, `place_object`, and so on. It does not seed the whole module
   automatically.
5. If the players engage, the GM creates and updates a storyline as usual. If they walk away, the
   module is still marked used; the material may return later as a consequence.

### The module block in the state prompt

A compact block, one per active campaign module:

```
Module: Twin Lakes (campaign)
Description: Two offers bring the party into Isthmus Town: find the missing
Tideborn butcher Aldra, or find Jandy the tithingman and the tithe he carried...
GM notes: This is a sandbox mystery. Let the players choose either offer or both...
Opening hooks: - The Tideborn have agreed to pay ...
Its places, people, and rules are ordinary world entries: use the normal lookup
tools rather than re-reading the module.
```

A mini module's body is returned by `use_module`/`lookup_module`; it is not repeated in the
state prompt every turn.

The module block belongs to the state and opening steps. The narration and review steps get the
ordinary world state and turn log, not the module brief; `lookup_module` itself is a state-step
tool. This keeps module secrets out of the prose pipeline.

### Tools

All module tools are core and available in every campaign; no tool group is needed.

- `list_modules(kind?, tags?, setting?, random?)`
  - Returns cards. For mini modules, filters by campaign tags, compatibility, and use.
  - `random=true` draws a small set, the long-form analogue of a creative-table roll.
  - Does not include a module's GM-only text.
- `lookup_module(module)`
  - Returns the module brief and its `body` when it has one.
  - The result is GM material and must not be quoted to the player.
- `use_module(module, reason?)`
  - Brings a mini module into play; returns its brief and body. Records `module.used`.
  - For an attached campaign module, marks it as used and returns its brief; does not seed
    content again.
- The owner attaches/detaches campaign modules through the Settings route, not a GM tool.

### Prompt fragments

`creative_oracle` (state):

> When a scene has run its course or the player is unsure what to do next, and no active storyline
> gives you the next beat, roll on the oracle tables with `roll_creative`: leave role out or pass
> role `"oracle"`, and name `action`, `theme`, and `plot_twist`, plus `location` or
> `location_descriptor` when the scene is about a place. Read each result as a prompt, not a
> command: keep what fits, drop what clashes. Do not roll on consecutive turns, do not roll when
> the player has a clear goal, and never roll in the middle of an encounter. If a mini module fits
> better than a table roll, use the module instead.

`modules` (state):

> A module is a human-written story pack. The Module block in your world state names the active
> campaign modules. A campaign module's material is the world's own places, people, and rules:
> use the normal lookup tools and bring them in when they fit what the player is doing. Treat the
> module as preparation, not a script; let the player's choices change the outcome. Never mention
> the module, its structure, or that the story was prepared. When a module thread becomes one the
> players are following, create a storyline with `create_storyline` and keep it current with
> `update_storyline`; the module does not track its own progress. Campaign modules are attached by
> the campaign owner. Mini modules are available when the story needs a new thread: call
> `list_modules`, then `use_module`. Do not use a mini module in the middle of an encounter unless
> it fits the encounter.

The opening instruction is appended by `opening_prompt()` when a starting campaign module is
present; it is not a separate `prompts.json` fragment.

`setting_forge` asks for the `setting` role:

> Roll the setting tables with `roll_creative`, passing role `"setting"`, to get a premise, a
> region, a faction, a conflict, a mystery, and a hook. Do not roll the scene oracle tables
> (`action`, `theme`, `plot_twist`, `location`, `location_descriptor`) to invent the setting; they
> are for play.

### Creative table roles

`DiceTable` and `creative_table` JSON carry `role`:

- `setting`: world-generation tables. Rolled by `setting_forge`; not the default for mid-play.
- `oracle`: scene oracles. The default for a no-argument `roll_creative`.
- `module`: a module's own tables, named explicitly.

Shipped roles: core `creative_tables.json` and world tables are `oracle`; core
`worldgen_tables.json` and Academia's `magicschool_tables.json` are `setting`.

`roll_creative` with no `tables` rolls only that role; named tables still work and win over the
role. `list_dice_tables` returns `role` and `genres`.

### The checklist

No core creative checklist item is shipped. The prompt-only experiment was enough to produce
mid-play rolls; a checklist item rolled every turn and added latency.

### Player-facing compendium

- A campaign module's embedded setting appears in the compendium world picker and is labelled
  "Campaign module". Mini modules are listed as cards in a "Story modules" section.
- The setting shows the same public entries a world shows: locations, NPCs, monsters, features,
  items, and rules. It does not show `gm_notes` or `body`. Mini module cards show the name,
  blurb, and genres only.
- A module's `blurb`, `source`, tags, and icon are player-facing.
- The in-play header stays the world/rule set. No module link, progress, or module name is shown
  in the play UI; the compendium is the out-of-game reference surface.
- Twin Lakes appears once, as a campaign module, not twice as a world and a module.

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
2. Keep `world.json` and the existing locations, NPCs, monsters, features, rules, tables, and
   regions. They are the module's material, exactly as the source presents them.
3. Add a small `module.json`: `kind: "campaign"`, `rule_set: "cairn2e"`, `world: "twin-lakes"`,
   the tags, source, description, and GM notes. There are no separate scene/clue/front/ending
   files, and the existing `rules.json` outcome rules are the endings.
4. Update the New Campaign form and compendium to show Twin Lakes as a campaign module. Keep
   `world_template: "twin-lakes"` resolving for old campaigns and exports.
5. Do not change the default campaign (`worldswithoutnumber` + `unwritten-world`) unless the team
   asks.

## How the AI GM uses creativity

The intended decision loop in the state step:

1. Look up what the action depends on.
2. If the action follows a focused storyline, module material, or an obvious consequence, play it.
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

The 3,745-test suite passes with the module work. `tests/test_modules.py` is the focused
coverage; the creative-table, worldgen, campaign, turn, compendium, content, and export tests were
updated for roles, module worlds, and the migration head.

A live DeepSeek smoke test on a throwaway database confirmed the plumbing:

- A Twin Lakes campaign opening used the module's start and hooks, created both storylines from
  the hooks ("Aldra's Whereabouts" and "The Tithingman's Tithe"), and never printed the word
  "module" in the player-facing reply.
- A follow-up turn at the Butcher read the module brief and used the world's own Butcher Shop
  and Flavia entries; the narration carried Aldra's ledger, the lantern, and Flavia's condition
  for returning the body.
- A directionless road-wait turn followed the existing Dark Drum thread and did not force a mini
  module. That is the intended behavior: the module pool is for a story that needs a new thread,
  not a compulsion to inject one every quiet turn.

### Automated

- Creative tables: core roles, no-arg oracle roll, named setting/module roll, genre filtering,
  `list_dice_tables` fields, prompt text, tool description first sentence.
- Module loader: valid mini/campaign modules, inline entries, embedded world, external world,
  compatibility, tags, duplicate ids, manifest/body validation, statblock validation, `content
  validate` coverage.
- Runtime: attached/detached/used events, state fold, one-shot filtering, starting module fields,
  export/import round trip, old export compatibility.
- Prompt placement: module brief in state/opening only; absent from narration/review; opening
  hooks in the opening.
- UI: new campaign form, settings panel, compendium public fields, no in-play module UI, mobile.
- Twin Lakes: loader, compendium, old `world_template` resolution, hooks, `twin_lakes` fixture.

### Live follow-up

These checks were designed but not all were run against a live model in this iteration. The
automated tests use the fake provider and assert prompt/tool plumbing; they do not prove the
model's choices.

- Twin Lakes campaign module start with DeepSeek: opening uses the module hooks; two storylines;
  normal world lookups for its places and people; no module name in play; no spoilers.
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
not show module prose or GM notes; user-imported modules are not supported.

Still open:

- Should the campaign's coarse selection be the existing `table_genres` (one list for tables and
  mini modules) or a separate `module_tags` list? This document recommends reusing
  `table_genres` for now and adding a separate field only if authors need finer selection.
- Should a mid-campaign campaign module seed all its locations/NPCs at attach, or only mark them
  available for the GM to create? This document recommends seeding authored content at attach,
  like a world expansion, while mini modules stay lazy.
- Should a module be able to replace a world entry implicitly? The shipped merge uses ids;
  a module entry with the same id wins.
- Should `module.completed` exist? The first version can omit it; the owner detaches and the GM
  resolves the module's storylines.
- Should mini modules default to one-shot or repeatable? This document recommends one-shot by
  default, with `repeatable: true` for encounter-style modules.
- Should the compendium show module prose or GM notes? The shipped version shows only the
  world-like entries and a mini module card.
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
