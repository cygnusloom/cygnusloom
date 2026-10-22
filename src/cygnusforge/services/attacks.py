"""Attack modifiers: named changes to the dice an attack rolls, such as impaired or enhanced. The
rule set lists each modifier with its dice. "normal" is always available and means the attack's own
dice. Several attacks on one target are resolved by keeping the highest roll."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cygnusforge.services.common import InvalidInput

NORMAL = "normal"


def modifier_name(modifier: object) -> str:
    """The modifier as the rule set spells it, or "" for an ordinary attack."""
    if modifier is None:
        return ""
    if not isinstance(modifier, str):
        raise InvalidInput("modifier must be text such as 'impaired'")
    name = modifier.strip().casefold()
    return "" if name in ("", NORMAL) else name


def modifier_dice(modifiers: Mapping[str, str], modifier: object) -> str | None:
    """The dice a modifier rolls instead of the attack's own, or None for an ordinary attack."""
    name = modifier_name(modifier)
    if not name:
        return None
    if name not in modifiers:
        options = ", ".join([NORMAL, *sorted(modifiers)])
        raise InvalidInput(f"unknown attack modifier {modifier!r}; modifiers: {options}")
    return modifiers[name]


def highest_index(rolls: Sequence[int]) -> int:
    """The position of the highest roll. On a tie, the earlier roll counts."""
    return max(range(len(rolls)), key=lambda index: (rolls[index], -index))


def rolls_text(rolls: Sequence[tuple[str, str, str, int]]) -> str:
    """Each attack's roll as one line, for the record. Each tuple is the attacker, the attack, the
    modifier ("" if none), and the roll."""
    parts = []
    for attacker, attack, modifier, roll in rolls:
        label = f"{attacker}'s {attack}" + (f" ({modifier})" if modifier else "")
        parts.append(f"{label} rolled {roll}")
    return "Rolls: " + "; ".join(parts) + "; the highest hits."
