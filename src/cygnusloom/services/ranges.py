"""Attack reach, range bands, and line of sight on the encounter grid.

The rule set's combat `ranges` object turns these on. It gives the default melee reach in feet
and the encounter object ids that block sight. A weapon feature or statblock attack may then name
its own `reach` or its `ranges` bands, each with a farthest distance in feet. Positions come from
the encounter's cells and the grid's cell size, so a ruleset without a grid or a campaign with an
unplaced participant is left alone."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import ceil

from cygnusloom.content.character import Feature
from cygnusloom.content.library import CombatRules
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    CampaignState,
    Character,
    Encounter,
    EncounterMap,
    name_key,
)


@dataclass(frozen=True)
class AttackRange:
    """How far one attack reaches: its melee reach in feet, and the farthest distance of each
    ranged band, such as {"short": 30, "long": 60}. A band whose distance is None reaches the
    whole encounter. No bands means the attack is melee only, and a reach of None is one grid
    cell, so the attacker must be next to the target."""

    reach: int | None = None
    bands: Mapping[str, int | None] = field(default_factory=dict)

    @classmethod
    def melee(cls, combat: CombatRules) -> AttackRange:
        """The rule set's reach for an attack with no reach of its own."""
        rules = combat.ranges
        return cls(reach=rules.melee if rules is not None else None)

    @classmethod
    def for_feature(cls, feature: Feature | None, combat: CombatRules) -> AttackRange:
        """A player character's weapon feature: its own reach and ranged bands, with the rule
        set's default reach for its melee use."""
        default = cls.melee(combat)
        if feature is None:
            return default
        return cls(reach=feature.reach or default.reach, bands=dict(feature.ranges))

    @classmethod
    def for_attack(cls, character: Character, attack: str, combat: CombatRules) -> AttackRange:
        """One of an NPC's attacks, as its statblock or update_npc recorded it."""
        default = cls.melee(combat)
        return cls(
            reach=character.attack_reach.get(attack, default.reach),
            bands=dict(character.attack_ranges.get(attack, {})),
        )


@dataclass(frozen=True)
class AttackDistance:
    """Where a checked attack lands: in melee, or in a named range band with its hit penalty."""

    melee: bool
    band: str = ""
    cells: int = 0
    feet: int = 0
    penalty: int = 0

    def as_dict(self) -> dict[str, object]:
        """The result a tool returns, so the GM and the review can see what the engine measured."""
        return {
            "melee": self.melee,
            "band": self.band,
            "cells": self.cells,
            "feet": self.feet,
            "penalty": self.penalty,
        }


def _cell(encounter: Encounter, character: Character) -> tuple[int, int] | None:
    return encounter.cells.get(name_key(character.name))


def _map_named(state: CampaignState, encounter: Encounter) -> EncounterMap | None:
    return state.maps.get(name_key(encounter.map))


def check_attack(
    state: CampaignState,
    combat: CombatRules,
    *,
    cell_feet: int | None,
    attacker: Character,
    target: Character,
    profile: AttackRange,
    band: str = "",
) -> AttackDistance:
    """Measure an attack from the attacker's cell to the target's. An attack within the profile's
    reach is a melee attack, and a reach of None means one cell. One beyond it must fit a ranged
    band: the named band when `band` is given, else the nearest band that covers the distance. A
    band with no limit covers any distance. The band's hit penalty is returned so the caller can
    apply it. A rule set without `ranges`, a campaign without an encounter grid, and a participant
    with no cell are all unchecked.

    Line of sight is checked for every measured attack. An intermediate cell whose object the rule
    set lists as a sight blocker refuses the attack."""
    rules = combat.ranges
    encounter = state.encounter
    if rules is None or cell_feet is None or encounter is None:
        return AttackDistance(melee=True)
    start = _cell(encounter, attacker)
    end = _cell(encounter, target)
    if start is None or end is None:
        return AttackDistance(melee=True)
    cells = max(abs(end[0] - start[0]), abs(end[1] - start[1]))
    feet = cells * cell_feet
    reach_cells = 1 if profile.reach is None else max(1, ceil(profile.reach / cell_feet))
    if cells <= reach_cells:
        result = AttackDistance(melee=True, cells=cells, feet=feet)
    else:
        chosen = _band_for(profile, feet, band, attacker, target)
        result = AttackDistance(
            melee=False,
            band=chosen,
            cells=cells,
            feet=feet,
            penalty=combat.range_penalties.get(chosen, 0),
        )
    _require_sight(state, encounter, start, end, rules.sight_blocked_by)
    return result


def _band_for(
    profile: AttackRange, feet: int, band: str, attacker: Character, target: Character
) -> str:
    """The range band a ranged attack falls in, refusing when the target is beyond every band or
    the caller named a band that does not fit. A band with no limit fits any distance."""
    ordered = _bands_in_order(profile.bands)
    if not ordered:
        reach = "one cell" if profile.reach is None else f"{profile.reach} feet"
        raise InvalidInput(
            f"{target.name} is {feet} feet away, beyond {attacker.name}'s melee reach of "
            f"{reach}; move within reach with place_participant first"
        )
    if band:
        wanted = band.strip().casefold()
        if wanted not in profile.bands:
            raise InvalidInput(
                f"{attacker.name}'s attack has no range band {band!r}; {_band_text(profile.bands)}"
            )
        maximum = profile.bands[wanted]
        if maximum is not None and feet > maximum:
            farther = next((name for name, limit in ordered if limit is None or limit >= feet), "")
            hint = f"; use the {farther} band" if farther else "; move closer first"
            raise InvalidInput(
                f"{target.name} is {feet} feet away, beyond the {wanted} band of {maximum} feet"
                f"{hint}"
            )
        return wanted
    for name, maximum in ordered:
        if maximum is None or feet <= maximum:
            return name
    farthest_name, farthest = ordered[-1]
    raise InvalidInput(
        f"{target.name} is {feet} feet away, beyond {attacker.name}'s {farthest_name} range of "
        f"{farthest} feet; move closer with place_participant first"
    )


def _bands_in_order(bands: Mapping[str, int | None]) -> list[tuple[str, int | None]]:
    """The bands from shortest to longest, with a band that has no limit last."""
    return sorted(bands.items(), key=lambda item: (item[1] is None, item[1] or 0))


def _band_text(bands: Mapping[str, int | None]) -> str:
    return ", ".join(
        f"{name} up to {feet} feet" if feet is not None else f"{name} with no limit"
        for name, feet in _bands_in_order(bands)
    )


def _require_sight(
    state: CampaignState,
    encounter: Encounter,
    start: tuple[int, int],
    end: tuple[int, int],
    blockers: frozenset[str],
) -> None:
    """Refuse an attack when an intermediate cell holds a sight-blocking object."""
    if not blockers:
        return
    entry = _map_named(state, encounter)
    if entry is None:
        return
    for column, row in _line_cells(start, end):
        mark = entry.marks.get((column, row))
        if mark is not None and mark.object in blockers:
            name = mark.object.replace("-", " ")
            raise InvalidInput(
                f"the {name} at column {column}, row {row} blocks the line of sight; move to a "
                "clear line with place_participant first"
            )


def _line_cells(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """The cells a straight line crosses between two cells, excluding both ends. A diagonal step
    also passes between its two side cells, so either of those can block the line."""
    x, y = start
    x1, y1 = end
    dx, dy = abs(x1 - x), abs(y1 - y)
    step_x = 1 if x < x1 else -1
    step_y = 1 if y < y1 else -1
    error = dx - dy
    cells: list[tuple[int, int]] = []
    while (x, y) != (x1, y1):
        double = error * 2
        diagonal = double > -dy and double < dx
        if diagonal:
            # The line squeezes between the two side cells; a blocker on either side of the
            # corner blocks it.
            cells.append((x + step_x, y))
            cells.append((x, y + step_y))
            x += step_x
            y += step_y
            error += dx - dy
        elif double > -dy:
            x += step_x
            error -= dy
        else:
            y += step_y
            error += dx
        if (x, y) != (x1, y1):
            cells.append((x, y))
    return list(dict.fromkeys(cell for cell in cells if cell not in (start, end)))
