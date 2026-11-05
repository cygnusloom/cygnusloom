"""The player's level-up and Advancement page.

The rule set's JSON says which pools a level grants, what each pick costs, and which campaign
option names its experience track. The rule set's Python hooks apply the automatic level gains
and the mechanical change of each pick. This module turns that data into a page and validates
the player's submission before any change is recorded.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from cygnusloom.content.library import (
    LevelUpPick,
    LevelUpRules,
    RuleSet,
    WorldContent,
)
from cygnusloom.rules.context import ToolContext
from cygnusloom.services.common import InvalidInput, session_scope
from cygnusloom.services.events import PC_KIND, Character, load_state, name_key
from cygnusloom.services.world import WorldTools

# What the page shows when a level or a pool of picks is ready.
LEVEL = "level"
PICKS = "picks"
OPTION = "options"


@dataclass(frozen=True)
class AdvancementPage:
    """What the advancement page shows, and what a submission means."""

    kind: str
    character: str
    label: str
    # The level-up rules in play, if the rule set has any.
    level_up: LevelUpRules | None = None
    # The summary the rule set's hook returned: the level, XP, and automatic gains.
    summary: Mapping[str, Any] | None = None
    # The picks a player may spend now, with their options and costs.
    picks: tuple[Mapping[str, Any], ...] = ()
    # The rule set's Advancement options, when its counter is full.
    options: tuple[Mapping[str, Any], ...] = ()
    # The Advancement rules whose counter is available.
    advancement: Any = None
    # Why the page is blocked, if it is.
    blocked: str = ""


def level_ready(
    state: Any,
    pc: Character,
    rules: LevelUpRules,
    counters: Mapping[str, Any],
) -> bool:
    """Whether the character's XP has reached the next level's threshold."""
    level = pc.counters.get(rules.level_counter, counters[rules.level_counter].initial)
    if not isinstance(level, int) or level >= rules.max_level:
        return False
    rate = state.options.get(rules.rate_option) or next(iter(rules.tracks))
    track = rules.tracks.get(rate)
    if track is None:
        rate = next(iter(rules.tracks))
        track = rules.tracks[rate]
    return pc.counters.get(rules.xp_counter, counters[rules.xp_counter].initial) >= track[level]


def advancement_due(state: Any, rule_set: RuleSet) -> bool:
    """Whether the player must choose an advancement before the game continues.

    A level whose XP threshold is reached pauses play. So does a point of an Advancement pool,
    because taking it is the player's choice. Unspent skill points and similar optional training
    do not pause play.
    """
    pc = state.pc()
    if pc is None or pc.out_of_play or pc.kind != PC_KIND:
        return False
    for rules in rule_set.level_ups.values():
        if level_ready(state, pc, rules, rule_set.counters):
            return True
    for advancement in rule_set.advancements.values():
        counter = rule_set.counters[advancement.counter]
        if pc.counters.get(advancement.counter, counter.initial) > 0:
            return True
    return False


def page_view(
    engine: Engine,
    campaign_id: int,
    rule_set: RuleSet,
    world: WorldContent,
) -> AdvancementPage | None:
    """The page for the player character's next advancement, or None when none is available."""
    with session_scope(engine) as db:
        state = load_state(db, campaign_id)
    pc = state.pc()
    if pc is None or pc.out_of_play or pc.kind != PC_KIND:
        return None
    level_rules = next(iter(rule_set.level_ups.values()), None)
    if level_rules is not None:
        ctx = tool_context(engine, campaign_id, rule_set, world)
        if level_ready(state, pc, level_rules, rule_set.counters):
            summary = level_rules.summary(pc.name, ctx) if level_rules.summary else {}
            picks = pick_views(pc, rule_set, level_rules, summary, ctx)
            return AdvancementPage(
                kind=LEVEL,
                character=pc.name,
                label=level_rules.label,
                level_up=level_rules,
                summary=summary,
                picks=picks,
            )
        picks = pick_views(pc, rule_set, level_rules, None, ctx)
        if picks:
            return AdvancementPage(
                kind=PICKS,
                character=pc.name,
                label="Spend advancement picks",
                level_up=level_rules,
                picks=picks,
            )
    for advancement in rule_set.advancements.values():
        counter = rule_set.counters[advancement.counter]
        if pc.counters.get(advancement.counter, counter.initial) > 0:
            return AdvancementPage(
                kind=OPTION,
                character=pc.name,
                label=advancement.label,
                advancement=advancement,
                options=option_views(pc, rule_set, advancement),
            )
    return None


def pick_views(
    pc: Character,
    rule_set: RuleSet,
    level_rules: LevelUpRules,
    summary: Mapping[str, Any] | None,
    ctx: ToolContext | None,
) -> tuple[Mapping[str, Any], ...]:
    """The pools a level-up or a training session lets the player spend, with their choices."""
    grants = summary.get("grants", {}) if summary else {}
    views: list[Mapping[str, Any]] = []
    for pick in level_rules.picks.values():
        counter = rule_set.counters[pick.counter]
        marked = pc.counters.get(pick.counter, counter.initial)
        available = marked + int(grants.get(pick.counter, 0))
        if available <= 0:
            continue
        if pick.kind == "skill":
            views.append(_skill_view(pc, rule_set, pick, available, summary))
        elif pick.kind == "stat":
            views.append(_stat_view(pc, rule_set, pick, available, summary))
        elif pick.kind == "feature" and ctx is not None:
            level = _new_level(pc, rule_set, summary)
            options = pick.options(pc.name, pick, ctx, level) if pick.options else []
            views.append(
                {
                    "id": pick.id,
                    "label": pick.label,
                    "kind": pick.kind,
                    "available": available,
                    "options": options,
                }
            )
    return tuple(views)


def _skill_view(
    pc: Character,
    rule_set: RuleSet,
    pick: LevelUpPick,
    available: int,
    summary: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Every skill's current level and the levels it could be raised to."""
    spec = rule_set.character
    level = _new_level(pc, rule_set, summary)
    skills = []
    for skill in spec.skills.values():
        current = spec.skill_level(pc.skills, skill.id)
        targets = []
        for target in sorted(pick.costs):
            if target <= current:
                continue
            cost = _skill_cost(pick, current, target)
            minimum = max(
                (pick.minimum_levels.get(step, 0) for step in range(current + 1, target + 1)),
                default=0,
            )
            if cost is None or level < minimum:
                continue
            targets.append({"level": target, "cost": cost, "minimum_level": minimum})
        if targets:
            skills.append(
                {"id": skill.id, "name": skill.name, "current": current, "targets": targets}
            )
    return {
        "id": pick.id,
        "label": pick.label,
        "kind": pick.kind,
        "available": available,
        "skills": skills,
    }


def _stat_view(
    pc: Character,
    rule_set: RuleSet,
    pick: LevelUpPick,
    available: int,
    summary: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """The attributes a skill point may raise, and the next boost's cost."""
    spec = rule_set.character
    level = _new_level(pc, rule_set, summary)
    bought = 0
    if pick.limit_counter:
        counter = rule_set.counters[pick.limit_counter]
        bought = pc.counters.get(pick.limit_counter, counter.initial)
    next_step = bought + 1
    cost = pick.costs.get(next_step)
    minimum = pick.minimum_levels.get(next_step)
    stats = []
    for stat in spec.stats.values():
        if stat.derived is not None:
            continue
        if pick.stat_group and stat.swap_group != pick.stat_group:
            continue
        value = pc.stats.get(stat.id, stat.low)
        if pick.maximum is not None and value >= pick.maximum:
            continue
        stats.append({"id": stat.id, "name": stat.name, "value": value})
    return {
        "id": pick.id,
        "label": pick.label,
        "kind": pick.kind,
        "available": available,
        "next_cost": cost,
        "next_minimum": minimum,
        "can_raise": cost is not None and (minimum is None or level >= minimum),
        "next_step": next_step,
        "stats": stats,
    }


def option_views(
    pc: Character, rule_set: RuleSet, advancement: Any
) -> tuple[Mapping[str, Any], ...]:
    """The Advancement options the player may take, with the stat or feature each asks for."""
    spec = rule_set.character
    views = []
    for option in advancement.options.values():
        taken = pc.advancements_taken.get(option.value, 0)
        if option.limit is not None and taken >= option.limit:
            continue
        stats = []
        if option.picks:
            stats = [
                {"id": stat.id, "name": stat.name}
                for stat in spec.stats.values()
                if stat.derived is None
            ]
        features: list[dict[str, str]] = []
        if option.learn is not None:
            learn = option.learn
            tag = learn.tag or (pc.fields.get(learn.tag_field, "") if learn.tag_field else "")
            restrict = bool(learn.tag or learn.tag_field)
            features = [
                {"id": feature.id, "name": feature.name}
                for feature in spec.features.values()
                if feature.feature_type == learn.feature_type
                and (not restrict or feature.tag == tag)
                and feature.id not in learn.exclude
                and (feature.repeatable or feature.id not in pc.features)
            ]
        views.append(
            {
                "value": option.value,
                "label": option.label,
                "text": option.text,
                "picks": option.picks,
                "stats": stats,
                "features": features,
            }
        )
    return tuple(views)


def _new_level(pc: Character, rule_set: RuleSet, summary: Mapping[str, Any] | None) -> int:
    """The character's level after the pending level-up, or their current level."""
    if summary is not None:
        return int(summary.get("new_level", pc.counters.get("level", 1)))
    return int(pc.counters.get("level", 1))


def apply_page(
    engine: Engine,
    campaign_id: int,
    rule_set: RuleSet,
    world: WorldContent,
    form: Mapping[str, Any],
) -> None:
    """Apply the player's level-up or training choices after validating every one."""
    view = page_view(engine, campaign_id, rule_set, world)
    if view is None:
        raise InvalidInput("there is no advancement to take")
    ctx = tool_context(engine, campaign_id, rule_set, world)
    if view.kind == LEVEL and view.level_up is not None:
        _apply_level(view, engine, campaign_id, rule_set, ctx, form)
    elif view.kind == PICKS and view.level_up is not None:
        _apply_picks(view, engine, campaign_id, rule_set, ctx, form)
    elif view.kind == OPTION:
        _apply_option(view, ctx, form)
    else:
        raise InvalidInput("there is no advancement to take")


def _apply_level(
    view: AdvancementPage,
    engine: Engine,
    campaign_id: int,
    rule_set: RuleSet,
    ctx: ToolContext,
    form: Mapping[str, Any],
) -> None:
    assert view.level_up is not None
    # Check every pick against the pools the new level will grant before changing anything.
    _plan_picks(view, engine, campaign_id, rule_set, ctx, form)
    view.level_up.apply_level(view.character, ctx)
    _apply_picks(view, engine, campaign_id, rule_set, ctx, form)


def _apply_picks(
    view: AdvancementPage,
    engine: Engine,
    campaign_id: int,
    rule_set: RuleSet,
    ctx: ToolContext,
    form: Mapping[str, Any],
) -> None:
    changes = _plan_picks(view, engine, campaign_id, rule_set, ctx, form)
    for pick, value in changes:
        if pick.kind == "feature":
            for choice in value:
                pick.apply(view.character, pick, choice, ctx)
        else:
            pick.apply(view.character, pick, value, ctx)


def _plan_picks(
    view: AdvancementPage,
    engine: Engine,
    campaign_id: int,
    rule_set: RuleSet,
    ctx: ToolContext,
    form: Mapping[str, Any],
) -> list[tuple[LevelUpPick, Any]]:
    """Validate every submitted pick against the current state and return what to apply."""
    pc = character_from(engine, campaign_id, view.character)
    if pc is None:
        raise InvalidInput(f"there is no character named {view.character!r}")
    assert view.level_up is not None
    summary = view.summary or {}
    new_level = int(summary.get("new_level", 0)) if summary else 0
    current = int(pc.counters.get(view.level_up.level_counter, 1))
    # A level-up grants its pools once. After the level is applied, the counters already hold
    # them, so adding the summary's grants again would double the player's picks.
    grants = summary.get("grants", {}) if summary and current < new_level else {}
    level = _new_level(pc, rule_set, view.summary)
    changes: list[tuple[LevelUpPick, Any]] = []
    spent: dict[str, int] = {}
    for pick in view.level_up.picks.values():
        counter = rule_set.counters[pick.counter]
        available = pc.counters.get(pick.counter, counter.initial) + int(
            grants.get(pick.counter, 0)
        )
        if pick.kind == "skill":
            picked = _skill_changes(pc, rule_set, pick, form, level)
            cost = sum(change["cost"] for change in picked)
            if cost > available:
                raise InvalidInput(f"{pick.label} cost {cost} points; {available} are available")
            spent[pick.counter] = spent.get(pick.counter, 0) + cost
            changes.append((pick, {change["skill"]: change["level"] for change in picked}))
        elif pick.kind == "stat":
            remaining = available - spent.get(pick.counter, 0)
            picked = _stat_boosts(pc, rule_set, pick, form, level, remaining)
            cost = sum(boost["cost"] for boost in picked["boosts"])
            if cost > remaining:
                raise InvalidInput(f"{pick.label} cost {cost} points; {remaining} are left")
            spent[pick.counter] = spent.get(pick.counter, 0) + cost
            changes.append((pick, picked["counts"]))
        elif pick.kind == "feature":
            choices = _feature_choices(pick, form)
            if len(choices) > available:
                raise InvalidInput(
                    f"{pick.label} offers {available} pick"
                    f"{'' if available == 1 else 's'}; {len(choices)} were chosen"
                )
            options = (
                {option["id"]: option for option in pick.options(pc.name, pick, ctx, level)}
                if pick.options
                else {}
            )
            seen: set[str] = set()
            for choice in choices:
                if choice not in options:
                    raise InvalidInput(f"{choice!r} is not an available {pick.label.lower()}")
                if choice in seen and not options[choice].get("repeatable"):
                    raise InvalidInput(f"{options[choice]['name']} was chosen twice")
                seen.add(choice)
            changes.append((pick, choices))
    return changes


def _skill_changes(
    pc: Character,
    rule_set: RuleSet,
    pick: LevelUpPick,
    form: Mapping[str, Any],
    level: int,
) -> list[dict[str, Any]]:
    spec = rule_set.character
    raised: dict[str, int] = {}
    for skill in spec.skills.values():
        raw = form.get(f"skill-{pick.id}-{skill.id}", "")
        if raw in ("", None):
            continue
        try:
            target = int(raw)
        except (TypeError, ValueError):
            raise InvalidInput(f"Choose a level for {skill.name}") from None
        current = spec.skill_level(pc.skills, skill.id)
        if target <= current:
            continue
        if target not in pick.costs:
            raise InvalidInput(f"{skill.name} cannot be raised to level {target}")
        minimum = max(
            (pick.minimum_levels.get(step, 0) for step in range(current + 1, target + 1)),
            default=0,
        )
        if level < minimum:
            raise InvalidInput(
                f"{skill.name} level {target} needs character level {minimum}; "
                f"{pc.name} will be level {level}"
            )
        if _skill_cost(pick, current, target) is None:
            raise InvalidInput(
                f"{skill.name} cannot be raised straight to level {target}; "
                "raise it one level at a time"
            )
        raised[skill.id] = target
    return [
        {
            "skill": skill,
            "level": target,
            "cost": _skill_cost(pick, spec.skill_level(pc.skills, skill), target) or 0,
        }
        for skill, target in raised.items()
    ]


def _skill_cost(pick: LevelUpPick, current: int, target: int) -> int | None:
    """The total cost to raise a skill from `current` to `target`, or None when a step has none."""
    total = 0
    for step in range(current + 1, target + 1):
        cost = pick.costs.get(step)
        if cost is None:
            return None
        total += cost
    return total


def _stat_boosts(
    pc: Character,
    rule_set: RuleSet,
    pick: LevelUpPick,
    form: Mapping[str, Any],
    level: int,
    available: int,
) -> dict[str, Any]:
    spec = rule_set.character
    counter = rule_set.counters[pick.limit_counter] if pick.limit_counter else None
    bought = pc.counters.get(pick.limit_counter, counter.initial) if counter else 0
    counts: dict[str, int] = {}
    boosts: list[dict[str, int]] = []
    for stat in spec.stats.values():
        if stat.derived is not None:
            continue
        if pick.stat_group and stat.swap_group != pick.stat_group:
            continue
        raw = form.get(f"stat-{pick.id}-{stat.id}", "")
        if raw in ("", None):
            continue
        try:
            count = int(raw)
        except (TypeError, ValueError):
            raise InvalidInput(f"Choose how many boosts to buy for {stat.name}") from None
        if count < 0:
            raise InvalidInput(f"Choose how many boosts to buy for {stat.name}")
        if count == 0:
            continue
        value = pc.stats.get(stat.id, stat.low)
        if pick.maximum is not None and value + count > pick.maximum:
            raise InvalidInput(f"{stat.name} cannot rise above {pick.maximum}")
        for offset in range(count):
            step = bought + len(boosts) + 1
            cost = pick.costs.get(step)
            minimum = pick.minimum_levels.get(step)
            if cost is None:
                raise InvalidInput(f"{pc.name} cannot buy another attribute boost")
            if minimum is not None and level < minimum:
                raise InvalidInput(
                    f"attribute boost {step} needs character level {minimum}; "
                    f"{pc.name} will be level {level}"
                )
            if len(boosts) + 1 > available:
                raise InvalidInput(f"{pick.label} cost more points than are available")
            boosts.append({"stat": stat.id, "step": step, "cost": cost})
        counts[stat.id] = count
    return {"counts": counts, "boosts": boosts}


def _feature_choices(pick: LevelUpPick, form: Mapping[str, Any]) -> list[str]:
    choices: list[str] = []
    # The form has one field per available pick, so read the fields it actually sent.
    for key, value in form.items():
        if key.startswith(f"pick-{pick.id}-") and value not in ("", None):
            choices.append(str(value))
    return choices


def _apply_option(view: AdvancementPage, ctx: ToolContext, form: Mapping[str, Any]) -> None:
    option = str(form.get("option", "")).strip()
    if not option:
        raise InvalidInput("Choose an Advancement")
    stat = str(form.get(f"stat-{option}", "")).strip() or None
    feature = str(form.get(f"feature-{option}", "")).strip() or None
    ctx.world.take_advancement(view.character, option, stat, feature)  # type: ignore[attr-defined]


def tool_context(
    engine: Engine,
    campaign_id: int,
    rule_set: RuleSet,
    world: WorldContent,
) -> ToolContext:
    """A rule-set context for the page's hooks and tools, with no model to speak or narrate."""

    async def speak(_speaker: str, _profile: str, _intent: str, _replace: bool) -> None:
        return None

    def ooc(_narrate: bool) -> None:
        return None

    tools = WorldTools(
        engine,
        campaign_id,
        {**rule_set.tables, **world.tables},
        random.Random(),
        rule_set.character,
        rule_data=rule_set.rule_data,
        grid=rule_set.grid,
        terrain=rule_set.terrain,
        encounter_objects=rule_set.encounter_objects,
        encounter_templates=rule_set.encounter_templates,
        combat=rule_set.combat,
        monsters={**rule_set.monsters, **world.monsters},
        features={**world.features},
        checks=rule_set.checks,
        conditions=rule_set.conditions,
        projects=rule_set.projects,
        moves=rule_set.moves,
        counters=rule_set.counters,
        advancements=rule_set.advancements,
        periods=rule_set.periods,
        tallies=rule_set.tallies,
        relations=rule_set.relations,
        dice_tables={**rule_set.dice_tables, **world.dice_tables},
        clock_rules=rule_set.clock,
        rests=rule_set.rests,
        interrupted_rest=rule_set.interrupted_rest,
        hooks=rule_set.hooks,
        inventory=rule_set.inventory,
        item_sizes=rule_set.item_sizes,
        magic=rule_set.magic,
        marketplace=rule_set.marketplace,
        plus=rule_set.plus,
        npc_hp=rule_set.npc_hp,
        npc_hp_stat=rule_set.npc_hp_stat,
        travel=rule_set.travel,
        wilderness=rule_set.wilderness,
        world_terrain=rule_set.world_terrain,
    )
    return ToolContext(
        world=tools,
        speak=speak,
        ooc=ooc,
        rules=rule_set.rules,
        lookup_tools={},
    )


def character_from(engine: Engine, campaign_id: int, name: str) -> Character | None:
    with session_scope(engine) as db:
        return load_state(db, campaign_id).characters.get(name_key(name))


def debug_grant_options(
    state: Any, rule_set: RuleSet
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """The XP or Advancement a debug control can grant, or None when the rule set has neither."""
    pc = state.pc()
    if pc is None or pc.kind != PC_KIND:
        return None, None
    xp = None
    level_rules = next(iter(rule_set.level_ups.values()), None)
    if level_rules is not None:
        level = pc.counters.get(
            level_rules.level_counter, rule_set.counters[level_rules.level_counter].initial
        )
        if isinstance(level, int) and level < level_rules.max_level:
            rate = state.options.get(level_rules.rate_option) or next(iter(level_rules.tracks))
            track = level_rules.tracks.get(rate) or next(iter(level_rules.tracks.values()))
            current = pc.counters.get(
                level_rules.xp_counter, rule_set.counters[level_rules.xp_counter].initial
            )
            xp = {"amount": max(1, track[level] - int(current))}
    advancement = None
    advancement_rules = next(iter(rule_set.advancements.values()), None)
    if advancement_rules is not None:
        advancement = {
            "counter": advancement_rules.counter,
            "label": advancement_rules.label,
        }
    return xp, advancement


def debug_grant(
    engine: Engine,
    campaign_id: int,
    rule_set: RuleSet,
    world: WorldContent,
    kind: str,
    name: str,
    amount: int = 0,
) -> None:
    """Give the player character XP or an Advancement from the debug panel.

    This is an explicit tester control, not a GM tool: it writes the counter directly through
    the normal world tool, outside a GM turn, so the turn review never sees it.
    """
    ctx = tool_context(engine, campaign_id, rule_set, world)
    if kind == "xp":
        level_rules = next(iter(rule_set.level_ups.values()), None)
        if level_rules is None:
            raise InvalidInput("this rule set has no levels")
        if amount < 1:
            raise InvalidInput("experience must be a whole number above 0")
        ctx.world.adjust_counter(name, level_rules.xp_counter, amount)
        return
    if kind == "advancement":
        advancement_rules = next(iter(rule_set.advancements.values()), None)
        if advancement_rules is None:
            raise InvalidInput("this rule set has no Advancements")
        ctx.world.adjust_counter(name, advancement_rules.counter, 1)
        return
    raise InvalidInput("kind must be 'xp' or 'advancement'")
