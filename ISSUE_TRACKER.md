This file contains issues encountered during play or feature requests. Triage them and use the questions tool to get details or clarification before moving them from Untriaged to Triaged. Combine duplicate or similar items if they can be implemented together.

Tackle triaged issues in the order presented (untraiged issues have no particular order). Ask more questions if any further details are needed (update the entry here). Once the item is done, remove it from this file.

# Untriaged Issues

None.

# Triaged Issues

Order below is the work order. Each entry lists the decisions made in triage.

1. Background terrain on the world map: colour or texture per terrain type, laid out as a proper world map. World map only; encounter maps already have terrain. World data defines regions (polygons); the GM can add a region the data lacks, which is saved per campaign and drawn for everyone. A new region that overlaps an existing one is refused unless forced. Once the regions exist, the world map also gets the terrain hover from item 3 (a floating tooltip showing the terrain kind and its rule effect), which was left out of item 3 because the world map has no terrain data yet.

2. Master audit: a full audit of the whole Cairn 2e and Twin Lakes implementation, checking the engine, data, prompts, and tests against the source books and the module. It includes the Twin Lakes play-through, the overland route data, the W3 torch and lantern light rules, and resupply at homes and villages (in the book, not built). It runs before the Twin Lakes play-through and the generic refactor. Covers:
  * GM narration and tool calls
  * Dice and rule loading
  * Items and inventory
  * Hirelings and NPCs
  * Death, rest, and marketplace
  * Bestiary
  * Rule attachment
  * Twin Lakes module
  * Browser flow and tests


4. Generic refactor: remove Cairn-specific words from generic code and prompts, so the engine is not cairn2e-shaped. Runs after the master audit and the Twin Lakes play-through. A second ruleset (D&D) is only a hypothetical example and has no rules here.
