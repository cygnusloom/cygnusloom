# Creativity, oracles, and story seeds

Status: implemented. This document records the audit that prompted the work, the final design,
and the behavior that ships.

## Summary

Two content concepts remain:

1. **Worlds** are the full adventures. A human-written module is a world: its locations, NPCs,
   rules, tables, and start hooks are the adventure, the same way a published module is read by a
   human GM. Twin Lakes is a world again; there is no separate campaign-module manifest, world
   copy, `Campaign.module`, or module picker beside the world picker.
2. **Mini modules** are small story seeds. They have no world of their own: a module is a short
   prose `body`, tags, compatibility, and GM notes. The AI GM may pick one when the campaign needs
   a new thread.

Creative tables remain a third input: the oracle tables suggest fresh material during play,
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

## Worlds are the modules

A world already has everything a long-form module needs:

- a name, blurb, description, icon, rule set, and start;
- locations, NPCs, regions, routes, and local maps;
- monsters, features, tables, rules, and outcome text;
- a campaign start and hooks.

Twin Lakes is the case that proved it. Its existing world files are the adventure; its outcome
rules are the endings. The campaign chooses a **world**, and the AI GM uses the ordinary world
lookups it already has. There is no second content type to keep in sync.

Consequences:

- No `Campaign.module` field or module migration.
- No campaign-module directory, embedded-world registration, or module world copy.
- No module picker beside the world picker, and no attach/detach panel.
- No `module.attached` or `module.detached` events. Only `module.used` remains, for mini modules.
- New Campaign requires a world again. Twin Lakes appears in the world picker like any other world.

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
    "gm_notes": "Mara is frightened, not villainous. She will trade the truth for her grandchild's safety.",
    "body": "Mara Ash saw the miller's son set the fire...",
    "repeatable": false
  }
]
```

The parser reads only `module` definitions. `body` is required: it is the seed's adventure text.
There are no scene, clue, front, ending, hook, storyline, or inline entry types. If a seed needs a
place or person, the GM creates it with the normal tools so the campaign keeps one record.

## Runtime

### Opening and state prompt

The opening prompt uses the chosen world's `start` and hooks, exactly as before. There is no
module opening card.

The state prompt adds one short note when mini modules are available:

```
Mini modules available: 3. Call list_modules when the story needs a new thread, then use_module
to bring one in.
```

The note is not repeated in narration or review. The module body is never in the state prompt; it
arrives only when a seed is used.

### Tools

- `list_modules(tags?, setting?, random?)` returns mini-module cards that fit the campaign's
  genres, rule set, and unused one-shot state.
- `use_module(module, reason?)` returns the seed's GM notes and body, records `module.used`, and
  removes a non-repeatable seed from the pool.
- `lookup_module(module)` rereads a seed already used in this campaign.
- Campaign module tools and the `kind` argument are gone.

### AI GM loop

1. Look up what the action depends on.
2. If the action follows a focused storyline, a world entry, or an obvious consequence, play it.
3. If the scene is complete or the player is unsure and nothing obvious follows:
   - if a mini module fits, `list_modules` then `use_module`;
   - otherwise `roll_creative` with the oracle tables;
   - if the player is mid-encounter, use the encounter's own logic.
4. Treat the result as a prompt, not a command; keep what fits and drop what clashes.
5. Record changes, update storylines/notes, and finish the state step.

Worlds and mini modules are the **injection** layer; storylines and notes are the **continuity**
layer. The engine does not track plot progress.

## Visibility

- A world's public entries appear in the compendium as they always have.
- Mini modules are listed as cards in the compendium's "Story modules" section: name, blurb, and
  genres only. Their `gm_notes` and `body` stay with the GM.
- There is no in-play module tab or module progress. A mini module's material appears only after
  the GM uses it.

## Twin Lakes

Twin Lakes is a normal world under `src/cygnusloom/data/worlds/twin-lakes/`. There is no
`module.json`. Its locations, NPCs, monsters, features, rules, tables, and outcome text are the
adventure. Existing campaigns and exports that name `world_template: "twin-lakes"` keep working.

## Testing

The full suite passes with the final model. `tests/test_modules.py` covers mini modules; campaign
and world tests cover Twin Lakes as a world; content and migration tests cover the restored world
layout and migration head.

Live DeepSeek checks on Twin Lakes:

- The opening used the world's start hooks, created both hooks' storylines, and never printed
  "module" in the player-facing reply.
- A Butcher turn used the world's own Flavia and Butcher Shop entries, with Aldra's ledger,
  lantern, and burial condition in the narration.
- A directionless road turn followed the existing thread and did not force a mini module.

## Deferred

- A content-defined tag vocabulary; `TABLE_GENRES` remains the genre list, extended with
  `mystery`.
- A separate content report for mini modules.
- Authoring docs beyond this document and the sample module.
- User-imported mini modules.
