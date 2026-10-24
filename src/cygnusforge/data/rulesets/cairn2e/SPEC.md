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
   - Blast: roll the damage die separately for each target. The book's rule (Core Rules, https://cairnrpg.com/second-edition/players-guide/core-rules): "Attacks with the Blast quality affect all targets in the noted area, rolling separately for each affected character... If unsure how many targets can be affected, roll the related damage die for a result." Flags and targets are in Phase 2.
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
- Book rule (core rules page, under the Detachments heading, players-guide/core-rules): "Attacks against detachments by individuals are impaired (excluding blast damage). Attacks against individuals by detachments are enhanced and deal blast damage." So a detachment's attacks on individuals are enhanced and Blast, and attacks on a detachment by individuals are impaired and exclude Blast damage.
- Closed: a Blast attack by an individual on a detachment is not impaired and rolls normal dice, because the book excludes blast damage from the impairment. The engine no longer refuses it. Decision by the user: follow the cairn2e rules as found on the website.
- Attack flags (design decision): a bestiary attack may be structured, for example `"attacks": {"claws": {"dice": "1d8+1d8", "flags": ["blast"]}}`. Plain dice strings such as `"bite": "1d6"` stay valid for unflagged attacks. Flags are `blast` and `ignores_armor`.
- A non-group attack with `blast` takes a `targets` list, and each target rolls separately. With no list the damage rolls once. Detachment (large group) Blast keeps its existing behaviour.
- OPEN: the GM tool argument shape. The attack tool takes an optional `targets` list for non-group Blast, and each target rolls separately. Whether this shape is right for the GM tool API is still to be confirmed.
- `resolve_attacks` refuses blast attacks. `ignores_armor` skips armor. Shadow (draining touch) and Unicorn (horn) use `ignores_armor`.
- Closed: non-detachment Blast is modelled by the `blast` flag and the `targets` list. `ignores_armor` is its own flag. Tests: `tests/test_blast.py`.
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

Start location (implemented; live test pending): the `twin-lakes` world template has an optional `start` object in `world.json`, with `location` set to `isthmus-town` and `hooks` set to the module's Hooks text verbatim (the Tideborn 750 gp and Cities 200 gp bullets, from https://cairnrpg.com/adventures/first-party/cas-1). The loader validates the location id. The opening prompt places the player character there with `move_actor` and gives the GM the hooks. Before this, the GM invented an opening village ("Thornhallow") that is not in the module, so the player could not reach the module locations.

Rule attachments (built; automated test `tests/test_rule_attachments.py` passes; live check blocked): a module rule that belongs to one NPC, item, or creature (for example Vuk's Choking Hold, Deadmill's rules, or the Bandits' morale) attaches to that thing. Its text goes into the GM context whenever the thing is present, not only through `lookup_rule`. This is a generic engine feature, so core and D&D need it too. cairn2e supplies the data. Gap register: see the Phase 12 rule attachment entry.

### Bestiary audit

All 84 source bestiary entries were checked against the source HTML. `monsters.json` holds 85 entries. The difference from 84 is not yet reconciled (see Traitor-Rats below).

- Descriptions are verbatim from the source bullets. Stats, armor, and attack dice match the source.
- Rules are verbatim, as inline rules `{id, text}` on each monster: 137 rules across 77 monsters. Monster rules are not listed in the rule set prompt. They are injected for the GM only when the monster is present, through the 'Rules for what is present' context part, and they are available to `lookup_rule`.
- Shadow (draining touch) and Unicorn (horn) ignore armor. They are modelled with the `ignores_armor` flag.
- OPEN: Traitor-Rats. Stats come from the book's Detachments example (3 HP, STR 5, DEX 15, WIL 4, bite d6, detachment). Armor 0 and the description are NOT verified and remain as they were. Verify against the book. It is the 85th entry, the worked example from the cairn2e Detachments page, and is not among the 84 source bestiary entries.
- OPEN (judgement, confirm with the user): Ghost's source entry has an empty description. `monsters.json` sets the description to the first bullet verbatim, which is also rule `ghost-rule-1`.
- RESOLVED (user): bandits and gnolls start as individuals. The GM runs them as a detachment when an encounter calls for it, by passing `detachment: true` to `create_monster`. The create result reports the flag. Tests: `tests/test_detachment.py` (bandit cases). Live check (campaign 1 "Bandit Road", fresh server on 8773): PASSED. Five bandits blocking the Farming Path came out as a detachment: the GM called `create_monster` with `template: "bandits"` and `detachment: true`, and the result reported `"detachment": true`.
- OPEN: Roc. The source has no statblock.
- OPEN: Warrior Snail Acolyte. The source has no separate block, only "Acolyte".
- OPEN: Night Cat. Its description differs between sources.
- OPEN: Boggart, Pixie, and Will-o-Wisp. The source gives them no attacks.

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
- Statless NPC HP: the book's guidance (average 3, hardier 6, serious 10+) is cairn2e data, not an engine rule. Closed in Phase 12 (`npc_hp` and `hp_tier`).
- Rule attachments and story-only NPCs stay generic. Confirm they have no Cairn terms in the engine.
- Verification: a grep for Cairn terms in `src/cygnusforge/` outside the adapter comes back clean, and the engine tests run with a minimal second rule set (a D&D-shaped fixture) to show the hooks work without Cairn data.

Leak audit (Phase 13 start, read-only search of `src/cygnusforge/` for Cairn terms). Engine code has no hard-coded Cairn stat names, dice, or numbers. These Cairn-specific items remain, each to be moved behind a generic hook or data key:

- `services/combat.py` (injury trigger, about L208–210): the injury fires on "exactly 0 on the injury stat". That is Cairn's STR 0 rule. Make it a data trigger in the rule set.
- `services/combat.py` (about L300 and L625): `death.on_critical_save_failed` names a Cairn save. Rename it to a hook keyed by the rule set.
- `services/combat.py` (about L122 and L577): `resolve_group_break` and its Warden-specific wording and flow. Decide whether morale is generic (it is in D&D terms too) and move the wording to data.
- `services/needs.py` (about L94 and L182–183) and `services/world.py` (about L249): the "deprived" condition is hard-coded. `rules/context.py` (about L127–131) and the adapter `rules/cairn2e.py` (about L109–110) pass `deprived`. Make the condition name come from the rule set.
- `services/world.py` (about L1799): the literal "Deprived:" label in the character summary. Move it to data.
- `services/inventory.py` (about L103): the "damage the Warden names" wording for uncatalogued items. Check whether it is generic.
- `services/morale.py`: mostly generic. Check that the first-casualty default comes from data.
- Not yet read: `services/death.py` (the body of `on_critical_save_failed`), `services/world.py` (about L286 and L791), and `services/dice_tables.py`. Read these before scheduling them.

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
  - Closed in this phase (bestiary audit): descriptions are verbatim, and rules are verbatim inline rules attached to each monster. See the Bestiary audit below.
  - OPEN: Roc has no stat block on the source page.
  - OPEN: Traitor-Rats is the 85th entry, the worked example from the Detachments page (3 HP, STR 5, DEX 15, WIL 4, bite d6, detachment). Armor 0 and the description are unverified. Verify against the book.
  - OPEN: "Warrior Snail Acolyte" is not a separate block. Only "Acolyte" exists.
  - OPEN: Night Cat is in the core bestiary and the twin-lakes monsters. Its stats match, but its descriptions differ.
  - OPEN: Boggart, Pixie, and Will-o-Wisp have no attacks in the source.
  - RESOLVED (user): bandits and gnolls start as individuals. The GM runs them as a detachment when an encounter calls for it, by passing `detachment: true` to `create_monster`. The create result reports the flag. Tests: `tests/test_detachment.py` (bandit cases). Live check (campaign 1 "Bandit Road", fresh server on 8773): PASSED. Five bandits blocking the Farming Path came out as a detachment: the GM called `create_monster` with `template: "bandits"` and `detachment: true`, and the result reported `"detachment": true`.
- RESOLVED (Phase 12, user chose the book's guidance): the eleven statless NPCs (Reeve Trellis, Aldra, Flavia, Jandy, Giva, Drum, Corsetta, Franz, Beju, Nicolas, and Berk) have no statblock in the module. Each has `hp_tier: "average"` in `twin-lakes/npcs.json`. A live seed check found five of them (Corsetta, Franz, Beju, Nicolas, Berk) untiered and rolling 1 to 6 HP; they were fixed and the test now covers all eleven, which the rule set maps to HP 3 (`npc_hp` in `cairn2e/rule_set.json`). Seeding passes that HP as given stats, so the other stats are still rolled. Tests: `tests/test_npc_hp.py`. Their description is commoner or worker, so the average tier is my judgment. Hardier and serious tiers are in the rule set for NPCs that need them. Aldra is murdered in the module, so her stats matter only if she is referenced.
- RESOLVED (Phase 12, rule attachments, user chose always-injected): NPCs and features list rule ids in `rules`. Their text goes into a "Rules for what is present" system part on every turn. "Present" means a living NPC at the PC's location, or an item the PC carries. Ids are validated at load. Unit tests: `tests/test_rule_attachments.py`. Live-verified on a fresh Twin Lakes campaign: at the start (PC in Isthmus Town) the part is absent. After the GM moved the PC to Deadmill with `move_actor`, the next reply's context included "Rules for what is present" with Jandy's `deadmill-time-loop` text.
  - OPEN (decision, pending): the field name `rules` and the "present" definition are my defaults. Confirm with the user. Decided by the user: defeated NPCs keep their rules while they remain at the location (implemented, tested in `test_a_defeated_npc_still_here_keeps_its_rules`).
  - RESOLVED: monsters carry rules. `MonsterTemplate.rules` is loaded and checked against the rule set (ruleset monsters) or the merged rules (world monsters). A monster made from a statblock gets its rules, and seeded NPCs that name a monster merge the statblock's rules. Twin Lakes attaches Night Cat's two rules, Suraru Imperator's critical damage rule, and `morale-bandits` to Bandits and Bandit Leader (both since removed; see the bandit entries below). Tests: `tests/test_rule_attachments.py`. Live-verified on a fresh DB: the GM created a Night Cat at Hollowmere Village, and the next turn's context listed "Rules for what is present" with `night-cat-attack` and `night-cat-critical-damage` (from Night Cat). After the cat left, the next turn's context showed "NPCs here: none" and no rules section.
  - OPEN: items from the catalogue or `give_item` do not carry rules. Only starting gear and direct events do. No twin-lakes feature has rules yet.
  - OPEN (browser, character creation): "Randomize all" overwrites a name the player has already typed. Found in the live run. Decide whether it should keep typed fields.
  - DONE: Jandy's `deadmill-time-loop` is attached to her NPC and to the Deadmill place. Her presence and her location both bring the loop rule to the GM. The rule text is the module's; it names Jandy as the one who breaks the loop.
  - OPEN: several module rules have no rule id, so they are not attached. These are Eye of Vuk, Fletcher's Ire, Echo Spark, Watersnatch, Curved Dagger, Memory Prism, Wisp Lamp, Annafranax's bird revenge (§2.6), and Fasha's drag-to-temple (§2.7).
- OPEN (Phase 12): the world loader reads only the text of world rules, so `"important": true` on a world rule has no effect.
- OPEN (Phase 12): world-scope `dice_table` entries are not loaded, so the d6 and d8 rolls in the module's tables are plain options, not rolls.
- OPEN (Phase 12, live gap, blocks the Vuk live check and the capstone; the route fix below is pending a live test): the GM will not route the player to T10b (Vuk's Interrogation Room). In a live session, Mara in Marrow Hollow asked to go north and into T10b. The GM replied "No change needed" and said the temple was unreachable from the crossing, with no move. In an earlier turn it had invented "South Bank Landing" with `create_location` instead of using the module's route. The module's path from the crossing to the Temple of the Fungal Lizards and T10a/T10b is not in the GM's context. Fix the location data, or the GM's guidance on routing, so the module path is reachable. Do not let the GM invent places the module already has.
  - Status (this change): route prose is added to `locations.json` on bone-sump, temple-of-the-fungal-lizards, t9-chamber-of-worship, and t10b-interrogation-room-interior. The module route is Bone Sump mound stairs, then the Temple, then T9, then the short hallway, then the T10a metal door, then T10b. Fixed, pending live test.
  - RESOLVED (user decision): Bone Sump is discovered through Fasha's or Giva's hook. The Bone Sump location text tells the GM not to send the player there until Fasha or Giva tells them of it, then to call `discover_location` for `bone-sump`. Live-verified on a fresh Twin Lakes campaign (port 8770): when Mara asked for Bone Sump at once, the GM kept her in Isthmus Town and sent her to ask in town. After Giva's Home, the GM discovered Bone Sump. Bone Sump was not discovered before Giva's hook.
- OPEN (Phase 12, invented locations): the GM can call `create_location` and invent places the module does not have, such as "Thornhallow" and "South Bank Landing". Nothing stops this on module-backed worlds. Decide whether to restrict `create_location` on module worlds or allow it and flag it.
- RESOLVED (Phase 12, user decision: enforce sensible travel through the GM, with enough information to do it): the engine does not check routes. The `locations` rule and the `move_actor` description say that travel follows the routes in the location descriptions, and that a player is never moved to a place they have not heard of. Every module location's description ends with a `Route:` sentence from the module text. A place the story reveals first is discovered with `discover_location`. Found in a live run: the GM moved Mara from Isthmus Town straight to White Tower Fens. Live-verified on port 8770: the GM moved Mara one stage at a time (Farming Path, Dunston, East Lake Pass, The Downs, White Tower Fens, then Bone Sump) and did not skip stages.
- NOT REPRODUCED (port 8771, one run): the reply after Giva's Home voiced her full directions, and a GM note recorded them. Keep watching this in later live runs; if it recurs, reopen it.
- RESOLVED (user decision: only the destination): a discovery at Giva's Home had also marked White Tower Fens, East Lake Pass, and The Downs as discovered, because her directions named them. The `locations` rule and the `discover_location` description now say that directions discover only the destination; the other places they name stay undiscovered until the player hears of them in detail or goes there. Live-verified on a fresh Twin Lakes campaign (port 8771): Giva's Home discovered only Bone Sump, and the reply named the Fens and the Pass without discovering them.
- OPEN: `create_location` lets the GM invent places on a module world. Decide whether module worlds should reject locations that are not in the module.
- OPEN (Phase 12, module text, user to decide; kept verbatim): "blast" (Suraru Imperator's bite) is undefined. "The Cities" and "Marchguard" are undefined. "memory prison" vs "memory prism" is treated as the prism item; the book prints both. "3 bandits*" has an asterisk with no footnote. "see p.3" points to a missing page. Vuk's heart count is "at least one" in the viscera line and three in Vuk's text.
- OPEN (Phase 12): the unarmed 1d4 is taken from the module for unarmed attacks. It is not verified against the printed page.
- OPEN (Phase 12): the module prints no HP or armor for Fasha. HP 6 is an assumption, from the "hardier" guidance. Confirm with the user.
- OPEN (Phase 12): Reeve's Office and the Cellar are nested under the Stuffed Inn in the source, but the one-level location schema puts them under isthmus-town. Decide whether the location schema needs nesting depth 2.
- OPEN (Phase 12): the Dungeon Events table is not reproduced in the world data.
- RESOLVED (Phase 12): the book's statless NPC HP tiers (average 3, hardier 6, serious 10 or more) are cairn2e data (`npc_hp`). Only an NPC with an `hp_tier` uses them.
  - RESOLVED (user chose: the GM picks the tier to fit the NPC's place in the story): `create_npc` takes an optional `hp_tier`, offered only when the rule set defines `npc_hp`. The cairn2e `npc-hp-tiers` rule, which the GM always receives, gives the book's guidance. An NPC with a statblock refuses `hp_tier` unless `force` is set, and `hp_tier` plus `hp` is refused. Without a tier, the NPC still rolls 1d6 HP. Tests: `tests/test_npc_hp.py`. Live-verified (commit `736d06e`, fresh DB): the GM created Hollis Vane, an ex-soldier, with `hp_tier: hardier` and got HP 6.
  - OPEN (Phase 12): the GM's first `create_npc` for Hollis Vane sent `quirks` as `{text, id}` objects, which the schema rejected (`argument 'quirks[0]' must be string`). The GM retried and succeeded. Consider making the quirk shape clearer in the tool description.
  - OPEN (Phase 12): a GM-created NPC with no statblock and no tier still rolls 1d6 HP. The GM is now told to set a tier, but the argument is optional, so an omitted tier still rolls. Decide whether a statless NPC must have a tier.
  - OPEN (Phase 12): the PC `hp` stat has a schema max of 6, so `character.stats.hp` cannot give an NPC more than 6 HP. A `serious` tier (10) passes through `hp_tier` only. Decide whether the stat bound should depend on the character or the NPC.
  - OPEN (Phase 12): hirelings (`hire_hireling`) roll 1d6 HP and have no tier. They are not covered by the GM's tier choice.

- OPEN (Phase 12, engine): attack modifiers on `update_npc` (`armor`, `attacks`) are cairn2e-specific in shape. Decide whether they belong in shared core or in cairn2e only.
- OPEN (Phase 12, tests): the `Ghoul` name collided with the new published Ghoul statblock. The test creature is now "Grave Thing" in five test files. `tests/test_content.py` still uses "Ghoul" in a loader test. This is a naming check, not a bug.

Unscheduled, needs a book reference or a decision:

- The bonds and omens section, the bibliography, and the game files and tools were not reviewed in detail. Revisit each before its phase.

## Bone Sump live run and bestiary convention (open items)

Decision (user): a monster's description is the book's description text, verbatim. Its mechanic bullets are ALSO added as whole rules, so the GM receives them through "Rules for what is present". Descriptions are never stripped and sentences are never split.

- RESOLVED: all 84 bestiary source entries follow the convention (`monsters.json`: 84 entries). Verification: every source bullet is in the description, and every mechanic bullet is a whole rule. Ghost's description was restored after regeneration. The regeneration dropped 17 fragment rule ids, and none are referenced in `src` or `tests`.
- OPEN (bestiary): mechanic/flavour calls that were judgement calls: aranea S0, sphinx S0 ("Immune to magic of any kind."), titan S2 (levitate and shape shift), invisible-stalker S0 ("Completely invisible and silent."), lamia S1, and troll S1 (regeneration). Confirm each with the user.
- RESOLVED (user): Traitor-Rats was removed. It was not in the source book list. Detachment tests now use killer-bees as the large-group template, with HP pinned to 3 in the test helper.
- RESOLVED (user): bandits and gnolls start as individuals. The GM runs them as a detachment when an encounter calls for it, by passing `detachment: true` to `create_monster`. The create result reports the flag. Tests: `tests/test_detachment.py` (bandit cases). Live check (campaign 1 "Bandit Road", fresh server on 8773): PASSED. Five bandits blocking the Farming Path came out as a detachment: the GM called `create_monster` with `template: "bandits"` and `detachment: true`, and the result reported `"detachment": true`.
- OPEN (bestiary): the Roc has no source statblock.
- OPEN (bestiary): source discrepancies for Warrior Snail Acolyte, Night Cat, and Boggart/Pixie/Will-o-Wisp. Night Cat's ruleset attack is `claws: 1d6+1d6` with no flag, and the Twin Lakes world overrides it with `night-cat-attack`.
- OPEN (injection): group members (bandits, gnolls) do not yet have a verified path into `present_rules_text`. A regression test is in progress (`tests/test_bestiary_convention.py`).
- OPEN (live, Bone Sump run): narration runs ahead of `move_actor`. Seen twice. (1) Arrival at Bone Sump was narrated while the engine still had Mara at The Dead Creek Trail. (2) The GM narrated Mara entering T2, the Columbarium, but no move was recorded, and the location panel stayed at Temple of the Fungal Lizards. Decide whether T2 is a sub-location of the Temple, and whether the prompt or tool results should require the move before narrated arrival.
- DECIDED (user): normal travel needs the place discovered first, so the GM reveals a place with `discover_location` when the story shows it, then moves the PC. Forced movement (a trap, a teleporter, or a spell) may move the PC to an undiscovered place, and the place is discovered on arrival. Both the `locations` rule and the `move_actor` description now say so. Before this change, the rule forbade every move to an undiscovered place, so the GM could not enter the Temple's rooms at all.
  - LIVE RESULT (fresh server on 8772): FAILED. Mara said she went into the Columbarium (T2). The GM narrated her passing the far doorway into a corridor beyond T2, with no `discover_location` and no `move_actor`. The Location tab still said Temple of the Fungal Lizards. So the text fix alone does not make the GM move. The narration still runs ahead of the tool calls.
  - FIX (guidance, not engine): the world state now lists the places inside the PC's location, with discovered status ("Places inside X: ..."), and the state-step prompt says the recorded location must match the narration. Discover, then move, before narrating arrival.
  - LIVE RESULT after the fix (fresh server on 8772): PASSED for a named room. "I step through the doorway into the Breeding Chamber, T3." The GM called `discover_location` ("T3. Breeding Chamber") and then `move_actor`. The Location tab showed T3. Breeding Chamber.
  - LIVE RESULT after the fix: PASSED for a vague move. "I push on through the far doorway into whatever lies beyond." The GM called `create_location` for "T3a. Rear Passage", then `move_actor`. The Location tab showed T3a. Rear Passage. The narration matched the engine.
  - DECIDED (user): the GM may invent places it needs. But when an authored route already links two places, the GM must use that link and must not invent an intermediary between them. The `locations` rule now says so ("A route that a location description names to another place is authored..."), and it has a regression test in `tests/test_world.py`.
  - LIVE RESULT (authored-link rule, fresh server on 8772, campaign 1): PASSED. From T3a the GM moved Mara back to T2 through T3 (discover and move, with no new place). Then "I twist the urn ... to open the hatch to T4" led to `discover_location` "T4. Maintenance Tunnel" and `move_actor`. No corridor was invented. The T4 narration matched the authored text (rubble over a cloth). The `dungeon_cycle` "loud" trigger fired from the urn.
  - OPEN (module fidelity): the GM could still invent an unauthored link where the module has none. T3 has no authored route, so the invented T3a is still allowed. Review the Temple's authored connections (T1 to T2, T3 to T2) against the module before the capstone play-through.
  - OPEN: the GM still narrates ahead of the tool calls when nothing forces it. Engine enforcement (reject or reconcile a narrated move with no tool call) is not built. Tracked with ISSUE_TRACKER item (j). The two live passes above rely on prompt guidance only.
  - NOTE (infrastructure): one turn failed with "provider deepseek is unreachable". The page showed "The GM stopped" with a Continue button, and Continue retried the reply successfully. Not a game bug.
- OPEN (live): the numbered Temple rooms (t1 to t10b) are already real locations nested under the Temple (commit 44cdbc6). The T2 failure was the GM narrating the move without calling `move_actor` for `t2-columbarium`, so the engine stayed at the Temple. Fix in the prompt or tool guidance, not the data. The room descriptions are condensed from `files/cas1-transcription.md` (section 4.12) with Route notes added. Decide whether to restore verbatim text, given ISSUE_TRACKER item (i) asks for briefer, non-spoiling descriptions. Also, the GM created the Suraru characters at the Temple rather than in a room.
- OPEN (live): Giva's second set of directions (Old Mill Road) conflicts with the first (creek toward the Fens). The GM invented "The Dead Creek Trail" as a filler. The user accepted invented fillers only between authored places, but the GM must not invent a route that contradicts the authored one.
- OPEN (live): the GM created "The Butcher" at Isthmus Town and then removed it with `remove_location`. Check whether invented places should be removed at all.
- RESOLVED (user): the Twin Lakes world's own `bandits` and `bandit-leader` monsters are removed, and so is the `morale-bandits` rule. Both duplicated the bestiary `bandit`, whose full text and rules (`bandit-rule-1`, `bandit-rule-2`, `bandit-loyal`) the GM now receives. A bandit leader is a stronger bandit: the GM calls `create_monster` with `template: "bandit"` and its own hit_points, strength, dexterity, willpower, and armor. The GM chooses the extra strength from the context. Tests: `tests/test_bestiary_convention.py` (group member rules), `tests/test_rule_attachments.py`.
- RESOLVED (live, 8773): the GM created a second "The Farming Path" when the authored "Farming Path" already existed. `create_location` checked only exact names. It now also refuses a name that matches an existing place once a leading "The" is ignored, and the error tells the GM to use `discover_location`. Test: `tests/test_world.py::test_a_place_cannot_repeat_an_existing_one_under_an_article`. OPEN: the GM may still invent a near-duplicate with different wording (for example "Farm Road" for "Farming Path"). 
- DECIDED (user): reject close duplicates too. A deterministic match is not reliable, so `create_location` makes a separate model call that receives the list of existing place names and decides whether the new name duplicates one. The engine refuses the creation when the call says it is a duplicate, and the error points to `discover_location`. BUILT (`services/campaigns.py` `check_new_location`, `NAME_CHECK_PROMPT`), and live-verified on 8775 and 8783 (the refusal fires for "Farm Road").

## Triage round: labels, hidden events, names, icons (tracker items 1, 4-6, 7-10)

- RESOLVED (tracker item 1, acting-PC label): each player message stores its speaker, and the transcript shows that speaker, not the current actor. Speakers are the PC or a hireling. Live-verified on fresh server 8775: the message reads "Arlo (you)". Not yet live-checked after an acting-character change (for example, a hireling speaks in an encounter). Unit tests: `tests/test_events.py`, `tests/test_encounters.py`.
- RESOLVED (tracker item 5, pronouns): NPCs take an optional `pronouns` field, set by the GM at creation. Live-verified on fresh server 8775: Oda was created with `she/her`, and the GM's narration wrote "Oda (she/her)". Unit tests: `tests/test_world.py`, `tests/test_events.py`.
- RESOLVED (tracker item 6, hidden events): every event kind has an engine-set hidden flag (`HIDDEN_KINDS`, `services/events.py`). A "Show hidden events" checkbox reveals them. Live-verified on fresh server 8775: 50 hidden rows were hidden by default, shown when ticked, and hidden again when unticked.
  - DECIDED (user): player dice rolls stay visible. Other tool calls are hidden: only rendered tool events reach the Log, and no built-in tool defines an activity line yet, so ordinary tool calls stay hidden. `tool.ran` stays visible, matching this rule.
  - DECIDED (user): NPC, location, object, and map record events are hidden, as GM bookkeeping. Discovery events ("Discovered: X") stay visible because the player perceives them. The narration instruction tells the GM to narrate each new NPC, place, or object the first time it appears. Live: "New location" and "New character" were hidden, and the GM narrated Oda.
- DECIDED: near-duplicate place names are refused. Exact and article variants ("The X" / "X") are refused deterministically. Close variants (for example "Farm Road" for "Farming Path") are refused by a model call (`NAME_CHECK_PROMPT`). Live-verified on fresh server 8775: the GM tried "Farm Road", the refusal came back, and the GM created "South Ferry Landing". Tests: `tests/test_place_names.py`.
  - OPEN (narration leak, found live): after that refusal, the GM told the player about the naming rules and quoted the rule's example ("the rule's own example is literally 'Farm Road'"). A narration prompt sentence now forbids mentioning refusals, rules, or tool descriptions. It is prompt guidance only.
  - Re-tested live on 8783 with "Farm Road" (the player explicitly asked for it). The refusal fired, and the GM explained it to the player: it named the engine and said the name is treated as the same place. The user decided (after an ask_user question): this is OK when the player explicitly requests a duplicate place, so the GM may explain the refusal in plain terms. A stricter silent redirect was not chosen. The narration prompt still forbids mentioning refusals in other cases.
  - Also observed: the GM retried "Farm Road" and then a fallback name, six tool rounds in total, before explaining. Acceptable, but watch it.
- RESOLVED (tracker item 4, icons): 60 SVG icons (terrain 15, objects 25, items 14, tokens 6) are under `web/static/icons/`, indexed by `registry.json` and `web/icons.py`. Map markers show each place's `icon` (six seed places have one; the fallback is `town`). Inventory items show `book` for spellbooks, `scroll` for scrolls, and `weapon` for anything with damage. Gear and relics have no icon. Live-checked on a fresh server: the map shows town markers, and the inventory shows the weapon icon on the Leather Whip. Tests: `tests/test_icons.py`. GAP (refactor): `ITEM_KIND_ICONS` in `web/icons.py` names Cairn item kinds in Python. Move that mapping into ruleset data.
- RESOLVED (tracker item 7, Twin Lakes layout): the seed locations carry `x`/`y` in kilometres from the map's center, read from the module's overview map (`CAS1_Map_v2`). Twelve places are placed (the Temple of the Fungal Lizards is included): Isthmus Town, Dunston, Deadmill, The Stone Forest, Old Mill Road, Farming Path, East Lake Pass, The Downs, White Tower Fens, The White Tower, and Bone Sump. The earlier faint-dot map was caused by seed places with no positions. The map has no scale bar. The user confirmed one hour's travel on foot = 4 km (the module's times fit this: Isthmus Town to Dunston is one hour, and Isthmus Town or Dunston to the White Tower Fens is two). Positions were read by eye, so they are approximate. The Temple is not drawn on the overview. The user chose to place it near Bone Sump, about 0.6 km southwest, and it stays undiscovered until found. It is a top-level place, since a place nested inside another has no position. Tests: `tests/test_content.py::test_twin_lakes_map_matches_the_module_map`, `tests/test_world.py::test_twin_lakes_map_places_every_module_location_it_has_a_position_for`.
- RESOLVED (tracker item 8, travel time): the user confirmed the speeds in `travel.json`: on foot 4 km/h, road 6, rough terrain 3, vehicle 8, animal 6. A leg with no computable distance takes the confirmed 10-minute default. NPCs and hirelings move free, and hirelings move with their employer. Live-checked on a fresh server: Isthmus Town to Farming Path (34 min) to Dunston (37 min), charged as 10 (first placement, default leg) + 34 + 37 = 81 min. That gives one clock hour and 21 minutes carried. The event `travel.charged` stores the carried remainder, not the leg's minutes, and its docstring says so. The GM may interrupt travel or rest with a story moment or an encounter, which is prompt guidance only. GAP: the cairnrpg Procedures page that held the original travel rates returns 404 when fetched, so the rates rest on the user's confirmation, not a live source. Tests: `tests/test_travel.py`.
- RESOLVED (tracker item 3, terrain, objects, templates): standard encounter templates (room, corridor, clearing, shoreline) and the terrain and object catalogue live in the generic `encounter_content.json` in the core ruleset, not in Cairn code. The GM can adjust a template per encounter with `set_map_cells`. Obstacle objects block movement (`wall` is the only solid object). Difficult terrain (mud) costs 2 movement on entry. Other terrain is descriptive. Background terrain and walls render as flat colours, and other objects keep their icons (user decision: templates stay minimal, and solid colour avoids clashing with the icons). Off-turn placement still needs a route around walls, and a blocked route is refused. Cost is only charged on the acting participant's turn. The result carries `cost`, because the GM once invented a distance. Live-checked on fresh servers 8781 and 8782: the wall refusal, the route around the wall (cost 7), mud (cost 2), and colour rendering all work. Tests: `tests/test_encounter_content.py`, `tests/test_encounter_marks.py`.
  - DECIDED (user): `set_map_cells` refuses a solid object on a cell a participant occupies, with "is occupied by X, so it cannot be walled off". The whole call is refused, and nothing changes. Test: `tests/test_encounter_marks.py::test_a_wall_cannot_be_set_on_a_cell_a_participant_occupies`.
  - GAP: `moves_left` is null until initiative is rolled, and the GM describes free movement without reporting it. Decide whether the GM should report moves before a turn begins.
  - GAP: tool coordinates are 0-based, while the GM narrates 1-based labels. The GM handled this correctly in live tests. Document the mapping in the GM guidance if it slips.
  - GAP: the GM does not start a turn by itself. Initiative needs a player roll.
  - GAP: token text contrast on dark terrain fills is checked only for the floor and mud, not for every colour.
- OPEN (tracker items 2 and 10, player input and narration coverage): prompt guidance only, as the user decided. Partly done: the narration instruction covers first appearances. Still open: the GM narrates ahead of tool calls when nothing forces it, and the GM can obey a player action the PC could not do. Both need a live test before the item closes.
- RESOLVED (tracker item 9, briefer location text): `discover_location` takes an optional player-facing `summary` (max 300 characters). The Places panel, the Location tab, and the "Here" block show only the summary, never the full description. Giving a summary for an already-discovered place replaces it through the hidden `location.summarized` event. With no summary, the player sees the place name only (confirmed by the user). Live-checked on a fresh server: the GM recorded a one-sentence summary for White Tower Fens, and the Places panel showed that sentence, not the GM-only description. Finding: before the fix, the GM's first summaries copied the full description (Bone Sump's summary was 238 characters). The tool guidance now asks for one short sentence, under 120 characters, and says not to copy the description. Only one sample has been checked since, so watch later summaries. Full suite: 1168 passed. Tests: `tests/test_world.py` (discovery and summary cases).
- RESOLVED (user request, map recenter): the map centers on the player character's place, and each distance label is measured from that place. With no placed place above the PC, the map centers on the origin. A Recenter button restores the default view after zoom or pan. Panning works only when zoomed in, which is existing behaviour. Live-checked on fresh server 8780: Isthmus Town read "0 km east, 1.2 km north" from Tideborn Net Shed, which matches the stored coordinates (-3.15, -0.85) and (-3.15, -2.1). Zoom, pan, and Recenter all worked.

## Live review on 8783: naming, travel minutes, dungeon cycles

- DECIDED (user): panning is allowed only when the map is zoomed in. At the default zoom the map centers on the PC and does not pan. No code change was needed.
- RESOLVED (live, 8783): tracker items 2 and 10 hold up for the cases tested. The player asked for a fireball and flight to the Temple, and the GM granted neither. A walk ran all tool calls before one narration, with no narration ahead of tool calls. These checks used prompt guidance only, so the GM can still narrate ahead of tools when nothing forces it (see the OPEN bullet in the gap register).
- DECIDED (user, after an ask_user question): a player who explicitly asks for a near-duplicate place ("Farm Road" for "Farming Path") is refused, and the GM may explain the refusal in plain terms. Live on 8783, the GM said the engine treats the name as the same place. This is accepted. A silent redirect to the existing place was not chosen. The narration rule still forbids mentioning refusals in other cases.
- RESOLVED (audit): the 56-minute Farming Path to Dunston charge was the carried remainder, not a leg. Leg minutes are the straight-line distance over the mode's speed: Farming Path to Dunston is 2.44 km on foot, which is 37 minutes. `travel.charged` stores the carried remainder (19 + 37 = 56 mod 60). The event field is named `minutes` but holds the remainder. Renaming it to `carried` is a cleanup to consider.
- DECIDED (user): dungeon cycles are forbidden during overland travel. The tool description and the prompt now say so. This is prompt guidance only. No engine check exists, because places have no dungeon or overland flag. Enforcing it needs a place-type field and a data change, which is a follow-up. Live on 8783, a travel turn made no dungeon call.

## Phase 12 completeness audit (CAS-1 import, read-only, checked against the transcription)

Verified gaps. Each is a data or rule fix to make before the Phase 14 capstone.

- DECIDED (user, commit e6d1ebc): Bandit Leader and Bandits stay out of twin-lakes. The cairn2e `bandit` carries the book's leader, detachment and loyalty rules, and the GM chooses leader overrides. A subagent re-added them; that was reverted.
- DONE: Section 4.13 outcomes are in `rules.json` as `outcome-vuk-reborn`, `outcome-gate-opened`, `outcome-vuk-reborn-and-gate-opened`, `outcome-vuk-stopped-gate-closed`. The second id differs from the audit wording because its heading has no Vuk condition. The 4.14 overview is `twin-lakes-overview`.
- DONE: Rule attachments. `deadmill-sinking` is on Deadmill. `deadmill-time-loop` is on Deadmill and on Jandy. `the-downs-travel` is on The Downs. `trapdoor-butcher-block` is on the Butcher Shop. The frog trappers rule names no location, so it stays unattached.
- DONE: The Eye of Vuk text keeps "[1d4]" as printed in the source.
- DONE: Location rules were silently dropped. `LocationTemplate` had no `rules` field, so the loader ignored them. Locations now carry rules through the loader, the `LocationAdded` event, and `Place`. `present_rules_text` includes the rules of the place the player is in, with the place name as the source. Tests: `tests/test_rule_attachments.py`.
- OPEN: `echo-spark` and `wisp-lamp` have no location link. The schema has no field for one, and the placements appear only in prose. Decide whether to add a field.
- OPEN: Stone Forest references the Wilderness Events table (Player's Guide pg. 78). The engine has no such table, and the source does not reproduce it. Needs a decision, not a guess.
- DONE: The Temple note now points to the cairn2e "Dungeon Events" table.
- OPEN: Victim Schedule (d8), Rumors (d6), and Encounters (d6) have no die field. `_load_table` reads only id, type, and options, so an added key is ignored. The Day 1 to 5 schedule text is not stored. Decide whether to extend the table schema.
- OPEN: The Bone Sump sconce objects read "150gp each". The source says "(150gp, bulky)" for the pair, so the per-object price is unclear. Ask the user.
- OPEN (decision): Suraru Imperator's venomous bite "blast" flag is dropped, and the source says it is undefined. Keep the note.
- OPEN (decision): Fasha's HP 6 is not printed in the source. It is an assumption.
- DECIDED (user, after an ask_user question): Vuk stays as line 406 records. Her ash form is story-only with the Choking Hold rule. Her full form uses the bestiary Lich stats. The user's note about an Ash Phantom statblock is not in the source, so no numbers are invented.
- OPEN: The start hook text in `world.json` could not be compared word for word, so recheck it against section 4 of the source.

Verified matches (no gap): the Night Cat, Annafranax, Suraru, Reeve Trellis, Aldra, Flavia, Jandy, Giva, Drum statblocks and descriptions, the Choking Hold, the Suraru Imperator critical damage, the Bloodsuckers and Vald Lore rules, and the Fletcher's Ire, Echo Spark, Moon Cloth, Curved Dagger, Watersnatch, and Memory Prism texts.

Triage decisions to record (user answers, not yet built):
- DECIDED: The "show hidden events" control moves into the model settings dialog.
- DECIDED: Hovering a terrain cell shows the terrain name and its movement cost.

## Wilderness procedure (tracker: wilderness cycle; decided, not yet built)

Source: Player's Guide, procedures page, "Wilderness Exploration" (the Stone Forest's "Wilderness Events table, pg. 78" is this table). Warden's Guide wilderness page has only the worked example.

User decisions:
- Scope: everything. The Wilderness Events roll, the Wilderness Actions, and the elements (night, sleep, light, weather).
- Design: ruleset data. Actions, tables, the lost roll, and weather live in the cairn2e JSON, and the engine reads them generically. No Cairn-specific service with hard-coded ids (unlike `services/dungeon.py`).
- Travel: switch to the book's watch model. Path, terrain, and weather penalties add watches. This replaces the minute-based leg, so the accepted 56-minute explanation and the minute charge change.
- Events are rolled by the engine after each overland Wilderness Action, the same way the dungeon cycle is triggered. `dungeon_cycle` stays for dungeons only.

Book rules to build (from the Player's Guide procedures page):
- Watches: three per day (morning, afternoon, night). Each character chooses one Wilderness Action per watch. A split party is treated as independent groups.
- Travel duration: watches. Path penalty: roads none, trails +1 watch, wilderness +2 watches. Path distance: short +1, medium +2, long +3. Terrain: Easy none, Tough +1, Perilous +2. Vast terrain up to +2. Weather adds per step. A pair of penalties sums.
- Lost: Path odds are roads none, trails 2-in-6, wilderness 3-in-6. Roll 1d6 after each Wilderness Action. A lost party must spend an action to recover.
- Wilderness Events, d6: 1 Encounter (roll the terrain or location table, then reactions), 2 Sign, 3 Environment (weather or terrain shift), 4 Loss (rations, tools, time, or effort), 5 Exhaustion (a barrier that costs an extra action or Fatigue), 6 Discovery (food, treasure, or resources; the Warden may reveal the area's main feature instead).
- Night travel: the Warden rolls the Events table twice. Night travel is far slower and more treacherous.
  - Fixed after W1: the double roll applies only to a night Travel action. Explore, Supply, and Make Camp at night roll once. The book says "Traveling at night is always more dangerous!" under Night, and the W1 code had rolled twice for every night watch. Data: `travel` has `effect: "travel"`, and `night_event_rolls` is 2.
- Sleep: the third watch is reserved for Make Camp. Skipping it adds a Fatigue to each member and makes them deprived. Travel while sleep-deprived raises terrain Difficulty by one step.
- Make Camp: each member and mount consumes a Ration. Members who rest remove all Fatigue.
- Light: torches light 40 ft, a torch lasts 3 uses, a lantern is refillable with an oil can (6 uses). Without light, panic.
- Supply: hunting, fishing, or foraging yields 1d4 Rations (3 uses each). The bounty die grows with party size (1d4, 1d6, up to 1d12).
- Weather: rolled daily by season (spring, summer, fall, winter) on a d6 table: Nice, Fair, Unpleasant, Inclement, Extreme, Catastrophic. Two Extremes in a row make it Catastrophic. Unpleasant adds a Fatigue or a watch. Inclement adds a Fatigue or +1 watch, plus a terrain step. Extreme adds a Fatigue, +1 watch, and a terrain step. Catastrophic: most parties cannot travel.

Decided (user, terrain source): travel path and terrain difficulty can be authored in the world data. Where the world data has none, the AI GM derives them and supplies them when it travels the party. Both paths feed the same watch calculation.

Clock fact: `clock.json` already has three parts (morning, afternoon, night) of 8 hours each. Those map directly onto the book's three watches, so no clock change is needed.

Decided (user, needs): track all the book's needs. Rations and torches are items with uses (the existing `ItemUsesSet` mechanism). Fatigue is the existing inventory penalty item (`penalty_item: fatigue`). Sleep is the existing need (`need.json`). No new engine counters.

Decided (user, season): the campaign gets a season (Spring, Summer, Fall, Winter), set at the start and advanced by the clock. Weather uses the current season.

Decided (user, season length): 90 days per season.

Gaps to resolve before building (ask the user):
- None.

### Design (decided; build in three phases, each live-checked and committed)

Book facts checked against the Player's Guide procedures page (Wilderness Exploration). Actions: Travel, Explore, Supply, Make Camp. Each watch is one cycle: one action per character, then one Wilderness Events roll (two at night). Travel also rolls 1d6 for lost (roads never, trails on 1-2, wilderness on 1-3). Weather is rolled once a day by season. Weather rows: Nice, Fair, Unpleasant, Inclement, Extreme, Catastrophic (two Extremes in a row).

Ruleset data (cairn2e, all generic in the engine):
- New content type `wilderness` (file `wilderness.json`): `seasons` (spring, summer, fall, winter in order), `season_days` (90), `night_event_rolls` (2), `catastrophe_streak` (2), `lost_on` by path (roads 0, trails 2, wilderness 3 faces of d6), and the four Wilderness Actions with their text (travel, explore, supply, make_camp).
- Dice tables in `tables.json`: `wilderness_events` (d6: Encounter, Sign, Environment, Loss, Exhaustion, Discovery, with text). Weather is one d6 table per season: `weather_spring`, `weather_summer`, `weather_fall`, `weather_winter`.
- Start season: a world's start data may set `season`. The default is spring. The season for a day is derived from the day count: `seasons[(start + (day-1) // season_days) % 4]`. No stored season state beyond the start.

Engine (new `services/wilderness.py`, generic, reads the `Wilderness` dataclass):
- `roll_weather`: once per in-game day, lazily, on the first wilderness action or travel of that day. Stores the day's weather, and counts consecutive Extremes to make Catastrophic.
- `wilderness_action(action, reason, ...)`: travel and explore return results. Supply rolls 1d4 rations, with the die growing with party size (1d4, 1d6, up to 1d12). Make Camp uses one Ration per member and clears Fatigue. Make Camp is the night watch.
- Events and lost are logged as events (`WildernessEventRolled`, `WeatherRolled`, `LostRolled`, `SupplyGathered`, `CampMade`). New event fields have defaults, and the HIDDEN_KINDS test must still pass.
- Registered in `rules/cairn2e.py` and exposed through the tool schemas in `tool_calls.json`. The prompt text says which tool to use for which action, and the dungeon-only restriction stays.

Phases:
- W1: calendar, weather, the four actions, the events roll, and the Make Camp / Supply rules. No change to travel yet.
- W2: travel as watches. Replace `_travel_minutes` and `_charge_travel` with watch penalties (path, distance, terrain, weather, vast, deprivation). Each watch of travel is one cycle: a lost roll, then an events roll (two at night). Path, terrain, and distance come from the world data where authored, otherwise the GM passes them in. The clock advances by watches (parts, eight hours each). The minute-based leg is removed for overland travel.
- W3: consumables. Torches (3 uses) and lanterns (oil can, 6 uses) lose a use each night watch, with no light causing panic. Rations are consumed by Make Camp (PC and hirelings). Fatigue uses the existing penalty item.
