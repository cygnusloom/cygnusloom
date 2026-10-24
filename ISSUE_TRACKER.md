This file contains issues encountered during play or feature requests. Triage them and use the questions tool to get details or clarification before moving them from Untriaged to Triaged. Combine duplicate or similar items if they can be implemented together.

Tackle triaged issues in the order presented (untraiged issues have no particular order). Ask more questions if any further details are needed (update the entry here). Once the item is done, remove it from this file.

# Untriaged Issues

* Center the world map where the PC is, so locations report their relative distances from them.
* Add background terrain to the maps.
* Move the show hidden events checkbox into the model settings dialog; expanded now to not just be model choice but also whether to show hidden events
* Mousing over terrain shows what kind of terrain it is and what it does somewhere near the map.

# Triaged Issues

Order below is the work order. Each entry lists the decisions made in triage.

2. Hardening against player input: enforced by prompt guidance only. The GM does not obey a player action the PC could not do. If the player insists, the GM may correct the mistake or redirect the story. No engine check.
3. Terrain, objects, and encounter templates: standard encounter templates live in the ruleset (generic, GM free to adjust). Maps gain terrain and object data that the GM sets. The GM may adjust terrain and add objects for an encounter.
4. SVG icons: a hand-drawn fixed set stored as data, covering terrain, objects, item types, and map tokens.
8. Travel time: each leg takes time from its route. Each ruleset has a reference table of travel rates (for example paved road, rough terrain, vehicle, animal). The GM may interrupt travel or rest with a story moment or encounter.
9. Briefer, non-spoiling location descriptions: player-facing text is shortened. GM-only detail stays out of it.
10. Narration covers everything: enforced by prompt guidance, the same as item 2.
