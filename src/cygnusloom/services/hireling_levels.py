"""Keeping hirelings at their employer's level.

A hireling does not gain experience on its own. When it joins the company, and whenever its
employer gains a level, it is given enough experience to reach the employer's level. The player
still makes every level-up choice on the advancement page, so the hireling catches up through the
same screens as a player character.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cygnusloom.content.library import CounterRules, LevelUpRules
from cygnusloom.services.common import session_scope
from cygnusloom.services.events import PC_KIND, Character, load_state, name_key


def sync_employer(world: Any, employer_name: str) -> list[str]:
    """Give every living hireling of one employer enough experience for the employer's level.

    The hireling's level is not raised here: the player raises it on the advancement page, where
    the level's automatic gains and choices are applied. Returns the hirelings whose experience
    was raised.
    """
    rules = next(iter(world.level_ups.values()), None)
    if rules is None:
        return []
    with session_scope(world.engine) as db:
        state = load_state(db, world.campaign_id)
        employer = state.characters.get(name_key(employer_name))
        if (
            employer is None
            or employer.kind != PC_KIND
            or employer.hireling
            or employer.out_of_play
        ):
            return []
        target = _level(employer, rules, world.counters)
        if target is None:
            return []
        track = _track(state.options, rules)
        if track is None:
            return []
        target_level = min(target, rules.max_level, len(track))
        if target_level <= 1:
            return []
        target_xp = track[target_level - 1]
        raised: list[tuple[str, int]] = []
        for character in state.characters.values():
            if not character.hireling or name_key(character.employer) != name_key(employer.name):
                continue
            if character.out_of_play:
                continue
            if rules.template_kinds and not any(
                world.character.templates.get(template_id) is not None
                and world.character.templates[template_id].kind in rules.template_kinds
                for template_id in character.templates.values()
            ):
                # A legacy hireling with no class cannot gain levels; leave it alone.
                continue
            level = _level(character, rules, world.counters)
            if level is None or level >= target:
                continue
            xp = _counter(character, rules.xp_counter, world.counters)
            if xp < target_xp:
                raised.append((character.name, target_xp - xp))
    for name, amount in raised:
        world.adjust_counter(name, rules.xp_counter, amount)
    return [name for name, _ in raised]


def _level(
    character: Character,
    rules: LevelUpRules,
    counters: Mapping[str, CounterRules],
) -> int | None:
    value = counters.get(rules.level_counter)
    if value is None:
        return None
    return int(character.counters.get(rules.level_counter, value.initial))


def _counter(
    character: Character,
    counter: str,
    counters: Mapping[str, CounterRules],
) -> int:
    value = counters.get(counter)
    return int(character.counters.get(counter, value.initial if value is not None else 0))


def _track(options: Mapping[str, Any], rules: LevelUpRules) -> tuple[int, ...] | None:
    rate = options.get(rules.rate_option) or next(iter(rules.tracks), None)
    if rate is None:
        return None
    return rules.tracks.get(rate) or next(iter(rules.tracks.values()), None)
