"""Checks a spell's named targets against its range and radial area before a cast spends anything.

The spell text states the range and any radial area, such as "Range: 60 feet" and "5-foot-radius
Sphere". When a cast names its targets, every one is measured on the encounter grid before the
slot, action, or concentration is spent, so a target that cannot be reached refuses the whole cast
instead of leaving it half applied. A campaign with no encounter grid, or a caster or target that
is not placed, is left unchecked because there is nothing to measure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil

from cygnusloom.content.character import CharacterSpec, Feature
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import CampaignState, Character, Encounter, name_key

_RANGE_FEET = re.compile(r"Range:\s*(\d+)\s*(?:feet|foot)\b", re.IGNORECASE)
_RANGE_TOUCH = re.compile(r"Range:\s*touch\b", re.IGNORECASE)
_RANGE_SELF = re.compile(r"Range:\s*self\b", re.IGNORECASE)
# Only a radial area is measured: a sphere, emanation, or cylinder is a circle around its centre,
# while a cone, cube, or line depends on an orientation the text does not give.
_AREA = re.compile(r"(\d+)-foot[-\s](?:radius\s+)?(Sphere|Emanation|Cylinder)\b", re.IGNORECASE)
_CELL = re.compile(
    r"\s*(?:(?:column|col\.?)\s*)?(\d+)\s*,\s*(?:(?:row)\s*)?(\d+)\s*", re.IGNORECASE
)


@dataclass(frozen=True)
class SpellReach:
    """How far a spell reaches: a distance in feet, by touch, or self. All three unset means the
    text gives no range this engine can measure."""

    feet: int | None = None
    touch: bool = False
    self_range: bool = False


@dataclass(frozen=True)
class SpellArea:
    """A radial area: its shape and its radius in feet."""

    shape: str
    radius_feet: int


def spell_reach(description: str) -> SpellReach:
    if _RANGE_TOUCH.search(description):
        return SpellReach(touch=True)
    if _RANGE_SELF.search(description):
        return SpellReach(self_range=True)
    match = _RANGE_FEET.search(description)
    if match is not None:
        return SpellReach(feet=int(match.group(1)))
    return SpellReach()


def spell_area(description: str) -> SpellArea | None:
    match = _AREA.search(description)
    if match is None:
        return None
    return SpellArea(shape=match.group(2).casefold(), radius_feet=int(match.group(1)))


def find_spell(spec: CharacterSpec, spell: str) -> Feature | None:
    """The spell feature named by id or name, or None when the rule set has no such spell."""
    wanted = spell.strip().casefold()
    wanted_id = wanted if wanted.startswith("spell-") else f"spell-{wanted}"
    matches = [
        feature
        for feature in spec.features.values()
        if feature.feature_type == "spell"
        and (feature.id.casefold() in (wanted, wanted_id) or feature.name.casefold() == wanted)
    ]
    return matches[0] if len(matches) == 1 else None


def check_targets(
    state: CampaignState,
    cell_feet: int | None,
    caster: Character,
    spell: Feature,
    targets: Sequence[str],
    center: str = "",
) -> None:
    """Refuse the whole cast when a named target is out of the spell's range or outside its
    radial area. Nothing is spent here; the caller runs this before a slot or action.

    The centre of an area defaults to the caster for a self-range spell, else to the first target
    that is placed. `center` may name a character, "caster", or a cell as "column,row". A target
    or caster with no cell cannot be measured and is left alone."""
    names = list(dict.fromkeys(str(target).strip() for target in targets if str(target).strip()))
    if not names:
        return
    found: list[Character] = []
    for name in names:
        character = state.characters.get(name_key(name))
        if character is None:
            raise InvalidInput(f"there is no character named {name!r}")
        found.append(character)
    encounter = state.encounter
    if encounter is None or cell_feet is None:
        return
    start = encounter.cells.get(name_key(caster.name))
    if start is None:
        return
    reach = spell_reach(spell.description)
    area = spell_area(spell.description)
    if reach.touch:
        for target in found:
            end = encounter.cells.get(name_key(target.name))
            if end is None:
                continue
            cells = _cells(start, end)
            if cells > 1:
                raise InvalidInput(
                    f"{target.name} is {cells * cell_feet} feet away; {spell.name} reaches only "
                    f"by touch, so move within one cell first or leave {target.name} out of "
                    "targets"
                )
    elif reach.feet is not None:
        limit = max(1, ceil(reach.feet / cell_feet))
        for target in found:
            end = encounter.cells.get(name_key(target.name))
            if end is None:
                continue
            cells = _cells(start, end)
            if cells > limit:
                raise InvalidInput(
                    f"{target.name} is {cells * cell_feet} feet away, beyond the {reach.feet}-foot "
                    f"range of {spell.name}; move within range or leave {target.name} out of "
                    "targets"
                )
    if area is None:
        return
    middle = _centre_cell(state, encounter, start, found, center, reach)
    if middle is None:
        return
    if reach.feet is not None:
        distance = _cells(start, middle) * cell_feet
        if distance > reach.feet:
            raise InvalidInput(
                f"the centre of {spell.name} at column {middle[0]}, row {middle[1]} is "
                f"{distance} feet away, beyond its {reach.feet}-foot range; name a centre "
                "within range"
            )
    radius = max(1, ceil(area.radius_feet / cell_feet))
    for target in found:
        end = encounter.cells.get(name_key(target.name))
        if end is None:
            continue
        cells = _cells(middle, end)
        if cells > radius:
            raise InvalidInput(
                f"{target.name} is {cells * cell_feet} feet from the centre, beyond the "
                f"{area.radius_feet}-foot radius of {spell.name}; centre it where it catches "
                f"every target, or leave {target.name} out of targets"
            )


def _centre_cell(
    state: CampaignState,
    encounter: Encounter,
    start: tuple[int, int],
    targets: Sequence[Character],
    center: str,
    reach: SpellReach,
) -> tuple[int, int] | None:
    text = center.strip()
    if not text:
        if reach.self_range:
            return start
        for target in targets:
            cell = encounter.cells.get(name_key(target.name))
            if cell is not None:
                return cell
        return None
    if text.casefold() in ("caster", "self", "me"):
        return start
    match = _CELL.fullmatch(text)
    if match is not None:
        cell = (int(match.group(1)), int(match.group(2)))
        entry = state.maps.get(name_key(encounter.map))
        if entry is not None and (cell[0] >= entry.columns or cell[1] >= entry.rows):
            raise InvalidInput(
                f"the centre at column {cell[0]}, row {cell[1]} is off the encounter map"
            )
        return cell
    other = state.characters.get(name_key(text))
    if other is None:
        raise InvalidInput(
            f"there is no character or cell named {center!r} for the centre; use a character's "
            'name, "caster", or a cell as column,row'
        )
    cell = encounter.cells.get(name_key(other.name))
    if cell is None:
        raise InvalidInput(
            f"{other.name} is not placed on the encounter map, so the centre cannot be measured; "
            "place them first or name a cell as column,row"
        )
    return cell


def _cells(start: tuple[int, int], end: tuple[int, int]) -> int:
    """Grid distance in cells, measured the way an encounter grid counts a diagonal step."""
    return max(abs(end[0] - start[0]), abs(end[1] - start[1]))
