"""Recovering harm. Every way a character gets back what it lost in a stat goes through `recover`,
so healing, rests, paid treatment, and the GM's restore tool clamp harm the same way: a stat never
loses more harm than it has, and nothing restores more than the character took."""

from __future__ import annotations

from cygnusloom.services.events import Character


def recover(character: Character, stat: str, amount: int) -> int:
    """Reduce the harm a character has in `stat` by up to `amount`, never below none. Returns how
    much harm was actually restored, which is less than `amount` when less harm was there, and 0
    when there was none or `amount` is not positive."""
    if amount <= 0:
        return 0
    taken = character.harm.get(stat, 0)
    restored = min(amount, taken)
    if restored <= 0:
        return 0
    remaining = taken - restored
    if remaining:
        character.harm[stat] = remaining
    else:
        character.harm.pop(stat, None)
    return restored
