"""The peoples a rule set or world offers the GM, and the names that reach the record.

A people is a content `species` definition. An NPC records the singular name, while a place
records its majority people in the plural, as in "mostly dwarves". A people may be named by its
id, name, plural, or adjective, so a GM may write "dwarf", "Dwarves", or "dwarven" for the same
entry. Core declares no peoples: only a rule set or a world that names its peoples offers them,
and the GM records none when neither does.
"""

from __future__ import annotations

from collections.abc import Sequence

from cygnusloom.content.library import Species
from cygnusloom.services.common import InvalidInput

# A place with no single majority people. It is recorded as this word rather than a species.
MIXED_PEOPLES = "mixed"


def _key(value: str) -> str:
    return " ".join(value.split()).casefold()


def species_by_name(species: Sequence[Species], query: str) -> Species | None:
    """The people a name, plural, adjective, or id refers to, or None."""
    key = _key(query)
    for entry in species:
        if key in {
            entry.id.casefold(),
            _key(entry.name),
            _key(entry.plural),
            _key(entry.adjective),
        }:
            return entry
    return None


def species_choices(species: Sequence[Species]) -> str:
    """The peoples as a sentence reads them, such as "Human, Dwarf, or Elf"."""
    names = [entry.name for entry in species]
    if not names:
        return "none"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])}, or {names[-1]}"


def npc_species(species: Sequence[Species], value: str) -> str:
    """The singular name an NPC records for a people. A rule set and world that declare no
    peoples offer none, so there is nothing to record."""
    text = " ".join(value.split())
    if not species:
        raise InvalidInput(
            "this rule set and world declare no peoples, so leave species out; only a rule set "
            "or world that names its peoples can give an NPC one"
        )
    found = species_by_name(species, text)
    if found is None:
        raise InvalidInput(
            f"{value!r} is not one of the peoples here. The peoples are "
            f"{species_choices(species)}; leave species out for a creature that is not one of "
            "them."
        )
    return found.name


def place_majority(species: Sequence[Species], value: str) -> str:
    """The plural a place records for its majority people, or "mixed" for a place with no single
    majority. An empty value clears the record. A rule set and world that declare no peoples
    offer none, so there is nothing to record."""
    text = " ".join(value.split())
    if not text:
        return ""
    if not species:
        raise InvalidInput(
            "this rule set and world declare no peoples, so leave majority_species out; only a "
            "rule set or world that names its peoples can give a place one"
        )
    if _key(text) == MIXED_PEOPLES:
        return MIXED_PEOPLES
    found = species_by_name(species, text)
    if found is None:
        raise InvalidInput(
            f"{value!r} is not one of the peoples here. The peoples are "
            f'{species_choices(species)}; pass "mixed" for a place with no single majority, '
            "or leave majority_species out."
        )
    return found.plural


def majority_species_name(species: Sequence[Species], majority: str) -> str:
    """The singular name of a place's recorded majority people, or "" when it has none or is
    mixed. An NPC created without a species at such a place takes this people."""
    if not species or not majority or _key(majority) == MIXED_PEOPLES:
        return ""
    found = species_by_name(species, majority)
    return found.name if found is not None else ""
