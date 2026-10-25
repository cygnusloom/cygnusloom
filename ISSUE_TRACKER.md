This file contains issues encountered during play or feature requests. Triage them and use the questions tool to get details or clarification before moving them from Untriaged to Triaged. Combine duplicate or similar items if they can be implemented together.

Tackle triaged issues in the order presented (untraiged issues have no particular order). Ask more questions if any further details are needed (update the entry here). Once the item is done, remove it from this file.

# Untriaged Issues

None.

# Triaged Issues

Order below is the work order. Each entry lists the decisions made in triage.

1. Hardening against player input: enforced by prompt guidance only. The GM does not obey a player action the PC could not do. If the player insists, the GM may correct the mistake or redirect the story. No engine check. Still open: the GM narrates ahead of tool calls (see the GM narration audit).

2. Narration covers everything: enforced by prompt guidance, the same as item 1. Still open: narration coverage gaps (see the GM narration audit).

3. Dungeon and wilderness cycles: the AI GM decides from the situation which cycle applies. The dungeon cycle applies in dangerous areas and dungeons; the wilderness cycle applies to broader long-term travel; neither applies in safe areas. Enforced by prompt guidance, as with items 1 and 2. The wilderness cycle is already built (W1 and W2), so this is about the GM's guidance and choosing the right cycle.

4. Background terrain on the world map: colour or texture per terrain type, laid out as a proper world map. World map only; encounter maps already have terrain. World data defines regions (polygons); the GM can add a region the data lacks, which is saved per campaign and drawn for everyone. A new region that overlaps an existing one is refused unless forced.

5. Terrain hover: a floating tooltip on hover, on both the world map and encounter maps, showing the terrain kind and its rule effect as the book gives it.

6. Lore panel: replaces the placeholder with a player-facing running journal of the whole campaign. The AI GM alone writes and edits it; players only read. It adds entries automatically after each GM turn when something notable happens. The AI GM can search it.

7. Master audit: a full audit of the whole Cairn 2e and Twin Lakes implementation, checking the engine, data, prompts, and tests against the source books and the module. It includes the Twin Lakes play-through, the overland route data, the W3 torch and lantern light rules, and resupply at homes and villages (in the book, not built). It runs before the Twin Lakes play-through and the generic refactor. Covers:
  * GM narration and tool calls
  * Dice and rule loading
  * Items and inventory
  * Hirelings and NPCs
  * Death, rest, and marketplace
  * Bestiary
  * Rule attachment
  * Twin Lakes module
  * Browser flow and tests


8. Generic refactor: remove Cairn-specific words from generic code and prompts, so the engine is not cairn2e-shaped. Runs after the master audit and the Twin Lakes play-through. A second ruleset (D&D) is only a hypothetical example and has no rules here.
