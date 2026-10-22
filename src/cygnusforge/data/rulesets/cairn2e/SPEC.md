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
| Detachments | One combatant from a flagged bestiary entry, formed by the GM. A failed Critical Damage save defeats it or routs its members, per scene. Phase 2. |
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
5. **Morale.** The engine tracks casualties for each enemy group. At the first casualty and at half the group, each trigger gets its own WIL save (made with the leader's WIL, if the group uses it). A failed save makes the group flee. Lone foes save at 0 HP. Morale never affects PCs.
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

Source: the Warden's Guide, Detachments (https://cairnrpg.com/second-edition/wardens-guide/detachments/).

- A detachment is one combatant. The GM forms it with an encounter tool call, using a bestiary entry flagged `detachment` (for example Killer Bees and Traitor-Rats).
- It uses its bestiary stat block: HP, armor, STR, DEX, WIL, and attacks.
- Attacks on a detachment are impaired. A detachment's attacks on individuals are enhanced and Blast.
- Damage past HP goes to STR. Damage that reaches STR triggers a Critical Damage save on STR.
- If that save fails, the GM chooses the outcome per scene: the detachment is defeated, or its members flee, as a morale break does.
- Engine-enforced: after a failed Critical Damage save the detachment is pending. Its attacks and hits on it are refused until the GM calls `resolve_detachment` with `defeat` or `flee`.
- Blast: a detachment's attack rolls its enhanced dice separately for each living individual on the other side. Blast applies to individuals only; detachments do not attack detachments.
- Detachments make no morale checks. Checked against the Combat and Detachments pages: morale is triggered by a group's casualties, and a detachment "remains a single unit until it takes critical damage", so its break point is its Critical Damage save.

Acceptance:
- Unit tests for a flagged statblock becoming a detachment, impaired attacks on it, Blast, the pending save, both resolutions, and no morale check.
- A live session with a detachment fight: the GM forms it, attacks it, the detachment strikes individuals, a Critical Damage save fails, and the GM resolves it.
- Transcript wording reviewed.

## Phase 3: Warden tools

- Reactions: 2d6 table (2 Hostile, 3-5 Wary, 6-8 Curious, 9-11 Kind, 12 Helpful). Tool with a logged result.
- Die of Fate: optional 1d6. 4+ favors PCs, 3- is bad luck. Tool with a logged result.
- Creating monsters: d20 tables for Appearance, Traits (Quirks, Weakness), Attacks (Type, Critical Damage), and Abilities (Ability, Target). The GM calls `roll_tables` with all eight, then combines the results into one creature with `create_monster`.
- Engine: a generic `dice_table` definition (dice formula plus contiguous rows) in `warden_tables.json`, and one generic `roll_tables` tool that rolls any number of them. It logs a `dice.tables_rolled` event. The GM decides when to call it. The tool returns the results and does not save or place a creature. Only the ruleset holds Cairn data.

## Phase 4: deprivation, fatigue, rest, and inventory

Generic engine mechanics, parameterized by the ruleset's JSON. Cairn numbers live only in `cairn2e/`.

- Clock: the campaign has a day counter and a part of the day. Parts come from the ruleset (`clock`: morning, afternoon, evening, night). Time moves only through tools: `advance_time` and `rest`. A new day's result lists discovered places not visited for `stale_days` (ruleset, 5), so the Warden updates them.
- Rest: the GM calls `rest(length, interrupted, reason)` when the party rests. Lengths come from the ruleset (`rest`): `moments` (one part, restores HP), `night` (until morning, full rest, clears Fatigue), `week` (seven days, full rest, restores attribute loss and clears Fatigue). An interrupted rest counts as `moments`: one part, not full. Resting is not only sleeping; the GM picks the length and whether it is interrupted.
- Recovery: a PC that is not Deprived recovers per the rest length. A deprived PC recovers nothing (HP, attributes, or Fatigue).
- Deprivation: `set_deprivation(name, deprived, cause)`. The GM decides using `rest_history`, which lists each PC's last full rest and recent rests. Deprived for more than one day (ruleset `after_days`) adds one Fatigue per day, applied by the engine as each day begins.
- Fatigue: an inventory item of kind `fatigue` (ruleset `fatigue_item`). `add_fatigue(name, cause, force)` covers events in the fiction, such as spells. A deprived character is refused unless `force` is set, because the engine adds deprivation Fatigue each day.
- Inventory: ten slots (ruleset `capacity`). Item sizes are ruleset definitions: petty (0 slots), normal (1), bulky (2). `give_item` and `drop_item` change items; the GM names and sizes new items.
- No free slot for Fatigue: the Fatigue is still recorded, and the character reads as over capacity until the GM calls `drop_item`.
- Full inventory: when an item makes the inventory full, the character's HP (ruleset `full.stat`) drops to 0 once. No overflow and no save; healing is needed to recover it.
- Events: `ClockAdvanced`, `RestTaken`, `RestRecovered`, `DeprivationSet`, `ItemAdded`, `ItemRemoved`, `CharacterDrained`. Engine logic is in `services/clock.py`, `services/needs.py`, and `services/inventory.py`.

## Phase 5: magic

- Spellbooks hold one spell and take one slot. Casting means holding the book in both hands, reading aloud, and adding one Fatigue.
- While deprived or in danger, the Warden may require a WIL save. Failure has consequences on par with the spell.
- Scrolls are petty, add no Fatigue, and disappear after one use.
- Relics have limited uses and a Recharge condition. They add no Fatigue.

Decisions (design pass):
- Spell content is free text the Warden writes when the spell is created. The engine stores the name, description, and book slot; the Warden decides the effect.
- Casting is a tool call, `cast_spell(name, spell)`, made after the player says the spell. The engine checks that the spellbook is held and adds one Fatigue. A spellbook holds one spell, and the spell lives on the book item.
- A WIL save for a spell or danger is a tool call. The engine rolls and records it; the Warden applies the consequence.
- Scrolls and relics are inventory items with a kind (`scroll`, `relic`). Scrolls are petty (no slots, no Fatigue) and are removed after one use. The spell text stays on the scroll.
- A relic has a use count and a Recharge rule the Warden writes as text. The engine decrements uses and records recharges. A relic cannot recharge on the same clock step as its last use, so time must pass first. Relics add no Fatigue.

## Phase 6: gold and marketplace

The book gives prices but no rules for most services. Decisions below come from the design pass and the implementation.

- Gold: the currency is the `gold` feature, named by `marketplace.currency`. Backgrounds grant it (3d6). Gold is not an inventory item and takes no slots. Changes are `GoldChanged` events, clamped at 0.
- Catalogue: `marketplace.json` holds one `shop_item` per Marketplace line, with `category`, `price`, `size` (petty, normal, or bulky), and optional `armor`, `damage`, or `uses`. Four `service` entries and twelve `hireling` entries (daily wage) are also there. Hirelings are stored but not charged until Phase 8.
- Buying: `buy_item(name, item, price, size)`.
  - A catalogue item costs its catalogue price, and its size must match the catalogue.
  - An off-catalogue item needs a price the GM sets, based on similar catalogue entries. Its size defaults to normal.
  - The engine checks gold, then capacity. Unlike `carry_item`, which allows over-capacity, a purchase is refused when it would exceed capacity.
  - Armor adds to the buyer's armor, capped at 3. A weapon's damage die is in its text, and the GM uses it in attacks.
- Services: `buy_service(names, service)`.
  - `room` (10 gp per night, one room for one PC) and `private_room` (35 gp, one room for up to four PCs) charge `price * ceil(n / fits)`. The first named character pays the whole bill. This is a design choice, not from the book.
  - A lodged PC is marked for the next rest. On a food rest it skips the ration and its bed is used up (`LodgingSet`).
  - `medical_healing` (50 gp) charges `price` per patient. A patient with any harm in HP, str, dex, or wil has each of those restored. The book gives no mechanical text for healing, so this restores everything, as the Warden chose. A PC with no harm is refused.
  - `stable` is deferred until mounts are modeled, and is refused.
  - `search_marketplace` is a lookup tool. It returns up to 20 matching items, a `more` flag, and the services, and the GM uses it before quoting a price.
- Rations: a `rations` item holds 3 uses. Starting rations from a background become one inventory item with 3 uses. A `night` or `week` rest feeds the party first (`_feed_party`): one ration use per PC that is not lodged, and an empty pack is removed. A PC with no ration and no lodging becomes Deprived, with the cause "no food". A `moments` rest uses no food.
- Open: transport capacity bonuses (cart, wagon, horse, and mule add slots) and stable and mounts.

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
