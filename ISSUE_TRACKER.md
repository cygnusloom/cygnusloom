This file contains issues encountered during play or feature requests. Triage them and use the questions tool to get details or clarification before moving them from Untriaged to Triaged. Combine duplicate or similar items if they can be implemented together.

Tackle triaged issues in the order presented (untraiged issues have no particular order). Ask more questions if any further details are needed (update the entry here). Once the item is done, remove it from this file.

# Untriaged Issues

None.

# Triaged Issues

Order below is the work order. Each entry lists the decisions made in triage.

2. Master audit: a full audit of the whole Cairn 2e and Twin Lakes implementation, checking the engine, data, prompts, and tests against the source books and the module. It includes the Twin Lakes play-through, the overland route data, the W3 torch and lantern light rules, and resupply at homes and villages (in the book, not built). It runs before the Twin Lakes play-through and the generic refactor. Covers:
  * GM narration and tool calls
    * Done: the journal step runs only at the opening and at day end (a turn that moves the clock into a new day, including multi-day jumps). It covers all log lines since the journal was last written. OOC replies answer in their first sentence.
    * Done: DeepSeek's HTTP 400 ("reasoning_content must be passed back") came from tool-call messages replayed without a reasoning_content key when the model produced no reasoning. Those messages now always send the key. Assistant text messages (OOC replies, journal intro, replayed history) had the same gap and now also always send it.
    * Open: routine narration is still 100 to 130 words in live play with DeepSeek when a turn includes a discovered location or dialogue, so it is not yet minimal for routine beats.
  * Dice and rule loading
  * Items and inventory
  * Hirelings and NPCs
  * Death, rest, and marketplace
  * Bestiary
  * Rule attachment
  * Twin Lakes module
  * Browser flow and tests
