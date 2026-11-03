"""Attack modifiers and flags. A modifier is a named change to the dice an attack rolls, such as
impaired or enhanced; the rule set lists each one with its dice, and "normal" always means the
attack's own dice. A flag is an engine behavior an attack has, such as hitting every target in its
area. Several attacks on one target are resolved by keeping the highest roll."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from cygnusforge.services.common import InvalidInput

NORMAL = "normal"
# Area attack: hits each target in the attack's area, rolling separately for each.
BLAST = "blast"
# Defense-bypassing attack: skips the target's armor.
IGNORES_ARMOR = "ignores_armor"


@dataclass(frozen=True)
class AttackFlag:
    """A named thing an attack does beyond rolling its dice. Combat asks a flag's behavior, never
    its name, so a newly registered flag that reuses an existing behavior needs no combat edit."""

    name: str
    # The attack hits every target in its area, rolling separately for each.
    hits_all_targets: bool = False
    # The attack skips the target's armor.
    ignores_armor: bool = False


# The attack flags the engine can run, by name, in registration order. Combat reads the
# descriptors; the content loader and the world service check a rule set's names against this.
ATTACK_FLAG_REGISTRY: dict[str, AttackFlag] = {}


def register_attack_flag(flag: AttackFlag) -> AttackFlag:
    """Add a flag. Registering a name twice is a programming error, not a content error."""
    if not flag.name or flag.name in ATTACK_FLAG_REGISTRY:
        raise ValueError(f"attack flag {flag.name!r} is already registered")
    ATTACK_FLAG_REGISTRY[flag.name] = flag
    return flag


class _FlagNames(Sequence[str]):
    """The registered names as a tuple-like sequence. It reads the registry on every use, so a
    flag registered after import is listed and accepted."""

    def __getitem__(self, index: int | slice) -> str | tuple[str, ...]:
        return tuple(ATTACK_FLAG_REGISTRY)[index]

    def __len__(self) -> int:
        return len(ATTACK_FLAG_REGISTRY)

    def __contains__(self, name: object) -> bool:
        return name in ATTACK_FLAG_REGISTRY

    def __iter__(self) -> Iterator[str]:
        return iter(ATTACK_FLAG_REGISTRY)


register_attack_flag(AttackFlag(BLAST, hits_all_targets=True))
register_attack_flag(AttackFlag(IGNORES_ARMOR, ignores_armor=True))
# The registered flag names, for code that lists or checks them rather than reading a descriptor.
ATTACK_FLAGS: Sequence[str] = _FlagNames()


@dataclass(frozen=True)
class AttackFlags:
    """The behaviors one attack's flags give it, looked up from the registry. Combat asks this
    instead of comparing flag names."""

    flags: tuple[AttackFlag, ...] = ()

    @classmethod
    def of(cls, names: Iterable[str]) -> AttackFlags:
        """The descriptors for an attack's flag names. A name with no descriptor is skipped; the
        loader refuses unknown names, so only a stale stored event could carry one."""
        return cls(
            tuple(
                ATTACK_FLAG_REGISTRY[name]
                for name in dict.fromkeys(names)
                if name in ATTACK_FLAG_REGISTRY
            )
        )

    @property
    def hits_all_targets(self) -> bool:
        return any(flag.hits_all_targets for flag in self.flags)

    @property
    def ignores_armor(self) -> bool:
        return any(flag.ignores_armor for flag in self.flags)


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
