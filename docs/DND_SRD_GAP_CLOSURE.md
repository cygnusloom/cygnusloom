# SRD 5.2 gap closure plan

## Implementation status

Closed: feature choices, pattern modes, class-feature level-up choices, named A/B/C
template kits, background equipment packages, turn resources, condition roll modes and
modifiers, background ASI modes, Alert/Savage Attacker, Magic Initiate spells and free
cast, Dragonborn/Tiefling/Human/Dwarf species mechanics, lineage choices, armor training
and categories, versatile damage, class always-prepared spells, Circle of the Land,
level-2/3 class feature choices, lineage cantrips and level-3/5 spells, Goliath ancestry
resource use, weapon mastery data and choices (Graze, Sap, Slow, Topple, Push, Vex),
Heavy weapon disadvantage, Rage's resource/action/mark/damage/resistance, Improved
Critical, Sneak Attack dice, per-feature resource/turn costs through `use_feature`, and
multiclassing.

This plan covers every mismatch recorded in `docs/DND_PLAYTEST.md` plus the action-economy
gap found in live play. The target is not a second character-creation app: every new
mechanic starts as a generic JSON-driven building block where that is practical, and
D&D-specific interpretation stays in `rules/dnd_srd_2024.py`.

## Acceptance rules

- Every closed gap gets a service-level test that proves the rule, not just that a field
  parses.
- Every choice closes through the normal creation or advancement form; no test-only
  back door.
- Every live path is checked with a throwaway server and DeepSeek.
- Content validation must stay at zero warnings and the full suite must stay green.
- Remaining source features that cannot be represented without a new class-level model
  (full weapon-mastery automation) are listed explicitly rather than silently omitted.

## Phase 1 - generic building blocks

1. **Feature choices**
   - `FeaturePick` gains `feature`/`spell` kinds, feature filters (`tag`, `feature_type`,
     `max_rank`), `patterns` for ASI allocation, and grants `proficiency`, `expertise`,
     `learn`, `resistance`, and `tool`.
   - Add a form mode control for `patterns` so a background can choose +2/+1 or +1/+1/+1.
   - Level-up feature choices use the same parser and are recorded on the character.

2. **Feature properties**
   - Add `versatile_damage`, `damage_type`, `resistances`, `casting_ability`,
     `flat_hp_bonus`, `level_hp_bonus`, `initiative_bonus`, `initiative_proficient`,
     `armor_training`, and `min_level`/rank-gated grants.
   - Apply the properties in creation, level-up, attack/spell math, and sheet rendering.

3. **Template kits**
   - `TemplateGrant` gains named alternatives (`A`/`B`/`C`), each with items, equipment,
     silver, and optionally a tool-pick placeholder.
   - Creation records the chosen kit; no kit choice means the first option.
   - Backgrounds can grant their equipment package or its 50 GP alternative.

4. **Turn resources**
   - `CombatRules` gains action, bonus-action, and reaction limits; `Encounter` tracks
     what each turn has spent.
   - `encounter_view` exposes a `turn_resources` list so every rule set can label its own
     resources instead of the UI hard-coding "attacks".
   - Attack/spell/resource tools mark the resource they spend; a generic
     `use_turn_resource` tool covers narrative actions.

5. **Conditions**
   - `ConditionRules` gains directional roll modes (self attack/check/save, attacks
     against), flat save/damage/initiative/speed modifiers, and a level counter for
     exhaustion.
   - Rolls merge advantage/disadvantage and flat modifiers centrally.
   - D&D condition data maps blinded, charmed, frightened, grappled, incapacitated,
     invisible, paralyzed, petrified, poisoned, prone, restrained, stunned, unconscious,
     and exhaustion.

6. **Spell preparation**
   - Class progression data gains always-prepared spells and a level-1 free-cast pool
     for Magic Initiate/Divine Smite/Hunter's Mark where the source gives one.
   - Always-prepared spells are excluded from the prepared limit and restored on a long
     rest.
   - Circle of the Land is a normal advancement/rest choice.

## Phase 2 - D&D content

0. Multiclassing: class-level state and events, per-class advancement, prerequisites,
   Hit Dice pools, class features and subclasses, multiclass armor/skill training,
   multiclass spell slots, separate Pact Magic slots, per-class spell preparation, and
   the Advancement page's class selector.
1. Background ASI modes and background equipment packages.
2. Class kit alternatives (A/B/C) and the Monk tool choice.
3. Origin feats: Magic Initiate, Alert, Savage Attacker, Skilled.
4. Species: Human Skillful/Versatile, Elf/Dragonborn/Gnome/Goliath/Tiefling choices,
   level-gated flight/Large Form/lineage spells, Dwarven Toughness, Draconic Resilience.
5. Armor training, armor categories, and shield training.
6. Versatile weapons and two-handed attacks.
7. Class always-prepared spells, Circle of the Land, and level 2-3 advancement picks
   (Deft Explorer, Scholar, College of Lore, Evocation Savant, Hunter's Prey).

## Phase 3 - tests and live matrix

- Unit/service tests for each item above in `tests/test_dnd_srd.py` and
  `tests/test_dnd_srd_play.py`, with multiclass coverage in
  `tests/test_dnd_multiclass.py`.
- Form tests for the new creation/advancement controls.
- UI test for the turn-resource panel.
- Live DeepSeek run: level-2/3 character, equipment package, species choice, Magic
  Initiate, versatile attack, armor training, condition, action/bonus/reaction, and
  always-prepared spell.
- Update `docs/DND_PLAYTEST.md` with the final matrix and any deliberate limitation.
