"""Combat: hits, armor, the harm they cause, and the saves they force.

The rule set's combat module decides what a save means and how initiative is ordered. Each
aggregate lives in its own module; this package re-exports the whole combat API.

Like the rest of the world service, each function loads the campaign's state, validates the
change, records its events, and commits."""

from cygnusloom.services.combat.armor import (  # noqa: F401
    armor_after,
    equipped_defense,
    recalculate_defense,
    set_armor,
    set_defense_equipment,
)
from cygnusloom.services.combat.attack import (  # noqa: F401
    PlannedAttack,
    _attack_dice,
    _attack_named,
    _attack_shock,
    _attribute_modifier,
    _blast,
    _blast_attack,
    _condition_attack_modifier,
    _level_damage_bonus,
    _player_bonuses,
    _player_character,
    _shield_round,
    _stat_bonus,
    _strike,
    _strike_each,
    _weapon_name,
    _weapon_skill,
    attack,
    attack_dice,
    player_attack,
    player_attack_check,
    resolve_attacks,
)
from cygnusloom.services.combat.constants import (  # noqa: F401
    MAX_CAUSE_TEXT,
    MAX_DAMAGE,
    MAX_RECOVERY_TEXT,
    NO_UNSKILLED_PENALTY,
    UNCAPPED_ARMOR,
)
from cygnusloom.services.combat.damage import (  # noqa: F401
    Damage,
    WeaponBonuses,
    _defeat_after_harm,
    _end_if_one_side_left,
    _harm_stat,
    _injury_from_hit,
    _living,
    _require_impaired,
    apply_damage,
    harm_character,
    heal_character,
    recover_character,
    surrender,
)
from cygnusloom.services.combat.debug import (  # noqa: F401
    add_debug_currency,
    set_debug_stat,
    set_invincibility,
)
from cygnusloom.services.combat.initiative import (  # noqa: F401
    _no_initiative_pending,
    player_initiative,
    resolve_group_break,
)
from cygnusloom.services.combat.save import (  # noqa: F401
    _required_rng,
    _save,
    player_save,
    resolve_save,
)
from cygnusloom.services.events import current  # noqa: F401
