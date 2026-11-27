This file contains issues encountered during play or feature requests. Triage them and use the questions tool to get details or clarification before moving them from Untriaged to Triaged. Combine duplicate or similar items if they can be implemented together.

Tackle triaged issues in the order presented (untraiged issues have no particular order). Ask more questions if any further details are needed (update the entry here). Once the item is done, remove it from this file.

# Untriaged Issues

* Hordes: `roll_move` rejects a stat the move does not use (a live run asked for Scavenge with Know-how). The refusal is clear and the GM retries, but the move tool schema or the Hordes prompt should name the stat each move rolls so the model does not have to guess.

# Triaged Issues

Order below is the work order. Each entry lists the decisions made in triage.

1. Use a character from one campaign in another. The transfer bundle carries the sheet, level, XP, advancements (including banked picks), items and currency, and the journal and notes about the character. Decided: same rule set only; import through a character bundle, not a whole-campaign import. Kept separate from item 2.
2. Persistence between campaigns (shared world). A new campaign can continue a world from an existing campaign so new characters play in the same world with its state carried forward. Decided: keep this separate from the character transfer; the transfer/growth model (seeded snapshot of the world versus a live shared world) is left to implementation.
3. User-provided content. Users provide any JSON-defined data object by upload or paste: rule sets, worlds, classes, kits, items, and anything else defined by JSON. Decided: both whole bundles and individual objects are allowed; the existing content loader and validator check them. First step only: content is private to the uploading account and its campaigns. A shared library is a much later step because of moderation; private content stays out of other accounts' pickers and compendium.
5. Human review of the UI wording. A person reads the visible UI copy and edits it directly to remove AI-heavy language: buttons, labels, headings, help text, flash messages, and dialogs. Decided: the owner makes the wording changes directly. Out of scope: GM prompts and tool descriptions, bundled content prose, and code comments.
