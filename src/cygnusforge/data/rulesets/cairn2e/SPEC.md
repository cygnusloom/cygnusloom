# Cairn Second Edition: implementation spec

Source: https://cairnrpg.com/second-edition/ (Player's Guide, Warden's Guide, Marketplace, Backgrounds, Game Files).

This spec plans the Second Edition rules as a set of phases. Each phase is implemented, live-tested in a browser session, reviewed, and committed before the next one starts.

## Principles

- The engine stays generic. Cairn rules live in this ruleset as JSON: tables, combat config, conditions, and stats. Rules that need Python (dice, saves, state changes) are generic engine features that a ruleset configures.
- Anything the book states as a mechanic is engine-enforced, not prose for the GM to remember. The GM calls tools and reads results. Prose is for flavor only.
- Every structured roll or state change is logged in the event log.
- No backwards compatibility (Version 0).

## Decisions

| Topic | Decision |
| --- | --- |
| Sequencing | Phased. Each phase is implemented, live-tested, and committed before the next. |
| Scars table | Engine-rolled, from the book's 1-12 table, triggered when an attack takes a PC to exactly 0 HP. Phase 1. |
| Persistent scar effects | Structured conditions with explicit end triggers (for example "rest a few hours", "extraordinary aid", "die in one hour"). Phase 1. |
| Morale | Engine-enforced WIL saves for enemies at first casualty, at half their number, and lone foes at 0 HP. Phase 1. |
| Attack modifiers | Impaired, Enhanced, Blast, two weapons, and multiple attackers are rolled and applied by the engine. Phase 1. |
| Detachments | Later phase (phase 2). |
| Reactions and Die of Fate | Engine tools the GM can call; results logged. Phase 3. |
| Deprivation and Fatigue | Engine-tracked, including inventory slots. Phase 4. |
| Magic | Spellbooks, scrolls, and relics, with the Fatigue and WIL-save rules enforced. Phase 5. |
| Gold and marketplace | Engine-tracked prices and services. Phase 6. |
| Character creation | Engine-rolled background tables (3d6 attributes, 1d6 HP, starting gear). Phase 7. |
| Hirelings | Built from the marketplace table or a background. Phase 8, before death rules. |
| Death | The book's rules: critical damage is fatal unless aided, STR 0 means death, and the player makes a new PC or takes a hireling who joins immediately. Phase 9. Until then, the house rule stays: a downed PC is knocked unconscious and the GM assigns the recovery narrative. |
| Dungeon exploration | Dungeon turn cycle and Dungeon Events table, engine-rolled. Phase 10. |

## Phase 1: combat core

Scope:

1. **Critical Damage.** Damage that takes HP below 0 is subtracted from STR by the remaining amount. The target then makes a STR save using the new STR. Failing means Critical Damage. A PC in Critical Damage is knocked out until aided. This phase keeps the house rule: the GM assigns the recovery narrative (`recover_character`). NPCs that fail a critical save die, at the Warden's discretion.
2. **Scars.** When an attack takes a PC to exactly 0 HP, roll on the Scars table (12 entries) using the HP lost in the attack. The table lives in `tables.json`. Each result is applied as a structured effect, not prose.
3. **Structured conditions (generic engine feature).** A condition has an id, a label, effects, and end triggers. Effects can be attribute or max-attribute changes, action restrictions (for example "deprived", "out of action", "hamstrung", "deafened", "die in one hour"), or a timer. End triggers can be `rest` (a few hours), `extended_rest`, `aid` (extraordinary aid), `time` (for example one hour, then death), or GM-applied. Conditions are defined in JSON and tracked in character state. They appear on the character sheet and in the GM's state text.
4. **Attribute loss outside combat.** Damage outside combat hits STR. STR 0 means death. DEX 0 means paralyzed. WIL 0 means delirious. Complete DEX or WIL loss means the character cannot act until restored by extended rest or extraordinary means.
5. **Morale.** The engine tracks casualties for each enemy group. At the first casualty and at half the group, each group makes a WIL save (or the leader's WIL, if the group uses it). A failed save makes the group flee. Lone foes save at 0 HP. Morale never affects PCs.
6. **Attack modifiers.**
   - Impaired: roll 1d4 instead of the damage die (cover, bound hands, distant ranged targets).
   - Enhanced: roll 1d12 instead of the damage die (helpless foe, daring maneuver).
   - Blast: roll the damage die separately for each target.
   - Two weapons: roll both dice and keep the single highest (written d8+d8).
   - Multiple attackers on one foe: roll all dice and keep the single highest.
   - Unarmed: d4.
   - All actions are declared before resolution.
7. **Retreat.** Running requires a DEX save and a safe destination. A character may also surrender, which takes them out of the fight; the encounter ends once one side has nobody who can fight.
8. **Armor.** Armor is subtracted from the damage roll before HP, with a maximum of 3.
9. **Rounds.** PCs act first each round, then opponents act simultaneously. Current behavior is kept.
10. **Attacks.** Attacks always hit (book rule), so the attack roll is only the damage roll.

Acceptance:

- Unit tests for the damage pipeline, Scars, conditions (apply and end triggers), morale, attack modifiers, and retreat.
- A live session covering critical damage, a Scars result, a morale break, and an impaired or enhanced attack.
- Transcript wording reviewed.

## Phase 2: detachments

- Large groups fight as one detachment.
- Critical Damage routes or weakens a detachment. 0 STR destroys it.
- Individual attacks on a detachment are impaired (except Blast). Detachment attacks on individuals are enhanced and Blast.

## Phase 3: Warden tools

- Reactions: 2d6 table (2 Hostile, 3-5 Wary, 6-8 Curious, 9-11 Kind, 12 Helpful). Tool with a logged result.
- Die of Fate: optional 1d6. 4+ favors PCs, 3- is bad luck. Tool with a logged result.
- Creating monsters: d20 tables for Appearance, Traits (Quirks, Weakness), Attacks (Type, Critical Damage), and Abilities (Ability, Target). Tool that rolls the columns and combines them.

## Phase 4: deprivation and fatigue

- A PC lacking food or rest is Deprived. Deprived for more than a day adds Fatigue, one per day.
- A deprived PC cannot recover HP, attributes, or item slots from Fatigue.
- Fatigue takes one inventory slot each and lasts until a full night's rest in a safe spot.
- Inventory: ten slots. Four or five items are comfortable. Bulky items take two slots. Petty items take none. A full inventory (10 slots) reduces the character to 0 HP.
- If there is no free slot for Fatigue, the character must drop an item.

## Phase 5: magic

- Spellbooks hold one spell and take one slot. Casting means holding the book in both hands, reading aloud, and adding one Fatigue.
- While deprived or in danger, the Warden may require a WIL save. Failure has consequences on par with the spell.
- Scrolls are petty, add no Fatigue, and disappear after one use.
- Relics have limited uses and a Recharge condition. They add no Fatigue.

## Phase 6: gold and marketplace

- Gold pieces and prices from the Marketplace: armor, weapons, transport, upkeep and recovery services, hirelings per day, and gear.
- Services: room and board, stable, medical healing (50 gp), rations.

## Phase 7: character creation

- Background roll or choice, name list, starting items.
- Attribute rolls: 3d6 per attribute, 1d6 HP. Max HP and max attributes follow the rules.
- Background tables from `backgrounds.json`.

## Phase 8: hirelings

- Built from a marketplace role or a background, with 3d6 attributes and 1d6 HP, then Character Traits rolls.
- Hirelings are NPCs with the PC sheet shape. They can join the party.

## Phase 9: death rules

- Critical Damage is fatal unless aided (bandages stabilize). Untreated, the PC dies within the hour.
- When a character dies, the player creates a new character or takes control of a hireling. The replacement joins immediately.
- Scars 11 (Mortal Wound) and 12 (Doomed) are wired into this phase.

## Phase 10: dungeon exploration

- Dungeon turn cycle: the party's actions in turn order, with the cost of time recorded.
- Dungeon Events table, rolled when the party spends too long in one place, makes noise, or enters a new area.
- Dungeon seeds and generated rooms are GM content, not engine logic.

## Open items (not in any phase yet)

- The bonds and omens section, the bibliography, and game files and tools were not reviewed in detail. They will be revisited before their phases.
- The Scars table text is the book's text. The table must be transcribed exactly into `tables.json` during phase 1.
