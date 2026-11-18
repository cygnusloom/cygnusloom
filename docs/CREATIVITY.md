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
   prose `body`, tags, compatibility, and GM notes. The AI GM may pick one when the campaign needs
   a new thread.

Creative tables remain a fourth input: the oracle tables suggest fresh material during play,
while the setting tables invent an unwritten world at the opening.

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
over the role. `list_dice_tables` returns each table's role and genres.

The core `creative_oracle` state prompt tells the GM to roll oracle tables when the scene stalls
or the player is unsure, not on consecutive turns, and never in the middle of an encounter. The
first sentence of the tool description carries the same trigger because `brief` mode keeps only
that sentence.

## Campaign modules own their setting

A campaign module has everything a long-form adventure needs:

- a name, blurb, description, icon, rule set, and start;
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
    "description": "A guarded herbwife, a burnt hut, and a purse of hush-money.",
    "blurb": "A reclusive herbwife knows what happened at the mill.",
    "icon": "campfire",
    "tags": {"genres": ["fantasy", "mystery"], "settings": ["wilderness"]},
    "rule_set": null,
    "recommended_rulesets": [],
    "third_party": false,
    "source": "Original content written for Cygnus Loom.",
    "gm_notes": "The herbwife is frightened, not villainous. She will trade the truth for her grandchild's safety. Create her, the miller, and his son as named characters, and keep notes on what the player learns.",
    "body": "The herbwife saw the miller's son set the fire...",
    "repeatable": false
  }
]
```

The parser reads `module` and ordinary world definitions. A mini module accepts only
`module`; `body` is required and is the seed's adventure text. There are no scene, clue, front,
ending, hook, storyline, or inline entry types.

A mini module describes roles, not named people or places: `the herbwife`, `the miller`, `the
burnt hut`. When one enters play, the GM creates it with the normal tools, chooses its name from
the creativity tables or their own invention, and records what the player learns in notes. That
keeps one campaign record and lets the same seed fit any setting.

A campaign module may also carry an optional `body`: extra long-form GM prose that is not setting
data. It reaches the state prompt with `gm_notes`, never the player-facing narration.

## Runtime

### Opening and state prompt

The opening prompt uses `ContentLibrary.setting_template(...)`'s `start` and hooks, so it works
the same for a world and for a campaign module. There is no separate module opening card.

For a campaign module, the state prompt adds the module's private GM notes (and optional long
`body`) as `Campaign module notes: ...`, capped. Storylines and notes remain the continuity
record; the engine does not track plot progress.

The state prompt adds one short note when mini modules are available:

```
Mini modules available: 3. Call list_modules when the story needs a new thread, then use_module
to bring one in.
```

The note is not repeated in narration or review. A mini module's body never enters the state
prompt; it arrives only when the seed is used. A campaign module's `body` and `gm_notes` are
private state-prompt text.

### Tools

- A campaign module uses the setting's ordinary world tools: lookups, maps, locations, NPCs,
  encounters, tables, and rule attachments. It has no module-specific tool.
- `list_modules(tags?, setting?, random?)` returns mini-module cards that fit the campaign's
  genres, rule set, and unused one-shot state.
- `use_module(module, reason?)` returns the seed's GM notes and body, records `module.used`, and
  removes a non-repeatable seed from the pool.
- `lookup_module(module)` rereads a seed already used in this campaign.
- Mini-module tools ignore campaign modules; a campaign module is chosen once at creation.

### AI GM loop

1. Look up what the action depends on.
2. If the action follows a focused storyline, a world entry, or an obvious consequence, play it.
3. If the scene is complete or the player is unsure and nothing obvious follows:
   - if a mini module fits, `list_modules` then `use_module`;
   - otherwise `roll_creative` with the oracle tables;
   - if the player is mid-encounter, use the encounter's own logic.
4. Treat the result as a prompt, not a command; keep what fits and drop what clashes.
5. Record changes, update storylines/notes, and finish the state step.

A campaign module's setting, worlds, and mini modules are the **injection** layer; storylines and
notes are the **continuity** layer. The engine does not track plot progress.

## Visibility

- A campaign module's public setting entries and a world's public entries appear in the
  compendium as they always have.
- Mini modules are listed as cards in the compendium's "Story modules" section: name, blurb, and
  genres only. Their `gm_notes` and `body` stay with the GM.
- There is no in-play module tab or module progress. A mini module's material appears only after
  the GM uses it; a campaign module's private notes are GM-only.

## Twin Lakes

Twin Lakes is the campaign module **Trouble in Twin Lakes**, under
`src/cygnusloom/data/modules/twin-lakes/`. Its `module.json` takes the setup, blurb, and both
hooks from CAS-1. The files beside it follow the source's sections without adding a new schema:
`npcs.json` holds the Important NPCs, `locations.json` the region and its numbered temple rooms,
`monsters.json` the bestiary, `features.json` the Relics & Items, `tables.json` the rumors,
encounters, and Victims table, and `rules.json` the key rules, faction lore, and four endings.

There is no `worlds/twin-lakes` copy and no `world.json`. Existing campaigns and exports that
name `world_template: "twin-lakes"` keep working because the module id is the content id.

## Testing

The full suite passes with the final model. `tests/test_modules.py` covers campaign modules,
mini modules, the picker, and the module state note; campaign and content tests cover selecting
Twin Lakes by module id and resolving its setting; migration tests confirm no module migration
was added.

Live DeepSeek checks on Twin Lakes:

- The opening used the module's start hooks, created both hooks' storylines, and never printed
  "module" in the player-facing reply.
- A Butcher turn used the module's own Flavia and Butcher Shop entries, with Aldra's ledger,
  lantern, and burial condition in the narration.
- A directionless road turn followed the existing thread and did not force a mini module.

## Deferred

- A content-defined tag vocabulary; `TABLE_GENRES` remains the genre list, extended with
  `mystery`.
- A separate content report for mini modules.
- Authoring docs beyond this document and the sample module.
- User-imported mini modules.
