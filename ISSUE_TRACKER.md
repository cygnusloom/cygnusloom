This file contains issues encountered during play or feature requests. Triage them and use the questions tool to get details or clarification before moving them from Untriaged to Triaged. Combine duplicate or similar items if they can be implemented together.

Tackle triaged issues in the order presented (untraiged issues have no particular order). Ask more questions if any further details are needed (update the entry here). Once the item is done, remove it from this file.

# Untriaged Issues

* A new ruleset and world for a zombie apocalypse with the scavenging, survival tone of Cataclysm: Dark Days Ahead. Candidates from the web search, with their licenses:
  * Cairn 2e, already shipped (CC BY-SA 4.0): the cheapest base for a survival hack; its d20 roll-under checks, slot inventory, deprivation, and hazards already exist.
  * Liminal Horror SRD v3.0 (CC BY 4.0; a Cairn descendant with a Doom Clock, Fallout, item tags, and conditions). Caveat: its third-party license forbids AI-generated art and text in compatible publications, so it can only serve as a mechanics reference.
  * Breathless SRD, Fari RPGs (CC BY 4.0): survival horror written for the undead; dice that degrade as you push are a good doom-clock source, but its sheets do not map to attribute-plus-skill characters, so only the procedures would be ported.
  * Sigil SRD / Sigil Atelier, Stormforge (Creative Commons): gritty d100 roll-under rules. Z-LAND itself is commercial and not reusable.
  * Cataclysm: DDA is not a tabletop ruleset, but its code and content are CC BY-SA 3.0, so its monsters, items, mutations, and locations can seed a world with attribution and share-alike; Night of the Living Dead (1968) is public domain in the US for tone.
  * World: an original setting written for the project (AGPL, like Academia Arcana) is the safe default; a Cataclysm-derived world needs a CC BY-SA 3.0 notice on that content.
  * Engine gaps for the feel: no crafting or recipes, vehicles, base building, noise and aggro, or infection model; survival needs, conditions, counters, timers, hazards, items, and currency already exist.
  * Open questions for triage: a new ruleset or a Cairn survival hack; how much crunch (crafting, infection, vehicles) is in scope; an original setting or an adaptation.

# Triaged Issues

Order below is the work order. Each entry lists the decisions made in triage.

1. Use a character from one campaign in another. The transfer bundle carries the sheet, level, XP, advancements (including banked picks), items and currency, and the journal and notes about the character. Decided: same rule set only; import through a character bundle, not a whole-campaign import. Kept separate from item 2.
2. Persistence between campaigns (shared world). A new campaign can continue a world from an existing campaign so new characters play in the same world with its state carried forward. Decided: keep this separate from the character transfer; the transfer/growth model (seeded snapshot of the world versus a live shared world) is left to implementation.
3. User-provided content. Users provide any JSON-defined data object by upload or paste: rule sets, worlds, classes, kits, items, and anything else defined by JSON. Decided: both whole bundles and individual objects are allowed; the existing content loader and validator check them. First step only: content is private to the uploading account and its campaigns. A shared library is a much later step because of moderation; private content stays out of other accounts' pickers and compendium.
4. Better storytelling. Blocked on the user's research. Symptoms to keep in mind: generic, safe prose; samey NPCs and plots; weak pacing and consequences. The research decides which to target; no code work starts until it lands.
5. Human-written SPEC and README. The owner writes or rewrites `SPEC.md` and `README.md` themselves. Decided: agents do not author or rewrite their prose; they may only correct factual drift and keep the instructions accurate.
6. Human review of the UI wording. A person reads the visible UI copy and edits it directly to remove AI-heavy language: buttons, labels, headings, help text, flash messages, and dialogs. Decided: the owner makes the wording changes directly. Out of scope: GM prompts and tool descriptions, bundled content prose, and code comments.
