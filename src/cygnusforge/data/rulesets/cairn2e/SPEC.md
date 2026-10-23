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
| Death | The book's rules: critical damage is fatal unless aided within one hour, STR 0 means death, and the player makes a new PC or promotes a hireling. The GM decides when a new PC joins. Phase 9. The knockout house rule is retired. |
| Dungeon exploration | Dungeon turn cycle and Dungeon Events table, engine-rolled. Phase 10. |
| Inventory panel | The player sees their items in an Inventory tab: name, size, slots, uses left, and kind, against the capacity. Built after phase 7. |
| Gaps | Every known gap from every phase is listed in the gap register, mapped to the phase that will close it or marked unscheduled. |
| Generic engine | Phase 11 moves every Cairn-specific term, number, and prompt rule out of the engine and into the ruleset JSON, parameterized where a second rule set would need the same hook. Phase 13 finishes the remaining shape gaps (damage pipeline, death, conditions, spell pools) for D&D. |
| Capstone | Phase 12 imports the CAS-1 data. Phase 13 refactors the engine for a second rule set. Phase 14 plays CAS-1 through in full as the end-to-end test of Cairn 2e. |
| D&D comparison | No D&D ruleset is in this repo, so the refactor uses D&D as a design reference only. The test for "generic" is whether a second rule set would need the same hook. |

## Phase 1: combat core

Scope:

1. **Critical Damage.** Damage that takes HP below 0 is subtracted from STR by the remaining amount. The target then makes a STR save using the new STR. Failing means Critical Damage. A PC in Critical Damage is knocked out until aided. Phase 9 retired the house rule: see Phase 9 for the death rules, and `recover_character` only after a PC is stabilized. NPCs that fail a critical save die, at the Warden's discretion.
2. **Scars.** When an attack takes a PC to exactly 0 HP, roll on the Scars table (12 entries) using the HP lost in the attack. The table lives in `tables.json`. Each result is applied as a structured effect, not prose.
3. **Structured conditions (generic engine feature).** A condition has an id, a label, effects, and end triggers. Effects can be attribute or max-attribute changes, action restrictions (for example "deprived", "out of action", "hamstrung", "deafened", "die in one hour"), or a timer. End triggers can be `rest` (a few hours), `extended_rest`, `aid` (extraordinary aid), `time` (for example one hour, then death), or GM-applied. Conditions are defined in JSON and tracked in character state. They appear on the character sheet and in the GM's state text.
4. **Attribute loss outside combat.** Damage outside combat hits STR. STR 0 means death. DEX 0 means paralyzed. WIL 0 means delirious. Complete DEX or WIL loss means the character cannot act until restored by extended rest or extraordinary means.
5. **Morale.** The engine tracks casualties for each enemy group. At the first casualty and at half the group, each trigger gets its own WIL save (made with the leader's WIL, if the group uses it). A failed save makes the group flee. Lone foes save at 0 HP. Morale never affects PCs.
6. **Attack modifiers.**
   - Impaired: roll 1d4 instead of the damage die (cover, bound hands, distant ranged targets).
   - Enhanced: roll 1d12 instead of the damage die (helpless foe, daring maneuver).
   - Blast: roll the damage die separately for each target.
   - Two weapons: roll both dice and keep the higher (the book writes d8+d8; stored as 1d8+1d8, since cairn2e's `+` keeps the higher).
   - Multiple attackers on one foe: roll all dice and keep the higher.
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
- Engine-enforced: after a failed Critical Damage save the detachment is pending. Its attacks and hits on it are refused until the GM calls `resolve_group_break` with `defeat` or `flee`.
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

- Clock: the campaign has a day counter and a watch of the day. Watches come from the ruleset (`clock`: morning, afternoon, night, eight hours each, the book's three watches). Time moves only through tools: `advance_time` and `rest`. A new day's result lists discovered places not visited for `stale_days` (ruleset, 5), so the Warden updates them.
- Rest: the GM calls `rest(length, interrupted, reason)` when the party rests. Lengths come from the ruleset (`rest`): `moments` (one hour, restores HP), `night` (full rest, clears Fatigue; begun in the morning it stops at dusk, the start of the night watch, and begun later it runs until morning via `stop_at`), `week` (seven days, full rest, restores attribute loss and clears Fatigue). An interrupted rest counts as `moments`: one hour, not full. Resting is not only sleeping; the GM picks the length and whether it is interrupted. A rest length can be given in `hours`, `parts`, `days`, or `until: "morning"`.
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
  - The GM can give or take gold with the core `change_gold` tool (a gift, reward, tithe, or bribe), which refuses a take the character cannot pay. Purchases and wages use the same `GoldChanged` event.
  - No play path earns gold until Phase 10 (treasure and discoveries). Until then the Debug panel's currency form gives a PC any amount, as a `DebugCurrencyAdded` event that is hidden from the GM.
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

Background tables come from `backgrounds.json`. Attributes roll 3d6 each, and HP rolls 1d6. Those rules were already in place. This phase changes what starting gear becomes.

- Starting items: each item feature in the background becomes an inventory item with its real size and slots. Gold is the exception: it stays a count (see Phase 6). Spell features become spellbooks only when the rule set has magic. Otherwise they stay as features.
- Sizes: `features.json` holds one `size` per feature (petty, normal, or bulky; default normal). Petty items take no slots and bulky items take two. Every item feature's size must exist in `inventory.json`'s `item_sizes`, which is checked when the library loads.
- Uses: a feature may set `uses`, which becomes the item's uses and max uses. Rations, torches, and bandages have 3 uses, oil has 6, and salves have 1. Items with no use count have no uses, and `use_item` refuses them. The candle helmet has both 6 uses and armor 1. The book's text for it should be checked, since a helmet that burns candles may be meant that way.
- Armor: a feature may set `armor`. Carried armor is summed into the character's armor when created, capped at `combat.MAX_ARMOR`. The cap is a design choice. Ten backgrounds grant armor.
- Backpack: it is an item feature of normal size, so it takes one slot. The book's six-slot capacity is not modeled. This is an assumption, not from the book.
- Overflow: the largest starting total is 9 slots (Outrider), under the 10-slot capacity, so starting gear never overflows.
- Using items: `use_item(name, item)` spends one use. The last use removes the item. It refuses an item with no uses, an item not carried, and a relic (which goes through `use_relic`). Its tool text is in `tool_calls.json`.
- Unverified: the petty and bulky flags for a few items (lockpicks, pincers, roll of tin, twin-daggers, and some petty items). Those came from a subagent's reading of the book.

## Phase 8: hirelings

Status: engine implemented (`hire_hireling` and `dismiss_hireling` core tools; daily wage paid from the employer's gold via the clock's `wage_currency`; unpaid wages recorded as `hireling.unpaid` with no deduction, and the Warden decides; hirelings are PC-kind, so the player controls their turns; hirelings travel with the employer on `move_actor`; hirelings listed in the location panel). Unit tests in `tests/test_hirelings.py`. Live browser test: hire, location listing, and daily wage charged (`wages for Wen` gold event) verified on a fresh DB. Unpaid path NOT reached live; the GM's narrated gold gift diverged from the engine balance (see gap register).

Source: Core Rules, "Hirelings"; Marketplace, "Hirelings (per day)". The book is the authority.

- Two creation paths, both from the book:
  - Role path: choose a role from the Hirelings table. Roll 3d6 for each attribute and 1d6 for HP. Give equipment appropriate to the role (chosen, not rolled). Then roll the Character Traits tables.
  - Background path: choose or roll a background and a name. Roll (or choose from) its tables for Rations, Gold, Attributes, HP, and age. Starting gear comes from the background's item table, as for a PC.
- Decided: the user chose station equipment from the book for role hires. The book gives no per-role gear list, only "equipment appropriate to their station," so the GM chooses it with `give_item` right after `hire_hireling`. Hireling gear is not rolled.
- Hireling wages are charged per day from party gold by the daily clock tick.
- When the party cannot pay, the hireling leaves if able. Otherwise it seeks the first chance to leave. The GM decides the outcome. The engine records the unpaid wage as an event.
- Hirelings can also be paid to carry equipment. Their carry capacity counts toward the party's inventory. Confirm the exact mechanic when building it.
- Hirelings are NPCs with the PC sheet shape. The player controls a hireling once it joins. The GM can also create ad hoc NPC allies with the same tool.
- A hireling joins the party when hired, or when a PC dies (Phase 9).

## Phase 9: death rules

Status: engine, death screen, and unit tests done. Live session partly done: Critical Damage, the dying clock, a death, the death screen, a replacement PC, and a Bandage stabilizing a dying PC are confirmed live. Hireling promotion and a death by the clock with a hireling present are not yet live. See the Phase 9 acceptance line.

Source: Core Rules, Critical Damage, and Scars 11 (Mortal Wound) and 12 (Doomed) in `tables.json`. The book is the authority.

Decided:
- The house rule ends. A PC in Critical Damage is unconscious and bleeding out, and dies within one hour unless aided. This replaces the knockout house rule in `rule_set.json`.
- The one-hour clock is engine-tracked. It starts when Critical Damage is taken or when Mortal Wound (Scars 11) applies. Bandages stop it, and any character may use a Bandage on the dying PC through `use_item`. The GM may also stabilize through a `stabilize` tool for other aid, which is GM-judged.
- Mortal Wound: out of action, dying. The clock runs as above. On recovery, roll 2d6 and take the result as max HP (existing `mortal_wound` condition).
- Doomed (Scars 12): the next save against Critical Damage that fails kills the PC. A pass rolls 3d6 for max HP if higher (existing `doomed` condition). The GM ends Doomed.
- STR 0 outside combat means death.
- When the engine kills a PC, it records the death at once. The GM narrates the death in the same turn, and the death screen appears when the turn ends.
- The death screen lets the player pick a replacement: a new PC through the character creation flow, or promotion of a hireling. The engine applies the choice.
- A new PC gets a new loadout and joins the party when the GM decides the story allows it. It does not inherit gear. The dead PC's gear stays with the body, and the GM decides what happens to it.
- Defeated PCs leave a body. Bodies hold the dead PC's gear for the GM to handle.
- NPCs that fail a Critical Damage save die, at the Warden's discretion (unchanged).
- The clock counts hours, so `advance_time` takes `hours`. The Phase 9 part of 6 hours was superseded in Phase 10 by the book's eight-hour watch (see Phase 10).
- A hireling is promoted with `hireling.promoted` once the PC is dead, and only if it is alive and not dying. It keeps its gear and stops being paid.
- A dead employer's hirelings are unpaid, so the GM decides what they do.

Acceptance:
- Unit tests: the one-hour clock fires, bandages stop it, Doomed kills on a failed save, Mortal Wound sets max HP on recovery, and a death emits an event and opens the death screen.
- A live session covering Critical Damage, bandaging, a death, a replacement PC, and a hireling promotion.
  - Done live (fresh DB, campaign 1): a wolf bite overflowed to STR, the STR save failed (`character.dying`, one-hour clock), the encounter ended, the GM advanced three hours and the clock ran out (`character.died`), the GM narrated the death in the same turn, the death screen appeared on reload, and a replacement PC (Kalam) was created with starting gear and placed by the GM.
  - Done live (campaign "Phase Nine B", a hireling Tam, and Dorn in critical damage): the GM stabilized Dorn with a Bandage, and `character.stabilized` fired. The GM first tried `use_item` on Tam's bandage with Dorn as the target, and the engine refused it (see the naming fix below). The GM then gave Dorn a Bandage and stabilized him, which left Tam's bandage in his pack. This live pass did not run the clean `use_item` path.
  - Done live (campaign 1 "Phase Ten Live", fresh DB): Tam was hired with catalogue Bandages (3 uses), the post-fix naming. Dorn at 0 HP and STR 1 was shot outside combat, and the engine recorded instant death (`character.died`). The death screen offered "Play Tam", and promotion fired (`hireling.promoted`). Tam then killed a foe with a hand-axe in a later fight.
  - Narration mismatch found live: the engine killed Dorn at once, but the `combat.harmed` status read "in critical damage", so the GM narrated an hour of dying. Fixed: a PC killed outright outside combat now reads "dead" (`player_character_dead` in `combat.json`, `defeat_status(..., killed=True)`). A test covers it (`tests/test_death.py`).
  - Done live (campaign 1, same DB): hireling Ysolde was hired in Rillhollow with her own 3-use Bandages. Tam, at 0 HP and 2 STR, was hit in combat (Moll's arrow): the overflow took STR to 0, the save failed, and the engine recorded Critical Damage with a one-hour clock (`character.dying`). The narration said "an hour" and matched. Ysolde used one of her Bandages (3 to 2 uses), with no duplicate, and `character.stabilized` fired. Tam's live state is stabilized, so no further clock runs.
  - Not yet live: a death by the clock with a hireling present (the engine path is shared with PCs and covered by unit tests), and wages stopping after promotion on a new day. The PC employer had no gold, so Ysolde's wage was recorded as unpaid, as designed.

Fixed during the live check:
- A dying record was lost when nothing committed after it. `begin_dying` and the Doomed branch now commit. A regression test covers this (`tests/test_death.py`). It was hidden in most fights because the encounter-end commit ran first.
- A Bandage given as "Bandage" (singular) matched no catalogue entry, so it became a freeform item with no uses, and the aid list ("Bandages") did not match it either. Item names now match ignoring case and a plural s (`same_item_name` in `services/events.py`, used by the catalogue lookup, the held-item lookup, and the aid match). A regression test covers the singular name (`tests/test_death.py`). The live server was running the old code during this session. Verify the fix live at the next dying PC.

Gaps from Phase 9:
- Superseded in Phase 10: the 6-hour part became the book's 8-hour watch (user choice). Death timing is in hours (`dying_hours: 1`), so it did not change.
- Resolved: STR 0 outside combat means instant death, confirmed by the user.
- OPEN: a new PC is placed by the GM on its first turn. The book's rule for when a new PC joins is not modeled, so the GM decides by narrative.
- OPEN: a dead PC's gear is not lootable by tools. The GM handles it in narration only.
- Confirmed live: the death screen appears when the page reloads at the end of the turn, not mid-turn.
- OPEN: `test_a_new_player_character_is_refused_while_one_lives_and_allowed_after` failed once in a full run and has not failed since (40 isolated runs and 30 runs of the related files, all passing). The likely cause is the commit bug above, since whether a random fight ended with a commit decided whether the dying record survived. Not proven. Watch for a recurrence.

## Phase 10: dungeon exploration

- Dungeon turn cycle: the party's actions in turn order, with the cost of time recorded.
- Dungeon Events table, rolled when the party spends too long in one place, makes noise, or enters a new area.
- Dungeon seeds and generated rooms are GM content, not engine logic.
- Source (cairnrpg.com Procedures and Warden's Guide > Dungeon Exploration, checked for Phase 10): dungeon time is counted in **turns** and **cycles**, not in minutes. A turn is one action per character plus about 40ft of movement. The book gives no length in minutes or hours for a turn. Dungeon Events are rolled when the party spends more than one cycle in one room, moves quickly, enters a new area, or makes noise. The six-entry d6 table is Encounter, Sign, Environment, Loss, Exhaustion, Quiet.
- Source (Encounter result, d6 = 1): the book says "roll on an encounter table" but prints no fixed one. The Dungeon Seeds page tells the Warden to write encounter tables per dungeon ("Encounter Tables: create random events... wandering monsters, factions, NPCs"). The only printed d20 roll that gives monsters is the Bestiary's Monster Categories table (20 groups, each with a list of monsters). The Warden picks the monster from the group. The Reactions rule in the Core Rules decides whether an encounter is hostile.
- Dungeon time decision (user): dungeon turns are a counter with no clock time. The hour clock still covers rest and travel.
- Dungeon Events trigger decision (user): the GM calls one dungeon-cycle tool that states the triggers (new area, loud, hasty, repeat in the same room), and the engine rolls the d6.
- Encounter decision (user: "follow the book procedure"): a d6 result of 1 rolls on an encounter table for the location. The Warden writes these tables per dungeon (Dungeon Seeds), so the engine rolls against a Warden-authored table and does not invent one. A non-obvious NPC reaction is a 2d6 roll on the Reactions table (Hostile 2, Wary 3-5, Curious 6-8, Kind 9-11, Helpful 12), from the Core Rules. Wilderness uses the same encounter-table wording, so the table mechanism is shared with the wilderness work.
- Generic candidate for Phase 11: a band lookup on a 2d6 (or any dice) result, defined in data. D&D's analogue is any table-by-range lookup, e.g. reaction or random-event tables.
- Source (treasure and gold income): the Cairn 2e pages checked (Procedures, Core Rules, Marketplace, Warden's Guide > Dungeon Exploration, Dungeon Seeds, Reliquary, Bestiary) print no treasure table and no gold-income rule. Gold appears only as Marketplace prices. The Dungeon Seeds page says to give treasure beyond gold (knowledge, allies, trade goods, artifacts). The Reliquary's Coin of the Father is an illusion, not income. The book's treasure is therefore the Warden's call. Any gold-income tool is a house rule until the user picks one.
- Day structure (resolved, user choice): the Procedures page says travel is counted in **watches**, three eight-hour segments per day, named morning, afternoon, and night. The engine now uses those (`clock.json`: `parts` morning, afternoon, night; `hours_per_part` 8). The day shape stays ruleset data. Tests that hard-coded four parts were updated. Full suite: 834 passing. Live-verified on a fresh DB: a moments rest advanced the clock by 1 hour; a night rest from morning reached day 2 morning.
- Cycle decision (user): a dungeon cycle is a separate counter. It never moves the day clock. Only `rest` and `advance_time` move the clock.
- Treasure decision (user): treasure is a **world object**, separate from actors (characters). An object is placed in a location or a dungeon area and holds gold, an item, or anything the GM chooses. The engine does not invent treasure. A dungeon area is a child `Place` (`parent` = the dungeon location), so areas already fit the model.
- Gold decision (user): a PC who picks up gold from an object gets it. There is no shared party purse.
- Generic candidate for Phase 11: world objects (placed, found, taken, with payload in data) are generic. D&D has the same need for treasure in rooms.
- Phase 10 defaults (not user-chosen, to revisit if wrong): `place_object(place, name, contents, hidden)` for the Warden, where `name` is the short name a PC takes the object by; `take_object(object, character)` for a PC, matching the name, then the description, then part of either; a taken item goes to inventory and is refused when the inventory is full; `dungeon_cycle(triggers, reason)` for the cycle tool, which rolls the d6 Dungeon Event and never moves the clock.
- Status: dungeon cycle, Dungeon Events, reaction rolls, and world objects are built. Unit tests: `tests/test_dungeon_cycle.py`, `tests/test_world_objects.py`. Full suite: 865 passing. Live-verified on a fresh DB: a dungeon event rolled on a new area; the Warden placed an offering niche with gold and an item; Mara took it by its short name, and gold and the item arrived once, with `object.taken` recorded and the object gone from the location.
- Resolved (user choice): `moments` is one hour. A rest length can now be given in `hours`, so the rest format supports hours as well as parts and days.

## Phase 11: generic engine refactor

Goal: the engine knows no Cairn terms, numbers, or prompt rules. Each Cairn rule is data in `cairn2e/`, read through a parameter the engine defines. Each parameter must be one a second rule set would need too, so the test is "would D&D need this hook?" (D&D's analogues: short and long rests, spell slots, encumbrance, hit dice, and a conditions list.) Where a rule is truly Cairn-only, it stays in `rules/cairn2e.py`, the rule-set adapter, and not in the engine.

Move out of the engine (from the leak audit, with file locations as of commit `b330090`):

- Deprivation penalty item: `FATIGUE_NAME` in `services/inventory.py`. Becomes a ruleset `inventory.penalty_item` with name, size, and kind. The engine reads the name.
- Daily deprivation tick: `services/clock.py` adds Fatigue each day. Becomes a `clock.daily_effects` entry that names a condition and its trigger.
- Fatigue rest message in `services/inventory.py`: moves into the item's `clears_on` and message template in JSON.
- Spell cost paid in Fatigue: `services/magic.py`. Becomes a ruleset `magic.cast_cost` referencing the penalty item.
- Armor cap (`MAX_ARMOR` in `services/combat.py`) and the armor purchase rule in `services/market.py`: become ruleset `combat.armor.max` and item `effects` with a cap.
- Morale: `wil_stat` becomes `save_stat`. The "half casualties" trigger becomes a JSON trigger list next to `first_casualty`. Docstrings that name DEX or WIL move to generic wording.
- `set_deprivation` in `rules/context.py` becomes `set_condition(name, active)`.
- DONE (user chose generic): the group-break tool is `resolve_group_break`, in `services/combat.py`. Cairn's word "detachment" stays only in the `cairn2e/` prose.
- DONE (user chose generic): relics are charged items. The tools are `give_charged_item`, `use_charge`, and `recharge_charged_item`. The kind (`relic`) is set by `magic.json` `charged_kind`, and the recharge rule is the item's text.
- Debug panel stat options hard-coded in `web/templates/campaign.html`: rendered from the rule set's stats.
- GM prompts in `services/campaigns.py` (`STATE_INSTRUCTION` and related): the generic loop stays in code. Cairn bullets (armor glancing blows, relic recharge, detachment, defeated PC rules, combat speech policy, and the sentence cap) move to the ruleset as `prompt_fragments` and `narration` config.
- Weapon damage: free text from the GM's attack. DONE: weapons read their dice from the ruleset (see the Phase 11 status).
- Names list in `services/names.py`: low priority. Optional per-rule-set JSON.

Already generic (keep, and use as the pattern): stat keys from JSON (`morale.wil_stat`, `morale.retreat_stat` before the rename), clock parts, the debug gold route keyed on `marketplace.currency`, the marketplace structure, the `core` shared rule set, dice tables passed in by ID, scar tables passed in as `ScarTable`, modifier names as a mapping, and generic event types with ruleset-supplied names.

Done when: a grep for `Cairn`, `Fatigue`, `relic`, `detachment`, `Scar`, `deprivation`, and the stat names in `src/cygnusforge/services`, `src/cygnusforge/web`, and `src/cygnusforge/rules/context.py` returns no hits outside docstrings that name the rule set generically. The full suite and a live Cairn session must still pass with no change in behavior, except for the `(used)` wording and the speech-cap fix, which are gap-register items.

D&D comparison (what a second rule set would need, and where Cairn's shape would get in the way):

| Area | Cairn 2e | D&D 5e (analogue) | Engine status |
|---|---|---|---|
| Stats | STR, DEX, WIL from JSON | six ability scores, from JSON | Generic (stat keys in JSON). |
| Dice | `1dN`, `+` keeps the higher | `1dN`, `+` sums (damage), advantage is two d20s kept | Generic. Each rule set declares its `plus` operator (`highest` or `sum`) in `rule_set.json`. |
| Damage pipeline | armor, then Hit Protection, then STR, then Critical Damage save | hit points, with temporary HP | Cairn-shaped in `combat.json`. Needs a one-stage (hit points) pipeline as data. Open. |
| Rests | short and long from `rest.json`, `until`, `stop_at`, clock parts | short rest (1 hour) and long rest (8 hours), hit dice spent on the short rest | Generic (`RestLength`, clock). Hit dice are not modeled. Open. |
| Spell cost | Fatigue penalty item, one per cast | spell slots per level, restored on a long rest | Penalty item is read from JSON. Slots need a charged-pool type. Open. |
| Encumbrance | item slots from JSON, petty and bulky | weight or item slots | Generic (slots from JSON). |
| Conditions | deprived (need), doomed, scars, injuries | a conditions list (poisoned, prone, and so on), and death saves | Needs are generic. Scars and death are Cairn-shaped. Death saves are a gap. |
| Morale | group morale on casualties, WIL save | none by default | Generic, from `morale` JSON. |
| Group break | detachment, "defeat" or "flee" | none | Generic tool `resolve_group_break`. |
| Currency and shops | `marketplace.currency` and listings | gold and equipment tables | Generic. |
| Time | watches, dungeon turns | rounds, turns, and days | Clock parts are data. Dungeon turns are a counter in JSON. |

Decision rule for the refactor: a Cairn term may stay in `cairn2e/` JSON prose or the `rules/cairn2e.py` adapter. An engine hook needs a generic name and must read its numbers from the rule set. Where D&D would need a different shape (the damage pipeline, spell slots, death saves, hit dice), the open rows above are the gaps to record, not patch.

Status (Phase 11): the leak grep is clean. The remaining hits are the word "discard" and generic docstrings. The engine reads these from `cairn2e/`: penalty item (`inventory.json` `penalty_name`, `penalty_lasts`), the armor cap (`combat.json` `armor_max`), the large-group flag (`monsters.json` `large_group`), the save stat (`save_stat`), charged items (`charged_kind`), GM prompts (`prompts.json`, 7 fragments), narration (`narration.json` `sentence_cap`), needs (`need.json`), and injury tables (`injuries`). The debug stat options come from the rule set's stats. Live-verified on a fresh DB: opening turn, a player turn with the pack, the Inventory tab ("Carrying 7 of 10 slots"), and the Debug tab. The `need_set`, `group_break_resolved`, and `injury.gained` paths were not hit in that run. Renamed event kinds break replay of old dev DBs; the user accepted this.

Second live run (fresh DB, two-weapon check): the opening turn, a GM `give_item` of Twin Daggers with `1d6+1d6`, an initiative roll, a player attack that prompted `1d6+1d6`, the combat result (`combat.struck` raw 6, save failed, the thief died), the Inventory tab, and a clean DB log. The `need_set`, `group_break_resolved`, and `injury.gained` paths are covered by unit tests only. Phase 12 is the full play-through, so it should hit these paths live, and they are checked there.

Built since the last status: weapon damage and the dungeon reaction table merge.

- Weapon damage (user decision: the engine rolls): a carried weapon's dice come from its ruleset `damage` (`features.json` for starting gear, `marketplace.json` for catalogue items, or a custom item's damage given with `give_item`). An unarmed attack uses `combat.json` `unarmed`. The GM no longer passes a damage number, and the player's roll is a flavor prompt that does not set damage. The dice grammar requires a count, so the book's single die is stored as `1d6`, and `d6+d6` as `1d6+1d6` (keep the higher, since cairn2e's `+` keeps the higher). Unit tests: `tests/test_weapon_damage.py`. Live-verified on a fresh DB: a Sling attack on a Reed Bandit with a Club prompted `1d6` as flavor, and the logged `combat.struck` event used `weapon: sling` with raw damage 5, within 1d6. Attacking with a weapon that is not carried is refused by unit test, not live.
- Reaction merge: the duplicate `reaction` table is removed from `warden_tables.json`. `dungeon_reaction` in `dungeon.json` is the one table, and the `roll_tables` tool enum uses it.
- Dice operator and attachments (per rule set): the `plus` operator is declared in each rule set's `rule_set.json`. cairn2e uses `+` as keep-higher (`plus` is `highest`), so the operator is no longer one fixed rule. Story-only NPCs and rule attachments are generic engine features, not Cairn-only code.

Phase 11 is closed. The generic refactor items are done, the live runs above pass, and the D&D comparison records the open shape gaps (damage pipeline, hit dice, spell-slot pool, death saves) for later. The three unhit live paths move to the Phase 12 play-through. Phase 12 is next.

## Phase 12: CAS-1 "Trouble in Twin Lakes" data import (in progress)

Import the first-party adventure's data into the `twin-lakes` world. The full play-through, as the capstone test, is Phase 14, after the Phase 13 refactor. Source: https://cairnrpg.com/adventures/first-party/cas-1

Known content from the module text (read in full during the design pass; statblocks and tables transcribed during the phase):

- Two mysteries: Aldra the butcher's disappearance and Jandy the tithingman's death loop. Hooks offer 750 gp, a 250 gp bonus, and 200 gp each.
- Locations: Isthmus Town (the Stuffed Inn with its Reeve's Office and cellar, the Butcher Shop, Giva's Home), the Farming Path, Dunston, Fasha's Farm (farmhouse and abandoned home), Old Mill Road, the Stone Forest, Deadmill, East Lake Pass, the Downs, White Tower Fens (Whistlewood, pit trap, log trap, the White Tower, Bone Sump), the Temple of the Fungal Lizards (antechamber, columbarium, breeding chamber, and more), and The Roots.
- NPCs with statblocks: Reeve Trellis, Fasha (STR 14, DEX 13, WIL 5, fists d8, hook knife d6), Aldra, Flavia, Jandy, Giva, Drum, Annafranax (the Frog Witch), and Vuk (a monster). Monsters include Night Cat and Watersnatch.
- Relics: the Eye of Vuk, the Echo Spark, and the Wisp Lamp. These use the Phase 5 relic rules.
- A 1d8 daily victim schedule and quicksand.
- Dungeon Exploration procedures (book p.71), not Wilderness, plus random encounter tables. These need Phase 10.

Phase 12 design decisions (user):

- Module content lives in the world folder. The rule set keeps the rules and the bestiary. The Night Cat already in the bestiary is used as is, so the module does not repeat it.
- The world folder accepts `monster`, `table`, `feature` (relics), and `rule` entries, scoped to that world. The GM can search world monsters, roll world tables, and give world features.
- Named NPCs with statblocks: an `npc` entry takes an optional `monster` id (a monster in the same world or the rule set), or inline `stats`, `armor`, and `attacks` in monster format. NPCs with no statblock omit both. Full PC-style NPC stats (features and templates on an NPC) are scheduled as a gap, since they need persisted NPC characters.
- Encounter tables and the victim schedule are world tables. Dungeon encounters use the Phase 10 rule: the Warden's table per dungeon, rolled by the engine.
- Relics are world features and use the Phase 5 relic rules.

World: a module is its own world. The CAS-1 import goes into the `twin-lakes` world template (`src/cygnusforge/data/worlds/twin-lakes/`). That world is the Cairn 2e test world until the feature set is complete. The `kilharbor` world was removed. Its only content was a description, and no locations or NPCs. The `twin-lakes` world has no locations or NPCs until the import fills it in.

Rule attachments (built, live check pending): a module rule that belongs to one NPC, item, or creature (for example Vuk's Choking Hold, Deadmill's rules, or the Bandits' morale) attaches to that thing. Its text goes into the GM context whenever the thing is present, not only through `lookup_rule`. This is a generic engine feature, so core and D&D need it too. cairn2e supplies the data. Gap register: see the Phase 12 rule attachment entry.

Done when: every location, NPC, monster, relic, and table in the module is in the ruleset or in an adventure data file, and each is live-checked where it matters. Play-through is Phase 14. Gaps found are added to the gap register and closed or scheduled.

## Phase 13: generic refactor, rule set boundary

Goal: the engine and the shared data shapes work for D&D as well as Cairn 2e. Cairn-specific behavior moves into `cairn2e/` data or the `rules/cairn2e.py` adapter. Each hook takes a generic name, and its numbers come from the rule set.

Test for each item: "would D&D need this hook, and what shape would it take?" Where the D&D shape differs, the item is generalized (a data list or a pluggable pipeline), not left as a Cairn special case. Phase 11 recorded the open shape gaps. Phase 13 closes or schedules each of them.

Known items, from Phase 11 and the gap register:

- Damage pipeline: `combat.json` hard-codes armor, then Hit Protection, then STR, then a Critical Damage save. D&D uses hit points with temporary HP. Make the pipeline data: an ordered list of stages, each naming its kind.
- Hit dice: not modeled. D&D's short rest spends them. Decide whether this is a rest stage hook or a charged pool.
- Spell cost: the Fatigue penalty item is data, but slots need a charged-pool type (spell slots per level, restored on a long rest).
- Death: death saves (D&D) and the Cairn death rules differ. The Cairn death rules stay in `cairn2e/`, behind a death hook the rule set names.
- Conditions: needs are generic. Scars and injuries are Cairn-shaped. Decide whether a generic conditions list covers both.
- `update_npc` attack modifiers (`armor`, `attacks`): cairn2e-specific in shape. Decide whether they belong in shared core or the cairn2e rule set (open in Phase 12).
- Prompts: GM prompt text that names Cairn terms (`prompts.json`, `narration.json`) moves fully to the rule set. The engine only reads it.
- Statless NPC HP: the book's guidance (average 3, hardier 6, serious 10+) is cairn2e data, not an engine rule (open in Phase 12).
- Rule attachments and story-only NPCs stay generic. Confirm they have no Cairn terms in the engine.
- Verification: a grep for Cairn terms in `src/cygnusforge/` outside the adapter comes back clean, and the engine tests run with a minimal second rule set (a D&D-shaped fixture) to show the hooks work without Cairn data.

Done when: the grep is clean, every item is closed or scheduled with a reason, the full suite passes, and a live test of Cairn 2e combat and character creation still works.

## Phase 14: capstone, CAS-1 "Trouble in Twin Lakes" play-through

Play the adventure from start to finish in a live session, on the refactored engine, as the end-to-end test of Cairn 2e: https://cairnrpg.com/adventures/first-party/cas-1

Done when: the module plays from the first hook to its endings in a live session, the transcript reads correctly, every gap found is in the gap register and closed or scheduled, and the Phase 12 import is complete against the transcription (every location, NPC, monster, relic, and table).

## Gap register

Every known gap, from the checkpoint history and the live tests, with the phase that closes it. "Unscheduled" means it needs a decision or a phase, and that decision is pending.

Engine and prompt (cross-cutting):

- Combat speech sometimes exceeds the "one sentence, two at most" cap. The cap is now `narration.json` `sentence_cap`, but it is still enforced by the prompt only. Needs a validator or stronger prompt, then a live re-test. Still open after Phase 11.
- Speech outside combat is not capped. An NPC gave a long non-combat speech. Still open after Phase 11; not live-verified.
- Phase 11 renames break replay of old dev DBs (event kinds and payload fields). Accepted by the user. Use fresh DBs for testing.
- The death cause reads "with hp at 0", using the stat id. Stat display names are not in the combat config, so the text is not player-facing yet. Needs the names passed in from the rule set.
- `tool_calls.json` descriptions still say "detachment". Ruleset prose to update.
- The magic result key changed to `penalty`. Not live-checked against the prompts.
- RESOLVED (Phase 11, user chose engine-rolled dice): weapon damage comes from the ruleset, and the GM no longer passes a damage number. Gaps left from this:
  - RESOLVED (user chose the book rule): two weapons roll both dice and keep the higher. Each rule set declares its own `plus` operator. Under cairn2e (`plus: "highest"`), `+` keeps the higher, so Twin Daggers is `1d6+1d6`, and monster fists `1d8+1d8` keep the higher too. The `|` operator is removed. A multi-term formula in a rule set with no `plus` fails at roll time. Unit tests: `tests/test_dice.py`, `tests/test_weapon_damage.py`. Live-verified on a fresh DB (before the `|` removal; the unit tests cover the new grammar): the GM gave Twin Daggers with `1d6+1d6`, and a player attack on a Hooded Thief logged `combat.struck` with weapon "Twin Daggers" and raw 6, within 1d6. The Inventory tab lists the daggers.
  - The player's visible roll is a flavor prompt, so it can differ from the damage logged. The user chose this. Unscheduled, needs a UI decision.
- The Inventory tab shows no weapon damage formula, so the player cannot see what a weapon rolls. Unscheduled, a small UI addition.
- After a fight ends on its own (the last foe dies), the GM can still call `end_encounter`, which fails with "there is no encounter in progress". The fix is a prompt rule or a tool that accepts a no-op. Unscheduled, found live.
  - The unarmed `1d4` and its names (`unarmed`, `fists`, `bare hands`) in `combat.json` are assumptions, not checked against the book. Unscheduled, needs a book check.
  - An item that is not carried is refused for an attack. The GM must `give_item` it first, with its damage if it is not in the catalogue.
- RESOLVED (Phase 11 follow-up): the duplicate `reaction` table is removed from `warden_tables.json`, and `dungeon_reaction` is the one table.
- The content loader does not reject duplicate table ids. `ContentLibrary.load` builds `dice_tables` as a dict, so a duplicate silently overwrites the earlier table. Unscheduled; a loader check would catch this class of bug.
- Repeated speech lines (four "Fenwick" lines in one scene). Cause unverified; narration quoting NPC words is the leading hypothesis. Unscheduled, needs investigation.
- The GM accepts player-invented past events as fact. Unscheduled, needs a prompt rule and a test.
- RESOLVED (Phase 11): debug panel stat options come from the rule set's stats.
- RESOLVED (Phase 11): Cairn GM prompt wording moved to `prompts.json` and `narration.json`.
- RESOLVED (Phase 11): weapon damage is rolled from the ruleset's weapon dice. See the Phase 11 status for the gaps left.
- OPEN (dice): multi-term formulas in a `plus: "sum"` rule set fail at roll time if `plus` is missing. Should be validated at load. Unscheduled.
- RESOLVED (NPC HP, from the book's Hit Protection section): an NPC's HP is average 3, hardier 6, or a serious threat 10+. The monster loader requires the HP stat, from the first `harm` damage step, to be at least 1. Fasha's 6 is an assumption, since the module prints no HP (transcription L540). Flagged for the user to confirm.
- RESOLVED (statblock precedence, user chose): a name that matches a published statblock takes that statblock's stats, armor, and attacks. Both `create_npc` and `create_monster` refuse explicit stats under that name, and `create_monster` also refuses armor or attacks, unless `force` is set. With `force`, the GM creates a custom creature under that name. Unit tests: `tests/test_combat.py`. Live re-test on a fresh DB is pending in Phase 12.
- RESOLVED (Phase 12, Vuk decision): Vuk has no numeric stats in the module (transcription L86, L541). Her ash form is story-only, and her full form uses the bestiary Lich. See the Phase 12 entries below.
- Test flakiness: a PC's max HP is random at creation and `_set_stat` clamps to it, so a max HP of 1 makes tests fail. Empty `Rolls` queues raise `IndexError`. Unscheduled, test infrastructure.
- `tests/test_world.py` snapshot-style prompt assertions need checking after prompt edits. Unscheduled.

Phase 1 (combat core):

- Live test of an impaired or enhanced attack was not done.
- Scars entry 1 `note` duplicates its text. The max-HP note in transcript wording needs fixing.
- The Scars table must be transcribed exactly into `tables.json`. Verify it is complete.

Phase 4 (deprivation, fatigue, rest, inventory):

- Rest end conditions (`conditions.json`) with rest triggers are not built.
- A rest plus `advance_time` may double-advance the clock. Not live-verified.
- An interrupted rest can mismatch the narration and the clock.
- RESOLVED (user choice, "Stop at dusk"): a `night` rest begun in the morning ends at the start of the night watch, not the next morning. `RestLength.stop_at` (ruleset: `rest.json` `night.stop_at: {"morning": "night"}`) sets this. A night begun in the afternoon or night still runs until morning. The rest notice now appears whenever a rest passes time, not only at a day change. Unit-tested and live-verified on a fresh DB: a night rest from the morning start ended at day 1 night (`clock.advanced`, 16 hours elapsed).
- The GM narrated a night's camp and eating without calling `rest`, so no ration was used. Needs a prompt rule and a test.
- FIXED (Phase 10): `take_object` matched only the full description, so the GM's short names failed. The GM then used `give_item` and a gold change, which duplicated the loot and never removed the object. Objects now have a short `name`, the matching is name, then description, then part of either, and the prompt says never to use `give_item` for a place's object. Live-verified.
- OPEN (Phase 10): hidden objects have no reveal path. A hidden object cannot be listed or taken, and no tool reveals it. Unscheduled; needs a decision on who reveals it (the Warden, or a search).
- OPEN (Phase 10): `drop_item` removes the item and does not place it in the location, so a dropped item vanishes. Seen live when Mara dropped her Lure. Unscheduled; needs a decision on whether a drop creates a place object.
- RESOLVED (Phase 11 follow-up): the dungeon reaction duplicate is merged (see the gap register entry above on duplicate table ids).
- The 10-slot capacity and the full-inventory HP rule are unverified against the book.
- The deprivation Fatigue threshold (day 2 or day 3) is unverified.
- The `visited_day or 1` stale-place rule and STR 0 outside combat are unverified.

Phase 5 (magic):

- The premature-recharge refusal and the spent-relic narration with the new prompt rule are not live-verified.
- The Phase 5 status in this spec was not updated after the work.

Phase 6 (gold and marketplace):

- Transport capacity bonuses (cart +4, wagon +8, horse +4, mule +6), stables, and mounts are deferred.
- GM catalogue visibility is unresolved: prices in world-state text, or guidance in tool descriptions.
- Medical Healing (50 gp) heals everything because the book gives no mechanical text. Antitoxin and Sedative have none either. Needs a user decision.
- Room & Board, Private Room, and Stable have prices but no mechanical text.
- Whether lodging covers only the next night's meal is unverified. The payer rule (first named PC pays) is an assumption.
- The live test on a fresh DB and the Phase 6 final-decision update were not completed.
- Hireling wages are stored but not charged. Phase 8. Resolved: wages are charged daily from the employer's gold, and unpaid wages are recorded.

Phase 7 (character creation and starting gear):

- The backpack's six-slot capacity is not modeled; it is treated as one normal slot. Verify against the book.
- Lantern oil does not burn down over time.
- The armor cap of 3 at creation is a design choice. Confirm.
- Petty and bulky flags are unverified for lockpicks, pincers, roll of tin, twin daggers, plain daggers, badge, and black outfit.
- `candle-helmet` has both `uses` 6 and `armor` 1, probably a data error.
- The `(used)` wording in the `ItemUsesSet` transcript line reads awkwardly: "has 2 uses left (used)".
- The inventory panel shows "1 uses left" (no plural handling) and lowercase size labels ("normal, 1 slot"). Cosmetic.
- The inventory panel's empty state ("Carrying nothing") was not live-tested.
- The GM spent oil on an unrelated needle-knife attempt, and blackened a blade over a lit lantern. The `use_item` description was tightened twice. Re-verify live.
- Last-use removal of an item was covered by unit tests only, not live.

Phase 8 (hirelings): scope in phase 8. Includes 3d6 attributes, 1d6 HP, wages, and joining the party.

- Phase 8 starting gear: resolved to station equipment chosen with `give_item`. Not yet verified live; the GM must follow the instruction.
- Hirelings get no armor or attacks: `HirelingAdded` does not carry armor or attacks, unlike `NpcAdded`. Unscheduled, needs a phase.
- Hirelings carrying gear: the book allows hirelings to carry equipment, but carried items do not yet count toward the party's inventory. Unscheduled, needs a decision on the mechanic.
- Hirings are not offered through the marketplace flow: the GM passes the wage by hand from the listing. Unscheduled, needs a phase.
- Unpaid hireling wages leave the GM to decide; there is no automatic departure rule. Unscheduled, needs a decision.
- RESOLVED (live-verified on a fresh DB): no tool gave, took, or set gold, so the GM's narrated gift left the engine balance unchanged. The core `change_gold` tool now records it (`character.gold_changed`, refusing a take the character cannot pay). The unpaid-wage path also fired live (`hireling.unpaid`, no deduction, GM narrated it).
- RESOLVED (live-verified on a fresh DB): hireling upkeep. The `give_item` description tells the GM to give hirelings Rations. A catalogue item given now keeps its listed uses (3), so Tam's Rations were not removed after one meal. At the camp rest, Boreas and Tam each ate one ration (3 to 2) and Tam kept his pack.
- RESOLVED (player decision, live-verified): the GM may voice hireling dialogue through `speak`, and the player chooses hireling actions. `speaker_profile` accepts hirelings at the player's location. Tam's line was spoken through `speak` in the live session.
- OPEN: narrated sums can diverge from the engine. In the live test the GM said "five, all told" for a 16-gold gift. The prompt now says sums and counts must match the tool results, but this is prompt-only. Needs a live recheck, and may need a tool-result line in the narration context.
- OPEN: the GM narrates state changes it never records through a tool (for example, gifts of items or injuries). Unscheduled.

Phase 9 (death rules): critical damage, Scars 11 (Mortal Wound) and 12 (Doomed), and new characters.

- Bandaging: live-stabilized once, but via a GM workaround. The clean `use_item` path with a singular name is unit-tested and verified live at the next dying PC. Also OPEN: the GM gave a second Bandage to the patient and used it, so there was a duplicate in the party. The GM's tool choice for aid is unreliable. Strengthen the `use_item` description or add an aid-specific tool. Phase 11 decides how.
- `stabilize` is live-verified once, as a GM tool call.
- RESOLVED (live-verified): hireling promotion. Tam was promoted after Dorn's death. Still OPEN: wages stopping after promotion is not yet seen live on a new day.
- RESOLVED: a dying-to-dead narration mismatch. The `combat.harmed` status now reads "dead" for a PC killed outright. The GM prompt was not changed, so the narration check is still live-unverified.
- OPEN (seen live): the GM gave Ysolde a "Walking stick" mid-turn with 0 uses, so it can be swung but never spent. The same freeform-item gap as below.
- OPEN: a hireling kit item the GM names freeform, such as "Bandage", gets no uses. The catalogue match is now plural-tolerant, but a freeform name still has no uses. Gaps like this should be caught by a `give_item` validator. Phase 11 decides.
- Superseded in Phase 10: 6-hour parts became 8-hour watches (user choice).
- Resolved: STR 0 outside combat as instant death, confirmed by the user.
- A new PC is placed by the GM, not by the book's rule for when a new PC joins. The narrative decision is the GM's.
- A dead PC's gear is not lootable by tools. The GM handles it in narration only.
- Whether a dead PC's gear counts toward the party inventory is undecided.
- `_save` and `player_save` use the word `critical` for different things. `player_save` always reports `critical: false`. Rename to avoid confusion. Unscheduled.
- Death happened at the end of a three-hour advance, not at the one-hour mark. The GM chose the advance. Consider whether the clock should stop the advance at the death hour. Unscheduled.
- The flaky-test cause above is unconfirmed.

Phase 10 (dungeon exploration): the dungeon turn cycle, Dungeon Events, treasure, and gold income.

Phase 11 (generic refactor): the list above.

Phase 12 (CAS-1 data import): the import and any gaps it reveals.

Phase 13 (generic refactor): the Phase 13 items above.

Phase 14 (capstone play-through): the full CAS-1 play-through and any gaps it reveals.

- RESOLVED (Phase 12, seeding): `seed_campaign` ignored an NPC's monster statblock, so module NPCs with a statblock got random stats. Seeding now uses the statblock. Unit tests: `tests/test_combat.py`. Live test verified in the DB.
- RESOLVED (Phase 12, Vuk decision): the Ash Phantom has no statblock on cairnrpg.com's Warden's Guide bestiary, and the module prints no numbers. Decision: Vuk's ash form is story-only with the Choking Hold rule attached (rule attachment is still OPEN below). Her full form, the 7 ft Lich Queen, uses the bestiary Lich (14 HP, 1 Armor, 8 STR, 8 DEX, 18 WIL, soul dagger d8). The Lich is not imported yet (see the bestiary gap).
- RESOLVED (Phase 12, bestiary): the cairn2e `monsters.json` now has 85 entries, up from 12. Each new entry was checked verbatim against the source HTML. Known gaps from the import:
  - OPEN: Roc has no stat block on the source page.
  - OPEN: Traitor-Rats has no match in the source. Its entry is unchanged and unverified.
  - OPEN: "Warrior Snail Acolyte" is not a separate block. Only "Acolyte" exists.
  - OPEN: Night Cat is in the core bestiary and the twin-lakes monsters. Its stats match, but its descriptions differ.
  - OPEN: Boggart, Pixie, and Will-o-Wisp have no attacks in the source.
- OPEN (Phase 12): the Reeve Trellis, Aldra, Flavia, Jandy, Giva, and Drum have no statblock in the module. The `story_only` flag and the `update_npc` stats path are built. Decision needed: should the Warden give these six stats when they first fight?
- RESOLVED (Phase 12, rule attachments, user chose always-injected): NPCs and features list rule ids in `rules`. Their text goes into a "Rules for what is present" system part on every turn. "Present" means a living NPC at the PC's location, or an item the PC carries. Ids are validated at load. Unit tests: `tests/test_rule_attachments.py`. Live-verified on a fresh Twin Lakes campaign: at the start (PC in Isthmus Town) the part is absent. After the GM moved the PC to Deadmill with `move_actor`, the next reply's context included "Rules for what is present" with Jandy's `deadmill-time-loop` text.
  - OPEN (decision, pending): the field name `rules` and the "present" definition are my defaults. Confirm with the user. Decided by the user: defeated NPCs keep their rules while they remain at the location (implemented, tested in `test_a_defeated_npc_still_here_keeps_its_rules`).
  - RESOLVED: monsters carry rules. `MonsterTemplate.rules` is loaded and checked against the rule set (ruleset monsters) or the merged rules (world monsters). A monster made from a statblock gets its rules, and seeded NPCs that name a monster merge the statblock's rules. Twin Lakes attaches Night Cat's two rules, Suraru Imperator's critical damage rule, and `morale-bandits` to Bandits and Bandit Leader. Tests: `tests/test_rule_attachments.py`. Live-verified on a fresh DB: the GM created a Night Cat at Hollowmere Village, and the next turn's context listed "Rules for what is present" with `night-cat-attack` and `night-cat-critical-damage` (from Night Cat). After the cat left, the next turn's context showed "NPCs here: none" and no rules section.
  - OPEN: items from the catalogue or `give_item` do not carry rules. Only starting gear and direct events do. No twin-lakes feature has rules yet.
  - OPEN (browser, character creation): "Randomize all" overwrites a name the player has already typed. Found in the live run. Decide whether it should keep typed fields.
  - OPEN (uncertain): Jandy's `deadmill-time-loop` attachment. The module says the loop breaks "by saving Jandy", so the attachment may be wrong.
  - OPEN: several module rules have no rule id, so they are not attached. These are Eye of Vuk, Fletcher's Ire, Echo Spark, Watersnatch, Curved Dagger, Memory Prism, Wisp Lamp, Annafranax's bird revenge (§2.6), and Fasha's drag-to-temple (§2.7).
- OPEN (Phase 12): the world loader reads only the text of world rules, so `"important": true` on a world rule has no effect.
- OPEN (Phase 12): world-scope `dice_table` entries are not loaded, so the d6 and d8 rolls in the module's tables are plain options, not rolls.
- OPEN (Phase 12, module text, user to decide; kept verbatim): "blast" (Suraru Imperator's bite) is undefined. "The Cities" and "Marchguard" are undefined. "memory prison" vs "memory prism" is treated as the prism item; the book prints both. "3 bandits*" has an asterisk with no footnote. "see p.3" points to a missing page. Vuk's heart count is "at least one" in the viscera line and three in Vuk's text.
- OPEN (Phase 12): the unarmed 1d4 is taken from the module for unarmed attacks. It is not verified against the printed page.
- OPEN (Phase 12): the module prints no HP or armor for Fasha. HP 6 is an assumption, from the "hardier" guidance. Confirm with the user.
- OPEN (Phase 12): Reeve's Office and the Cellar are nested under the Stuffed Inn in the source, but the one-level location schema puts them under isthmus-town. Decide whether the location schema needs nesting depth 2.
- OPEN (Phase 12): the Dungeon Events table is not reproduced in the world data.
- OPEN (Phase 12): the book gives average 3, hardier 6, and serious threat 10 or more for statless NPCs. The engine rolls 1d6 for statless NPCs. Add this guidance to the cairn2e data, not the engine.

- OPEN (Phase 12, engine): attack modifiers on `update_npc` (`armor`, `attacks`) are cairn2e-specific in shape. Decide whether they belong in shared core or in cairn2e only.
- OPEN (Phase 12, tests): the `Ghoul` name collided with the new published Ghoul statblock. The test creature is now "Grave Thing" in five test files. `tests/test_content.py` still uses "Ghoul" in a loader test. This is a naming check, not a bug.

Unscheduled, needs a book reference or a decision:

- The bonds and omens section, the bibliography, and the game files and tools were not reviewed in detail. Revisit each before its phase.
