# Content pools

These files hold the original text that `scripts/forge_tables.py` and
`scripts/forge_bestiary.py` draw from. Nothing here is copied from the free
*Worlds Without Number* PDF; the tables and creatures it contains are the game
author's creative property and are not covered by the CC0 SRD.

## House register

The rule set is a plain fantasy game: pre-industrial kingdoms, free cities, and
wild frontiers. Magic is real, rare, and unsettling. Ruins are the remains of
fallen realms of sorcery, not of machines. There are no firearms, no electrical
or alien technology, no far-future Earth, and no named setting, nation, or
character from any published game.

Do not use real-world cultures, faiths, or stereotypes as the basis for an
entry, and do not reuse proper nouns from other games or fiction.

## Table pools

A `*_tables.json` file is one JSON object mapping a table id to a list of
candidate results. Supply more candidates than the table has rolls; the forge
draws a random subset and shuffles the order.

- One result per entry, no leading numbers, no "roll a d6" text.
- One-line entries read best: 8-20 words for a full sentence, 2-8 words for a
  tag-style table.
- Entries must be unique.

Run `uv run python scripts/forge_tables.py --check` for the exact number each
table wants.

## Bestiary pools

A `bestiary_*.json` file is a list of creatures in the schema described at the
top of `scripts/forge_bestiary.py`. Mechanical stats come from
`monster_stats.json`, so a creature only names its Hit Dice.

Run `uv run python scripts/forge_bestiary.py --check`.
