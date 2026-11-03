# Worlds Without Number parity checklist

This is the checkable status of the ruleset. It is not a claim of full parity. Every **yes** row
names the JSON, tool, or test that proves it; **partial** and **open** rows name the remaining
gap. Keep this list and [SPEC.md](./SPEC.md#known-simplifications) in step when a gap closes.

Legend: **yes** implemented with evidence; **partial** engine support exists but the source
procedure is not complete; **open** rule text or GM procedure only.

| Area | Status | Evidence | Remaining gap |
| --- | --- | --- | --- |
| Character creation: attributes, skills, backgrounds, classes, Foci, gear | yes | `classes.json`, `backgrounds.json`, `foci.json`, `equipment.json`, `stats.json`; `tests/test_wwn.py`, `tests/test_wwn_advancement.py` | — |
| Dual-mage Adventurer with two partial traditions | yes | `classes.json` (`adventurer_mage_mage`, `*_partial_2`), `progression.json` (`dual_mage`); `tests/test_wwn_advancement.py::test_a_dual_mage_takes_two_partial_traditions` | — |
| Arbitrary partial-class pairings, including Healer and Vowed | open | `classes.json` has four fixed Adventurer pairings; Healer and Vowed are standalone classes | No generic partial + partial class builder |
| Readied and Stowed pools | yes | `equipment.json`: `readied_stat: "str"`, `readied_divisor: 2`, `capacity_multiplier: 1.5`; `services/inventory.py`; `tests/test_wwn_combat.py::test_a_stowed_weapon_must_be_readied_before_it_can_strike` | — |
| Armor-encumbrance penalty to Sneak/Exert | open | Armor descriptions and `rules.json` state the penalty | `roll_check` does not apply it automatically |
| Shield bash | partial | `equipment.json` has `shield_bash_large` and `shield_bash_small` attack profiles | No state suspends the shield's AC until the next turn |
| Effort commitment eligibility | yes | `stats.json` derived pools; `progression.json` tradition Effort stats; `wwn_magic.commit_effort`; `tests/test_wwn_magic.py::test_effort_cannot_be_committed_to_an_unavailable_tradition` | — |
| Effort pools only on eligible sheets | open | `stats.json` derives `effort_high`, `effort_necromancer`, `effort_elemental`, `effort_healer`, and `effort_vowed` for every character | Every pool is derived and displayed, not just the character's |
| Surprise, grappling, execution attacks, total defense, dual wielding, range bands | partial | `conditions.json` (`grappled`, `prone`), `hit_modifier` on `attack`/`player_attack`, `prompts.json`, `rules.json` | No engine states, surprise round, or range tracking; the GM runs the checks and modifiers |
| Bestiary multi-attacks | partial | `combat.json` `multi_attack: "each"`; `attack` uses `MonsterTemplate.attacks_per_action`; `tests/test_wwn_combat.py::test_a_statblocks_attack_count_is_resolved_in_one_action` | `attacks_per_action` is parsed from the description, not a structured field |
| Monster special abilities | open | `monsters.json` keeps abilities in `description`; every `rules` list is empty | No machine-readable abilities |
| PC Morale and Instinct | partial | `rules/wwn.py`: `check_morale` and `instinct_check` refuse PCs, as the source says | PCs still roll and display a Morale score |
| Projects, spell research, Magic Workings, faction-turn tracking | partial | `start_project`, `advance_project`, `abandon_project`, `list_projects`; `prompts.json`; `tests/test_wwn_combat.py::test_a_project_tracks_progress_to_completion` | Generic progress only; book-specific costs, required rolls, and faction assets are GM text |
| Hireling tracking and wages | yes | `hire_hireling`, `dismiss_hireling`; `rules/wwn.py`, `services/world.py`; wage and unpaid events; `tests/test_hirelings.py`, `tests/test_promotion.py` | — |
| Magic item creation | open | `rules.json` (`magic-items`) only | No creation or crafting subsystem |
| Developed Attribute, Specialist, Spirit Familiar choices | open | `foci.json` uses `note` effects for the choices | Choices are not stored or validated |
| Named NPC defeat (`important_npc`) | yes | `combat.json` `defeat.important_npc`; `tests/test_library_strictness.py::test_an_important_npc_is_mortally_wounded_not_dead`, `tests/test_wwn_combat.py::test_an_important_npc_is_mortally_wounded_rather_than_killed` | — |

The tool catalog (`tool_calls.json` plus `adventure_tools.json`) is checked by
`tests/test_parity_docs.py`, which also pins the project and hireling tools and the
`important_npc` defeat label.
