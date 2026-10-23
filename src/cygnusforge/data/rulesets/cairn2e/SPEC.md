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
| Generic engine | Phase 11 moves every Cairn-specific term, number, and prompt rule out of the engine and into the ruleset JSON, parameterized where a second rule set would need the same hook. |
| Capstone | Phase 12 imports the first-party adventure CAS-1 in full and plays it through as the end-to-end test of Cairn 2e. |
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
- The clock counts hours, so `advance_time` takes `hours`. A part is 6 hours (confirmed by the user).
- A hireling is promoted with `hireling.promoted` once the PC is dead, and only if it is alive and not dying. It keeps its gear and stops being paid.
- A dead employer's hirelings are unpaid, so the GM decides what they do.

Acceptance:
- Unit tests: the one-hour clock fires, bandages stop it, Doomed kills on a failed save, Mortal Wound sets max HP on recovery, and a death emits an event and opens the death screen.
- A live session covering Critical Damage, bandaging, a death, a replacement PC, and a hireling promotion.
  - Done live (fresh DB, campaign 1): a wolf bite overflowed to STR, the STR save failed (`character.dying`, one-hour clock), the encounter ended, the GM advanced three hours and the clock ran out (`character.died`), the GM narrated the death in the same turn, the death screen appeared on reload, and a replacement PC (Kalam) was created with starting gear and placed by the GM.
  - Done live (campaign "Phase Nine B", a hireling Tam, and Dorn in critical damage): the GM stabilized Dorn with a Bandage, and `character.stabilized` fired. The GM first tried `use_item` on Tam's bandage with Dorn as the target, and the engine refused it (see the naming fix below). The GM then gave Dorn a Bandage and stabilized him, which left Tam's bandage in his pack. This live pass did not run the clean `use_item` path.
  - Done live (campaign 1 "Phase Ten Live", fresh DB): Tam was hired with catalogue Bandages (3 uses), the post-fix naming. Dorn at 0 HP and STR 1 was shot outside combat, and the engine recorded instant death (`character.died`). The death screen offered "Play Tam", and promotion fired (`hireling.promoted`). Tam then killed a foe with a hand-axe in a later fight.
  - Narration mismatch found live: the engine killed Dorn at once, but the `combat.harmed` status read "in critical damage", so the GM narrated an hour of dying. Fixed: a PC killed outright outside combat now reads "dead" (`player_character_dead` in `combat.json`, `defeat_status(..., killed=True)`). A test covers it (`tests/test_death.py`).
  - Not yet live: a bandage used on a dying PC by a hireling with the singular name, a death by the clock with a hireling present, and wages stopping after promotion on a new day.

Fixed during the live check:
- A dying record was lost when nothing committed after it. `begin_dying` and the Doomed branch now commit. A regression test covers this (`tests/test_death.py`). It was hidden in most fights because the encounter-end commit ran first.
- A Bandage given as "Bandage" (singular) matched no catalogue entry, so it became a freeform item with no uses, and the aid list ("Bandages") did not match it either. Item names now match ignoring case and a plural s (`same_item_name` in `services/events.py`, used by the catalogue lookup, the held-item lookup, and the aid match). A regression test covers the singular name (`tests/test_death.py`). The live server was running the old code during this session. Verify the fix live at the next dying PC.

Gaps from Phase 9:
- Resolved: 6 hours per part, confirmed by the user.
- Resolved: STR 0 outside combat means instant death, confirmed by the user.
- OPEN: a new PC is placed by the GM on its first turn. The book's rule for when a new PC joins is not modeled, so the GM decides by narrative.
- OPEN: a dead PC's gear is not lootable by tools. The GM handles it in narration only.
- Confirmed live: the death screen appears when the page reloads at the end of the turn, not mid-turn.
- OPEN: `test_a_new_player_character_is_refused_while_one_lives_and_allowed_after` failed once in a full run and has not failed since (40 isolated runs and 30 runs of the related files, all passing). The likely cause is the commit bug above, since whether a random fight ended with a commit decided whether the dying record survived. Not proven. Watch for a recurrence.

## Phase 10: dungeon exploration

- Dungeon turn cycle: the party's actions in turn order, with the cost of time recorded.
- Dungeon Events table, rolled when the party spends too long in one place, makes noise, or enters a new area.
- Dungeon seeds and generated rooms are GM content, not engine logic.

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
- `resolve_detachment` is Cairn's tool name. Either it stays in `rules/cairn2e.py`, or it is renamed generically (for example `resolve_group_break`). Decide during the phase.
- Relics (`give_relic`, `use_relic`, `recharge_relic` in `services/inventory.py` and `rules/context.py`): a generic "consumable with recharge rule" type, or kept in `rules/cairn2e.py`. Decide during the phase.
- Debug panel stat options hard-coded in `web/templates/campaign.html`: rendered from the rule set's stats.
- GM prompts in `services/campaigns.py` (`STATE_INSTRUCTION` and related): the generic loop stays in code. Cairn bullets (armor glancing blows, relic recharge, detachment, defeated PC rules, combat speech policy, and the sentence cap) move to the ruleset as `prompt_fragments` and `narration` config.
- Weapon damage: free text from the GM's attack (`services/combat.py` around L484–500). Weapons should read their damage and features from the ruleset.
- Names list in `services/names.py`: low priority. Optional per-rule-set JSON.

Already generic (keep, and use as the pattern): stat keys from JSON (`morale.wil_stat`, `morale.retreat_stat` before the rename), clock parts, the debug gold route keyed on `marketplace.currency`, the marketplace structure, the `core` shared rule set, dice tables passed in by ID, scar tables passed in as `ScarTable`, modifier names as a mapping, and generic event types with ruleset-supplied names.

Done when: a grep for `Cairn`, `Fatigue`, `relic`, `detachment`, `Scar`, `deprivation`, and the stat names in `src/cygnusforge/services`, `src/cygnusforge/web`, and `src/cygnusforge/rules/context.py` returns no hits outside docstrings that name the rule set generically. The full suite and a live Cairn session must still pass with no change in behavior, except for the `(used)` wording and the speech-cap fix, which are gap-register items.

## Phase 12: capstone, CAS-1 "Trouble in Twin Lakes"

Import the first-party adventure in its entirety and play it through as the end-to-end test of Cairn 2e: https://cairnrpg.com/adventures/first-party/cas-1

Known content from the module text (read in full during the design pass; statblocks and tables transcribed during the phase):

- Two mysteries: Aldra the butcher's disappearance and Jandy the tithingman's death loop. Hooks offer 750 gp, a 250 gp bonus, and 200 gp each.
- Locations: Isthmus Town (the Stuffed Inn with its Reeve's Office and cellar, the Butcher Shop, Giva's Home), the Farming Path, Dunston, Fasha's Farm (farmhouse and abandoned home), Old Mill Road, the Stone Forest, Deadmill, East Lake Pass, the Downs, White Tower Fens (Whistlewood, pit trap, log trap, the White Tower, Bone Sump), the Temple of the Fungal Lizards (antechamber, columbarium, breeding chamber, and more), and The Roots.
- NPCs with statblocks: Reeve Trellis, Fasha (STR 14, DEX 13, WIL 5, fists d8, hook knife d6), Aldra, Flavia, Jandy, Giva, Drum, Annafranax (the Frog Witch), and Vuk (a monster). Monsters include Night Cat and Watersnatch.
- Relics: the Eye of Vuk, the Echo Spark, and the Wisp Lamp. These use the Phase 5 relic rules.
- A 1d8 daily victim schedule and quicksand.
- Dungeon Exploration procedures (book p.71), not Wilderness, plus random encounter tables. These need Phase 10.

Done when: every location, NPC, monster, relic, and table in the module is in the ruleset or in an adventure data file, the module can be played from start to finish in a live session, and the transcript reads correctly. Gaps found are added to the gap register and closed or scheduled.

## Gap register

Every known gap, from the checkpoint history and the live tests, with the phase that closes it. "Unscheduled" means it needs a decision or a phase, and that decision is pending.

Engine and prompt (cross-cutting):

- Combat speech sometimes exceeds the "one sentence, two at most" cap. Prompt-only. Needs a validator or stronger prompt, then a live re-test. Phase 11.
- Speech outside combat is not capped. An NPC gave a long non-combat speech. Phase 11.
- Repeated speech lines (four "Fenwick" lines in one scene). Cause unverified; narration quoting NPC words is the leading hypothesis. Unscheduled, needs investigation.
- The GM accepts player-invented past events as fact. Unscheduled, needs a prompt rule and a test.
- Debug panel stat options are hard-coded in the template. Phase 11.
- Cairn-specific GM prompt wording lives in `services/campaigns.py`. Phase 11.
- Weapon damage is free text, and weapons do not read their features. Phase 11.
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
- The GM narrated a night's camp and eating without calling `rest`, so no ration was used. Needs a prompt rule and a test.
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
- OPEN: a hireling kit item the GM names freeform, such as "Bandage", gets no uses. The catalogue match is now plural-tolerant, but a freeform name still has no uses. Gaps like this should be caught by a `give_item` validator. Phase 11 decides.
- Resolved: 6 hours per part, confirmed by the user.
- Resolved: STR 0 outside combat as instant death, confirmed by the user.
- A new PC is placed by the GM, not by the book's rule for when a new PC joins. The narrative decision is the GM's.
- A dead PC's gear is not lootable by tools. The GM handles it in narration only.
- Whether a dead PC's gear counts toward the party inventory is undecided.
- `_save` and `player_save` use the word `critical` for different things. `player_save` always reports `critical: false`. Rename to avoid confusion. Unscheduled.
- Death happened at the end of a three-hour advance, not at the one-hour mark. The GM chose the advance. Consider whether the clock should stop the advance at the death hour. Unscheduled.
- The flaky-test cause above is unconfirmed.

Phase 10 (dungeon exploration): the dungeon turn cycle, Dungeon Events, treasure, and gold income.

Phase 11 (generic refactor): the list above.

Phase 12 (capstone): the CAS-1 import and any gaps it reveals.

Unscheduled, needs a book reference or a decision:

- The bonds and omens section, the bibliography, and the game files and tools were not reviewed in detail. Revisit each before its phase.
