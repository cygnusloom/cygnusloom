This file contains issues encountered during play or feature requests. Triage them and use the questions tool to get details or clarification before moving them from Untriaged to Triaged. Combine duplicate or similar items if they can be implemented together.

Tackle triaged issues in the order presented (untraiged issues have no particular order). Ask more questions if any further details are needed (update the entry here). Once the item is done, remove it from this file.

# Untriaged Issues

1. Add svg icons to everything that can have icons (monsters, items, location types, etc.). Also add looks_like fields to definitions so that things without icons can inheirit their looks.
2. Make the branding generic. "cygnusforge" is the generic internal name of the software; but the user-facing branding should be configurable. "Cygnus Forge" should be changeable to whatever is needed, by environment variable.
3. The GM can narrate incorrect mechanics even when the recorded state and tool results are correct. In DeepSeek tests it misexplained Vowed armor calculations and said an advancement point remained after it was spent. Improve grounding or validation so player-facing narration follows the engine's results.
4. Close the remaining [Worlds Without Number parity gaps](src/cygnusforge/data/rulesets/worldswithoutnumber/PARITY.md):
   - Suspend a shield's AC benefit after a shield bash until the next turn (the bash attack profiles exist; the state does not).
   - Implement source-appropriate handling for surprise, grappling, execution attacks, total defense, dual wielding, range bands, and other combat actions currently left to rule text and general tools.
   - Represent monster special abilities as machine-readable rules instead of prose alone (all bestiary `rules` lists are empty).
   - Make Developed Attribute, Specialist, and Spirit Familiar benefit choices structured instead of GM-guided notes.
   - Implement magic item creation rather than leaving it as rule text.
   - Model the source's project, research, Magic Working, and faction-turn costs and requirements rather than only tracking free-text progress.
5. Leveling up (if rulesets have that idea, and it requires choices) should be an interactive process.

# Triaged Issues

Order below is the work order. Each entry lists the decisions made in triage.

None.
