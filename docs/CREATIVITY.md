# Creativity, oracles, and modules

Status: implemented. This document records the audit that prompted the work, the final design,
and the behavior that ships.

## Summary

Three content concepts remain:

1. **Campaign modules** are long-form, human-written adventures. A campaign module owns its
   setting: its manifest, locations, NPCs, monsters, rules, tables, and start hooks sit in one
   directory, the same way a published module is read by a human GM. Twin Lakes is a campaign
   module.
2. **Worlds** are setting material chosen at campaign creation. A world is not a second copy of a
   campaign module; a module's setting is never registered as one. The picker offers worlds and
   campaign modules side by side.
3. **Mini modules** are small story seeds. They have no setting of their own: a module is a short
   `blurb`, one private `body` of notes and prose, shared categories and tones, and the location
   densities it fits. The AI GM may pick one when the campaign needs a new thread.

Creative tables remain a fourth input: the oracle tables suggest fresh material during play,
while the setting tables invent an unwritten world at the opening. Tables and modules draw on one
shared category and tone vocabulary, chosen in the campaign's Creative sources panel.

## Audit

The audit predates the shipped changes.

- Historical exports had 125 turns and 3,698 events but only one creative roll, at the opening.
- Live Twin Lakes/Cairn 2e turns with no clear next action made zero creative rolls.
- Live Unwritten World/WWN opened with six setting-table rolls and made zero mid-play rolls.
- `roll_creative` was core and always offered, but the default `brief` tool schema cut its
  description to the first sentence, hiding the guidance to use it when the player is unsure.
- No state prompt told the GM to use creative tables after the opening.
- Prompt-only experiments produced mid-play oracle rolls; a checklist item produced more rolls but
  added latency, so no checklist item ships.

## Creative tables

`DiceTable` and `creative_table` JSON carry a `role`:

- `oracle`: scene oracles. The default for a no-argument `roll_creative`.
- `setting`: world-generation tables. `setting_forge` asks for this role.
- `module`: a module's own tables, named explicitly when relevant.

`roll_creative` with no `tables` rolls only the requested role. Named tables still work and win
over the role. `list_dice_tables` returns each table's role and categories.

Every `creative_table` definition lives in a `creative.json` file in its rule set, world, or
module directory. Ordinary dice tables stay in their own files, such as
`worldgen_tables.json`; the loader rejects a creative table found anywhere else.

### Categories, tones, and density

Tables and modules use the same three creative categories: `generic`, `fantasy`, and
`magicschool`. `generic` fits every setting; otherwise a table or module must share at least one
category with the campaign. The vocabulary lives in `TABLE_GENRES` so a table tag and a module tag
cannot drift apart.

Modules also declare one or more tones from a fixed list: `grim`, `mystery`, `whimsical`,
`relaxing`, and `adventure`. A campaign's selected tones narrow which mini modules the GM may
introduce. Category and tone matching are both overlap tests: a module tagged `grim` and `mystery`
is offered when either tone is selected. A module with no tones fits every tone, but module loading
requires at least one, so shipped modules always state their mood.

Each rule set declares its default `table_genres` and `table_tones`. The campaign form starts with
those boxes checked; the player may narrow either selection, and a blank stored selection falls
back to the rule set's defaults. Campaigns store the category selection in `table_genres` and the
tone selection in `table_tones`. The columns keep the existing names so exports and existing
campaigns remain compatible.

`location_density` is a non-empty list of the densities a module can fit. The possible values
are:

- `low`: one compact site or scene, with no travel network.
- `moderate`: a handful of nearby sites, with a short trip between them.
- `dense`: a connected area with many locations to explore.

A module that can fit anywhere lists all three. A module with one compact site lists only
`low`; a lone string in an older manifest is read as a one-entry list. The densities and their
explanations appear on the card returned by `list_modules`. The AI GM picks one of the listed
densities to decide where the seed can meet the party and how far it can spread. Density never
appears in the player-facing compendium and is not part of the campaign form.

The core `creative_sources` state prompt puts the world, module, and active storylines first,
then requires the GM to consult the creative tables before inventing a new place, faction,
conflict, mystery, clue, complication, or direction. The core `creative_oracle` prompt tells the
GM to roll oracle tables when the scene stalls or the player is unsure, and never in the middle
of an encounter. The first sentence of the tool description carries the source-before-invention
trigger because `brief` mode keeps only that sentence.

The creation tools repeat that trigger in their first sentence. `create_location` says to seed an
invented place with `roll_creative` before recording it. `create_npc` says to leave personality,
accent, and quirks out so its own tables roll them; the GM passes a trait only when the player or
an earlier record fixes it. `speak` is likewise told not to let dialogue introduce a new place,
faction, or clue, and the speech step itself is told to use only what the intent or the speaker's
profile gives. This matters in play because a model that writes traits itself bypasses the roll
tables, a model that records a place before rolling has already invented it, and a speech writer
that adds a landmark can put an unrecorded place into the middle of a turn.

## Campaign modules own their setting

A campaign module has everything a long-form adventure needs:

- a name, blurb, description, icon, rule set, and start;
- categories, tones, and the location densities it fits;
- locations, NPCs, regions, routes, and local maps;
- monsters, features, tables, rules, and outcome text;
- a campaign start and hooks, plus private GM notes.

Twin Lakes is the case that proved it. Its existing setting files are the adventure; its outcome
rules are the endings. The directory is `src/cygnusloom/data/modules/twin-lakes/`: a
`module.json` with `kind: "campaign"` sits beside the ordinary location, NPC, monster, feature,
region, route, rule, and table files. There is no `world.json`, no second directory under
`worlds/`, and no module world copy. The module id doubles as the setting id.

A campaign stores only `world_template`. For a campaign module that column holds the module id;
`ContentLibrary.setting_template` resolves it to the module's own world content. Campaigns,
exports, URLs, and the compendium can keep naming `world_template: "twin-lakes"` without a draft
migration. Consequences:

- No `Campaign.module` field and no campaign-module migration.
- A campaign module's setting is not registered in `world_templates`, so it cannot appear twice.
- New Campaign lists campaign modules and worlds in one required picker; module options are
  labelled separately and filtered by rule set.
- No attach/detach panel and no `module.attached` or `module.detached` events. Only
  `module.used` remains, for mini modules.
- The module's ordinary world tools, lookups, maps, and compendium entries work unchanged.

## Mini modules

A mini module is a small story seed with no setting of its own. It lives in
`src/cygnusloom/data/modules/<id>/module.json`:

```json
[
  {
    "type": "module",
    "id": "the-hermit-of-ash-hollow",
    "name": "The Hermit of Ash Hollow",
    "blurb": "A reclusive herbwife knows what happened at the mill.",
    "icon": "campfire",
    "categories": ["fantasy"],
    "tones": ["grim", "mystery"],
    "location_density": ["low"],
    "rule_set": null,
    "recommended_rulesets": [],
    "third_party": false,
    "source": "Original content written for Cygnus Loom.",
    "body": "The herbwife is frightened, not villainous... Create her, the miller, and his son as named characters, and keep notes on what the player learns.\n\nThe herbwife saw the miller's son set the fire...",
    "repeatable": false
  }
]
```

The parser reads `module` and ordinary world definitions. A mini module accepts only
`module`; `blurb` is its short public card and `body` is its required private GM text. It has no
`description` and no `gm_notes`; the seed's notes and prose share one `body`. Every module names
at least one category, one tone, and one density. There are no scene, clue, front, ending, hook,
storyline, or inline entry types.

A mini module describes roles, not named people or places: `the herbwife`, `the miller`, `the
burnt hut`. When one enters play, the GM creates it with the normal tools, chooses its name from
the creativity tables or their own invention, and records what the player learns in notes. That
keeps one campaign record and lets the same seed fit any setting.

A campaign module keeps `description` as its long public setting text, `blurb` as its short card,
and `body` as its optional private GM notes and prose. There is no separate `gm_notes` field.

## Runtime

### Opening and state prompt

The opening prompt uses `ContentLibrary.setting_template(...)`'s `start` and hooks, so it works
the same for a world and for a campaign module. There is no separate module opening card.

For a campaign module, the state prompt adds the module's private `body` as
`Campaign module notes: ...`, capped. Storylines and notes remain the continuity record; the
engine does not track plot progress.

The state prompt adds one short note when mini modules are available:

```
Mini modules available: 3. Call list_modules when the story needs a new thread, then use_module
to bring one in.
```

The note is not repeated in narration or review. A mini module's `body` never enters the state
prompt; it arrives only when the seed is used. A campaign module's `body` is private state-prompt
text.

### Tools

- A campaign module uses the setting's ordinary world tools: lookups, maps, locations, NPCs,
  encounters, tables, and rule attachments. It has no module-specific tool.
- `list_modules(category?, tone?, random?)` returns mini-module cards that fit the campaign's
  categories, tones, rule set, and unused one-shot state. A card carries the module's categories,
  tones, location densities, and the help text for each. A module that fits anywhere lists all
  three densities. The optional `category` and `tone` arguments filter an already-available
  card; unknown values are rejected.
- `use_module(module, reason?)` returns the seed's private body, records `module.used`, and removes
  a non-repeatable seed from the pool.
- `lookup_module(module)` rereads a seed already used in this campaign.
- Mini-module tools ignore campaign modules; a campaign module is chosen once at creation.

### AI GM loop

The world, campaign module, and active storylines are the first source of any detail. When none
of them supplies a new place, faction, conflict, mystery, clue, complication, or direction, the
GM uses the creative tables before inventing it: setting tables build a world and its powers,
while oracle tables seed play. The GM invents only when no applicable table fits or the result
contradicts established facts, and keeps that invention small.

1. Look up what the action depends on.
2. If the action follows a focused storyline, a world entry, or an obvious consequence, play it.
3. If the action needs a new element, seed it with the relevant creative table first. If a mini
   module fits, `list_modules` then `use_module` is another authored source. For a new NPC, omit
   personality, accent, and quirks unless the player or an earlier record already fixes them;
   `create_npc` rolls the omitted traits. Record a place, person, or actionable object in the
   state step before the narration names it; a detail that appears only in narration is not a
   source for later play.
4. If the scene is complete or the player is unsure and nothing obvious follows, `roll_creative`
   with the oracle tables; if the player is mid-encounter, use the encounter's own logic.
5. Treat the result as a prompt, not a command; keep what fits and drop what clashes.
6. Record changes, update storylines/notes, and finish the state step.

A campaign module's setting, worlds, and mini modules are the **injection** layer; storylines and
notes are the **continuity** layer. The engine does not track plot progress.

## Visibility

- A campaign module's public setting entries and a world's public entries appear in the
  compendium as they always have.
- The compendium has the same **Creative sources** categories and module tones as campaign
  creation. They default to the rule set's choices, and they narrow which creative table entries
  and story modules the page shows. A campaign's compendium tab uses that campaign's saved
  choices. The source panel is collapsed until opened. Each creative table entry carries its
  role and category tags.
- Mini modules are listed as cards in the compendium's "Story modules" section: name, blurb,
  categories, and tones only. Their `body` and location densities stay with the GM. The section
  says plainly when no story module fits the selected sources.
- There is no in-play module tab or module progress. A mini module's material appears only after
  the GM uses it; a campaign module's private notes are GM-only.

## Twin Lakes

Twin Lakes is the campaign module **Trouble in Twin Lakes**, under
`src/cygnusloom/data/modules/twin-lakes/`. Its `module.json` takes the setup, blurb, and both
hooks from CAS-1. The files beside it follow the source's sections without adding a new schema:
`npcs.json` holds the Important NPCs, `locations.json` the region and its numbered temple rooms,
`monsters.json` the bestiary, `features.json` the Relics & Items, `tables.json` the rumors,
encounters, and Victims table, and `rules.json` the key rules, faction lore, and four endings.

The content was audited against the published CAS-1 markdown: setup, hooks, both mysteries, the
Victim Schedule, every Important NPC, every location and temple room T1-T10b, both random tables
and their roll triggers, the bandit leader and bandit statblocks with their morale rule, all Relics
& Items, the memory-prism vision, both factions, Vald lore, and all four endings. The audit's
inventory lives in `tests/test_modules.py` so a later edit cannot silently drop a section.

There is no `worlds/twin-lakes` copy and no `world.json`. Existing campaigns and exports that
name `world_template: "twin-lakes"` keep working because the module id is the content id.

## Compatibility

Module manifests are bundled content, not campaign data, so changing their categories, tones, or
location densities needs no campaign migration. A campaign whose saved `table_genres` names a
retired category keeps the valid categories and ignores the rest; when none remain it falls back
to the rule set's choices. Campaigns created before the `table_tones` column get the rule set's
tones, and an import with no `table_tones` does the same. A single-string `location_density` in an
older manifest is read as a one-entry list. Unknown categories or tones in an old export are
dropped on import instead of rejecting the file.

## Testing

The full suite passes with the final model. `tests/test_modules.py` covers campaign modules,
mini modules, the picker, and the module state note; campaign and content tests cover selecting
Twin Lakes by module id and resolving its setting; migration tests confirm the only new migration
adds `campaign.table_tones`.

Live DeepSeek checks on Twin Lakes:

- The opening used the module's start hooks, created both hooks' storylines, and never printed
  "module" in the player-facing reply.
- A Butcher turn used the module's own Flavia and Butcher Shop entries, with Aldra's ledger,
  lantern, and burial condition in the narration.
- A directionless road turn followed the existing thread and did not force a mini module.

## Deferred

- Moving the category and tone vocabularies out of Python constants and into content data.
- Filtering the campaign-module picker by category or tone; it is still chosen before campaign
  options exist.
- A separate content report for mini modules.
- Authoring docs beyond this document and the sample module.
- User-imported mini modules.
