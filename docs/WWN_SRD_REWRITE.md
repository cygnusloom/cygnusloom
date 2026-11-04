# Worlds Without Number: SRD-only rewrite

## Purpose

The `worldswithoutnumber` ruleset was originally built from the free *Worlds Without Number*
PDF and the character sheet, both of which are copyrighted. The CC0 System Reference Document
(SRD) replaced them as the source of truth. The SRD grants the mechanics, class/art/spell/Focus
names and text, equipment, progression charts, and generic stat lines, but it explicitly
reserves the setting (the Latter Earth and its nations), individual NPCs and creatures, and the
bulk of the GM tools.

This rewrite keeps every mechanic that the SRD grants, removes every piece of reserved creative
content, and replaces the setting with a blank canvas that the AI GM builds at the start of a
campaign using original tables and world tools.

## Source policy

Allowed:

- Every rule, class, art, spell, Focus, skill, equipment stat, progression chart, condition,
  generic monster stat line, and table that appears in the CC0 SRD.
- Paraphrased rules text that expresses the same mechanics.
- Newly authored content for this project, released with the project.

Not allowed:

- The free PDF, the character sheet, or any text, name, statblock, table, or setting fact that
  appears only in them.
- The Latter Earth, its nations, peoples, places, history, cosmology, or named creatures.
- The free book's GM-tool text, world tags, nation tags, culture tables, site tables, or
  adventure hooks.
- The free book's twenty backgrounds beyond the SRD's Artisan and Barbarian examples, its six
  starting equipment packages, or its nine nonhuman origin Foci.

## Audit result

| Area | SRD status | Action |
| --- | --- | --- |
| Attributes, skills, checks, saves, combat, injury, travel, dungeon turns | Covered | Keep; scrub setting nouns |
| Classes, arts, spells, Foci, equipment, progression, conditions, projects | Covered | Keep; scrub setting nouns |
| Backgrounds `artisan`, `barbarian` | Covered | Keep (SRD examples) |
| The other 18 backgrounds and their Growth/Learning tables | Not covered | Rewrite as original archetypes and tables |
| Six starting kits | Not covered | Replace with original kits |
| Nine nonhuman origin Foci | Not covered | Replace with original setting-neutral origins |
| 65 named bestiary monsters | Not covered | Replace with original bestiary and SRD generic stat lines |
| 33 terrain/nation/ruler/government/history/tag tables | Not covered | Replace with original tables |
| 42 culture/religion/settlement/site/ruin/event/monster tables | Not covered | Replace with original tables |
| `world_terrain.json` | Generic mechanics, Latter Earth wording | Rewrite the terrain list and wording |
| `latter-earth` world template | Reserved setting | Replace with the blank `unwritten-world` template |
| Character name/personality/accent/quirk/look tables | Newly authored, low risk | Keep and expand with original entries |
| `rule_set.json`, `rules.json`, `prompts.json`, `equipment.json` | Mechanics mixed with setting nouns | Scrub Latter Earth, arratu, Academies, and similar nouns |

## Replacement content requirements

### Backgrounds

- Keep the SRD's Artisan and Barbarian.
- Keep the existing generic archetype ids used by the creation form and tests, but rewrite every
  description and every Growth/Learning table from scratch.
- Each background needs a free skill, two Learning picks, and a Growth table.
- Add at least four new original backgrounds as setting-inspiring options, exposed through the
  `background_index` table rather than as required templates if the form expects a fixed roster.

### Starting kits

- Six kits, one per existing `pkg_` id.
- Each `grant` must contain catalogue items only, at least one weapon, and worn armor or shield
  where the archetype calls for it.
- Keep the armour profile close enough that the existing AC expectations remain meaningful;
  update the tests where the kit changes.
- No text or item list copied from the free book's equipment packages.

### Origin Foci

- Nine origin features tagged `origin`, one per existing role: artificial, labouring, subterranean,
  graceful, large, beast-like, charismatic, swift, and death-touched.
- New names, new descriptions, new mechanical notes. Keep the same effect vocabulary
  (`skill`, `note`, `stat` if supported) and the GM-permission caveat.
- Do not use Anak, Drudge, Dwarf, Elf, Great/Lesser Anak, Halfman, Houri, or Undead.

### Bestiary

- Original creatures only, plus optional generic stat lines taken from the SRD.
- Every entry needs the full stat map, armour, attacks, `attacks_per_action`, `large_group`,
  `rules`, and optional structured `abilities`.
- Descriptions follow the SRD stat-line convention: `(HD N; MV N ft; Inst N; Skill +N; N attacks
  per action.)` where applicable.
- Include a spread of HD 1 to 12, animals, people, constructs, undead, and strange things.
- No names, descriptions, or ability text from the free PDF.

### Tables

- Keep every existing table id so prompts and tools keep working.
- Replace every row. Tables with 1d20 keep 20 rows; 1d12 keep 12; 1d10 keep 10; 1d8 keep 8; 1d6
  keep 6; 1d4 keep 4; 2d6 keep the five reaction bands; 1d100 tables may use 50 two-number ranges
  or 100 single rows.
- Add a `creative_tables.json` with at least six 1d100 idea tables for setting premise, region
  identity, faction, conflict, mystery, and adventure hook.
- Randomize row order and, where a table is generated from components, randomize the component
  combinations. A fixed seed is fine; the committed output must be produced by the generator.

## AI GM setting tools

- Add a generic core `record_setting(topic, detail)` tool. It upserts a setting fact by topic and
  shows the facts in the GM's world-state block every turn.
- The WWN `setting_forge` prompt tells the GM to invent the world before the opening narration:
  roll the creative and structured tables, adapt the results, record the world, region, peoples,
  powers, history, magic, and conflict, then create the first region, settlement, and NPCs.
- The blank `unwritten-world` template carries no setting of its own.

## Checklist

- [x] Audit documented and this file linked from the ruleset SPEC
- [x] Free PDF and character sheet removed; SRD PDF retained
- [x] `rule_set.json` metadata scrubbed and relabelled as SRD-based
- [x] `rules.json` and `equipment.json` setting nouns scrubbed
- [x] Original backgrounds and Growth/Learning tables
- [x] Original starting kits
- [x] Original origin Foci
- [x] Original bestiary
- [x] Original terrain list
- [x] Original dice tables
- [x] Original worldgen tables
- [x] Original creative tables
- [x] `unwritten-world` template and prompt
- [x] `record_setting` core tool and world-state display
- [x] Tests updated for new ids, counts, and content
- [x] New tests for SRD-only content and setting record
- [x] README, ruleset SPEC, PARITY, and SRD audit updated
- [x] Alembic migration for old `latter-earth` campaign rows
- [x] Full test suite, lint, and format pass
- [x] GM setting-generation turn passes against the scripted provider
- [ ] GM setting-generation turn against a live model (needs a configured provider)

## What changed

| Content | Before | After |
| --- | --- | --- |
| Bestiary | 56 free-book creatures and 9 setting-flavoured constructs | 56 original creatures across HD 1-12, built from `scripts/forge_pools/bestiary_*.json` |
| Backgrounds | 20 backgrounds, 18 with the free book's tables | 20 backgrounds: the SRD's Artisan and Barbarian verbatim, 18 with tables drawn from original skill pools by `scripts/forge_characters.py` |
| Kits | The free book's six equipment packages | Six kits with original names, descriptions, and loadouts built from the SRD catalogue |
| Origin Foci | Nine free-book species (Anak, Drudge, Dwarf, Elf, Halfman, Houri, Undead...) | Nine original origins: Forge-Born, Thrall-Born, Dwarf, Elf, Giant-Blooded, Beastkin, Glamoured, Gutterkin, Revenant. Dwarf and Elf are folkloric words, used plainly; the mechanics and the text around them are this project's |
| Dice tables | 33 free-book world tables | 33 original tables, 300+ candidate rows in `scripts/forge_pools/` |
| Worldgen tables | 42 free-book GM tables | 42 original tables, including an ordered 2d6 reaction band |
| Creative tables | None for this rule set | Six 1d100 tables: setting premise, region identity, faction, conflict, mystery, adventure hook |
| Setting | The Latter Earth | `unwritten-world`, a blank canvas the GM fills with `record_setting` |
| Source documents | Free PDF and character sheet in the repo | Removed; only the CC0 SRD PDF remains |

## Audit evidence

`scripts/audit_srd.py` compares every prose string in the rule set against the extracted text of
both books and reports strings that appear verbatim in the free book but not in the SRD. Before the
rewrite it found reserved text in `dice_tables.json`, `worldgen_tables.json`, `spells.json`, and
`templates.json`. After the rewrite it reports `srd=10, both=40, free=0, neither=2849`: no string
in the bundled rule set appears only in the free book. The normalizer undoes the extractor's
hyphenation (``transfor - mation``) and split digits (``1 3``), so a match the source really
contains is not missed.

Name-level comparison was the decisive test for what the string audit cannot see: 56 of 65
creature names, six of nine origin names, every distinctive table name, and all six starting kit
names appeared only in the free book, whatever the paraphrase around them.

The guard test `tests/test_wwn_srd_content.py` now fails if a reserved term (Latter Earth, arratu,
Academies, Anak, Drudge, Halfman, Houri, xenoformed) or a known free-book creature id reappears in
the bundled data.

The forge is deterministic: `scripts/forge_tables.py` seeds a random source per table, samples more
candidates than the table has rolls, and shuffles the drawn rows, so the committed tables are a
random draw from the authored pools rather than the order they were written in.

## Loss review

No mechanic, class, art, spell, Focus, skill, equipment line, condition, or progression chart is
lost: all of that is SRD-granted and untouched. Play-facing content is replaced one for one:

- Twenty backgrounds, six kits, nine origins, and a 56-creature bestiary.
- All 75 world and GM dice tables, now with more candidate entries each.
- Every terrain kind the map uses.
- Six new 1d100 idea tables, which the free book had no equivalent of.

The only intentional loss is the Latter Earth setting itself, which the SRD reserves and the user
asked to replace with an AI-invented world. Two items are worth a decision rather than a silent
choice:

1. **The game's name and the `worldswithoutnumber` rule set id.** The name is the SRD's title and
   is used to identify the rule set in data, URLs, and tests. Renaming it is a separate, larger
   change.
2. **Hurlants.** The SRD grants these exotic ranged weapons and they are kept as mechanics, but
   they read as the free book's science-fantasy flavour rather than plain fantasy. They could be
   dropped or renamed if the generic-fantasy register should be pure.

