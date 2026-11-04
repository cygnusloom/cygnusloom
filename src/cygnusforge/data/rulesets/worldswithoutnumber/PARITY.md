# Worlds Without Number parity checklist

This is the checkable status of the ruleset. It is not a claim of full parity. Every **yes** row
names the JSON, tool, or test that proves it; **partial** and **open** rows name the remaining
gap. Update this list when a gap closes or a new area is added.

Legend: **yes** implemented with evidence; **partial** engine support exists but the source
procedure is not complete; **open** rule text or GM procedure only.

| Area | Status | Evidence | Remaining gap |
| --- | --- | --- | --- |
| Character creation: attributes, skills, backgrounds, classes, Foci, gear | yes | `classes.json`, `backgrounds.json`, `foci.json`, `equipment.json`, `stats.json`; `tests/test_wwn.py`, `tests/test_wwn_advancement.py` | — |
| Dual-mage Adventurer with two partial traditions | yes | `classes.json` (`adventurer_mage_mage`, `*_partial_2`), `progression.json` (`dual_mage`); `tests/test_wwn_advancement.py::test_a_dual_mage_takes_two_partial_traditions` | — |
| Arbitrary partial-class pairings, including Healer and Vowed | yes | `classes.json` source-valid pairings, including Healer and Vowed; `tests/test_wwn_advancement.py` | — |
| Readied and Stowed pools | yes | `equipment.json`: named `stowed` (1 x STR) and `readied` (0.5 x STR) pools, `size_pools`/`default_pool`; `services/inventory.py`; `tests/test_inventory.py` and `tests/test_inventory_schema.py` | — |
| Armor-encumbrance penalty to Sneak/Exert | yes | Armor features and shop items carry `encumbrance_checks`; `WorldTools.roll_check` subtracts the item's slots; `CheckMade` records `armor_penalty`; `tests/test_wwn_combat.py` | — |
| Shield bash | yes | `equipment.json` bash profiles; `ShieldBashed` suspends the shield's actual AC contribution until `EncounterTurnBegun`; `tests/test_wwn_combat.py::test_a_shield_bash_suspends_the_shields_ac_until_the_next_turn` | — |
| Effort commitment eligibility | yes | `stats.json` derived pools; `progression.json` tradition Effort stats; `wwn_magic.commit_effort`; `tests/test_wwn_magic.py::test_effort_cannot_be_committed_to_an_unavailable_tradition` | — |
| Effort pools only on eligible sheets | yes | `stats.json` `requires_templates` on each Effort stat; `content/character.py` `DerivedStat` availability; `tests/test_wwn_effort.py` (Warrior and Expert have none, each Mage tradition has its own, Healer and Vowed have theirs, dual-mage keeps two) | — |
| Surprise, grappling, execution attacks, total defense, dual wielding, range bands | yes | `combat.json` `surprise_turns`, `grapple`, execution preparation, `total_defense`, `dual_wield_*`, `range_bands`; events/tools; `tests/test_wwn_combat.py` combat-action tests | — |
| Bestiary multi-attacks | yes | `combat.json` `multi_attack: "each"`; `monsters.json` `attacks_per_action`; `attack` uses `MonsterTemplate.attacks_per_action`; `tests/test_wwn_combat.py::test_a_statblocks_attack_count_is_resolved_in_one_action`, `tests/test_parity_docs.py::test_wwn_bestiary_attack_counts_are_structured_fields` | — |
| Monster special abilities | yes | `monsters.json` `abilities` with validated kind/trigger/save/condition/damage; `MonsterTemplate.abilities`; `tests/test_parity_docs.py::test_wwn_bestiary_abilities_are_structured_fields` | — |
| PC Morale and Instinct | yes | `stats.json` no longer defines Morale for PCs; `rules/wwn.py`: `check_morale` and `instinct_check` refuse PCs, as the source says; `tests/test_wwn.py::test_a_wwn_player_character_sheet_has_no_morale_but_a_statblock_does` | — |
| Projects, spell research, Magic Workings, faction-turn tracking | yes | `projects.json` targets/costs/currencies/skills; `start_project` charges and validates them; `tests/test_wwn_combat.py::test_a_source_project_charges_its_cost_and_requires_its_skill` | Faction assets remain table-driven, but the project kind and turn tracker are modeled |
| Hireling tracking and wages | yes | `hire_hireling`, `dismiss_hireling`; `rules/wwn.py`, `services/world.py`; wage and unpaid events; `tests/test_hirelings.py`, `tests/test_promotion.py` | — |
| Magic item creation | yes | `create_magic_item` charges the cost and records the item and its powers; `tests/test_wwn_combat.py::test_creating_a_magic_item_charges_its_cost_and_records_its_powers` | — |
| Developed Attribute, Specialist, Spirit Familiar choices | yes | `foci.json` `picks`; `choose_feature_choice` validates and stores the choice; `tests/test_wwn_combat.py::test_a_developed_attribute_focus_stores_a_validated_choice` | — |
| Named NPC defeat (`important_npc`) | yes | `combat.json` `defeat.important_npc`; `tests/test_library_strictness.py::test_an_important_npc_is_mortally_wounded_not_dead`, `tests/test_wwn_combat.py::test_an_important_npc_is_mortally_wounded_rather_than_killed` | — |

The tool catalog (`tool_calls.json` plus `adventure_tools.json`) is checked by
`tests/test_parity_docs.py`, which also pins the project and hireling tools and the
`important_npc` defeat label.
