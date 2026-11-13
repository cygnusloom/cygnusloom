"""Character options a rule set defines, and the JSON schema derived from them.

A character has five kinds of option. Stats are numbers rolled with a dice formula, assigned
from a fixed pool of values shared by their swap group, or derived from the other stats. Skills
are levels a character gains from their templates. Templates,
such as a class or background, are chosen by kind and grant features, skills, and picks. Features
are items, traits, spells, or feats. Fields are text, or a choice from a table. The core rule set
defines the options every rule set shares, such as appearance and biography."""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from cygnusloom.content.definitions import (
    ContentError,
    Definition,
    range_bands,
    required_text,
)
from cygnusloom.content.icons import FALLBACK_ITEM, FALLBACK_WEAPON, resolve_icon
from cygnusloom.rules.dice import FORMULA, is_formula, roll_total


@dataclass(frozen=True)
class FeatureTypeRules:
    """How one ruleset names and groups a kind of feature. The engine's seven defaults cover
    the bundled rulesets; a ruleset may add its own or relabel one (Academia calls feats Arts)."""

    id: str
    name: str
    plural: str = ""
    group: str = ""
    rank_label: str = ""
    tag_label: str = ""
    # Subheadings for particular tags, such as Foci and Arts sharing the feat type.
    tag_groups: Mapping[str, str] = field(default_factory=dict)
    unique: bool = False
    inventory: bool = False


DEFAULT_FEATURE_TYPES: Mapping[str, FeatureTypeRules] = {
    "item": FeatureTypeRules("item", "Item", "Items", "Items", inventory=True),
    "trait": FeatureTypeRules("trait", "Trait", "Traits", "Traits"),
    "spell": FeatureTypeRules(
        "spell",
        "Spell",
        "Spells",
        "Spells",
        rank_label="Rank",
        tag_label="Tradition",
        unique=True,
    ),
    "potion": FeatureTypeRules("potion", "Potion", "Potions", "Potions"),
    "enchantment": FeatureTypeRules("enchantment", "Enchantment", "Enchantments", "Enchantments"),
    "subject": FeatureTypeRules("subject", "Subject", "Subjects", "Subjects"),
    "feat": FeatureTypeRules("feat", "Feat", "Feats", "Feats", rank_label="Rank"),
}
# The effects a feature may run when it is granted. A `stat`, `skill`, or `counter` effect adds
# its `delta` to that stat's score, that skill's level, or that counter's starting value. An
# `armor` effect adds its `delta` to the character's armor. A `note` effect only says something.
FEATURE_EFFECT_OPS = ("stat", "skill", "counter", "armor", "note")
# When a feature may react to a roll that is already being resolved.
REACTION_TRIGGERS = ("move", "check", "reaction", "save", "attack", "damage")
# What a reaction may do to that roll.
REACTION_EFFECT_OPS = (
    "raise_band",
    "add_total",
    "set_reaction",
    "reroll",
    "reroll_ones",
    "best_of",
)


@dataclass(frozen=True)
class TableOption:
    """An entry in a table. A named entry has a `name` the player reads as a title, above its
    `description`. Other entries are a description only. A character stores the `value`: the
    name when there is one, otherwise the description."""

    name: str | None
    description: str
    # The group an option belongs to, such as "core". Fields can roll by group or restrict groups.
    group: str | None = None

    @property
    def value(self) -> str:
        return self.name or self.description


@dataclass(frozen=True)
class Skill:
    """A skill a character can have a level in. The level is kept apart from the skill itself,
    which every character of the rule set shares."""

    id: str
    name: str
    description: str


@dataclass(frozen=True)
class SkillReplacements:
    """Excluded skills for excess rolled/template and feature grants during creation."""

    roll_exclude: tuple[str, ...] = ()
    feature_exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillProgression:
    """Untrained and trained skill levels, including the creation and advancement limits."""

    untrained: int
    first_level: int
    creation_maximum: int
    maximum: int
    creation_replacements: SkillReplacements | None = None


@dataclass(frozen=True)
class DerivedStat:
    """A stat whose value comes from the other stats, the counters, and the skills rather than
    from a roll. Its value is `base` + `plus`, minus the values of `minus_counters`, plus the
    best modifier among `best_modifier`, minus the best modifier among `minus_best_modifier`,
    plus the best raw score among `best_score`, plus the levels of `plus_skills` and selected
    `plus_skill_picks`, and minus one when any template in `minus_templates` was chosen. A stat
    with `requires_templates` exists only when one of those templates was chosen."""

    base: int = 0
    plus: int = 0
    minus_counters: tuple[str, ...] = ()
    best_modifier: tuple[str, ...] = ()
    best_score: tuple[str, ...] = ()
    plus_skills: tuple[str, ...] = ()
    plus_skill_picks: tuple[str, ...] = ()
    # Modifiers subtracted from the value, such as a saving throw that improves with an
    # attribute modifier. The best of them is used.
    minus_best_modifier: tuple[str, ...] = ()
    # Template ids that each make the value one lower, such as a partial class's Effort penalty.
    minus_templates: tuple[str, ...] = ()
    # Template ids that make the stat available, such as the classes or traditions that grant an
    # Effort pool. Empty means every character has the stat; otherwise it is left off the sheet
    # when none of them was chosen.
    requires_templates: tuple[str, ...] = ()
    # The lowest value the stat may take, when it has one.
    minimum: int | None = None


@dataclass(frozen=True)
class Stat:
    id: str
    name: str
    # The dice a stat is rolled with, or None when it is assigned from `values` or derived.
    formula: str | None
    # Stats that share a swap group may trade values. A rolled group allows one swap; a group
    # with `values` may be assigned in any order.
    swap_group: str | None
    low: int
    high: int
    # The pool a stat with no formula is assigned from. Every stat in its group shares the pool.
    values: tuple[int, ...] = ()
    # The modifier bands a score falls into, as (minimum, modifier) pairs with rising minimums.
    # A score takes the modifier of the highest minimum it reaches. With no bands the modifier
    # is the score itself, which is how Cairn reads its stats.
    modifier_bands: tuple[tuple[int, int], ...] = ()
    # The stat whose modifier is added to a rolled or given value, such as Constitution to a hit
    # die. Empty when the value is used as it stands.
    plus_modifier: str = ""
    # The lowest value the stat may take after it is rolled or given, such as 1 hit point.
    minimum: int | None = None
    # The parts of the stat, when it is computed from the others rather than rolled. Such a stat
    # takes no formula, values, swap group, or modifier of its own.
    derived: DerivedStat | None = None

    def modifier(self, score: int) -> int:
        """The modifier a score gives: the modifier of the highest band it reaches, or the score
        itself when the stat has no bands."""
        if not self.modifier_bands:
            return score
        reached = [modifier for minimum, modifier in self.modifier_bands if score >= minimum]
        return reached[-1] if reached else 0


@dataclass(frozen=True)
class DefenseFloor:
    """A minimum base defense with optional counter and equipment restrictions."""

    base: int
    counter: str = ""
    divisor: int = 1
    round_up: bool = True
    requires_no_armor: bool = False
    requires_no_shield: bool = False


@dataclass(frozen=True)
class FeaturePick:
    """A structured choice a feature requires, such as the attribute for Developed Attribute.
    `choices` limits the pick and `exclude` removes options; an empty `choices` means every stat
    or skill of the pick's kind. `grant` says what the choice gives: `trained` makes a skill
    trained at level 0, `modifier` adds one to a stat's modifier, and `check_best` makes checks
    with a chosen skill roll `dice` dice and keep the best `keep`."""

    id: str
    kind: str
    label: str
    choices: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_value: int = 0
    count: int = 1
    grant: str = ""
    dice: int = 0
    keep: int = 0


@dataclass(frozen=True)
class Reaction:
    """A feature's reaction to a roll being resolved. `trigger` names the kind of roll, `when`
    narrows which rolls it applies to, and `effect` says what it does. A reaction with `uses`
    above zero may be used that many times before its `reset` label clears its uses, such as
    "week". `text` is what the log says when it fires; a reaction with no text uses a default."""

    id: str
    trigger: str
    when: Mapping[str, Any]
    effect: Mapping[str, Any]
    uses: int = 0
    reset: str = ""
    # What the Log says when the reaction fires, in the past tense, so the GM knows it is done.
    text: str = ""
    # What the character sheet says the reaction does, in the present tense. Falls back to
    # `text` when the reaction does not supply its own sheet wording.
    sheet: str = ""
    # A reaction the rules let the player choose to use, such as one that costs Effort. The
    # player confirms it before it spends a use; automatic reactions such as Lucky fire by
    # themselves.
    optional: bool = False
    # Reactions with the same `shared` name spend one pool of uses, such as a Focus that can
    # reroll either a check or a save once before it recharges.
    shared: str = ""


@dataclass(frozen=True)
class Feature:
    """A feature a character can have. An `item` is carried as an inventory item of `size`, with
    `uses` if it is consumed and `armor` added to the character's armor while carried. A `spell`
    is a power the character can cast; how it is cast, and from what, is set by the rule set. A
    feature with no `uses` has none to spend. A feature may carry `effects` that run once when
    it is granted while a character is created, and `reactions` that modify rolls during play."""

    id: str
    name: str
    feature_type: str
    description: str
    size: str
    uses: int
    armor: int
    # The weapon's damage dice, such as "1d6", or "1d6+1d6" for two weapons (keep the higher). Empty when the feature is not a weapon.
    damage: str = ""
    # The ids of rules attached to this feature. Their text reaches the GM while the item is carried.
    rules: tuple[str, ...] = ()
    # The rule set's label for the feature, such as the Year a spell belongs to. A template's
    # `choose` picks features by it.
    tag: str = ""
    # The feature's rank within its kind, such as a focus's level (1 or 2) or a spell's level
    # (1 to 5). Zero when the feature has no rank.
    rank: int = 0
    # The skill a weapon is used with, such as "stab". Empty when the feature is no weapon.
    skill: str = ""
    # The ids of the stats whose modifier a weapon adds to its damage, such as strength.
    attributes: tuple[str, ...] = ()
    # The Shock a weapon deals to a target it misses whose armor is at most `shock_ac`.
    shock: int = 0
    shock_ac: int = 0
    # A weapon's melee reach in feet when it is longer than the rule set's default, such as a
    # pike's 10. Zero uses the rule set's default reach.
    reach: int = 0
    # A ranged attack's bands and the farthest distance in feet of each, such as
    # {"short": 30, "long": 60}. Empty for a melee-only weapon.
    ranges: Mapping[str, int] = field(default_factory=dict)
    # Engine flags the weapon or item carries, such as "no_unskilled_penalty".
    flags: tuple[str, ...] = ()
    # Check ids this worn item penalizes by its Encumbrance, such as Sneak or Exert.
    encumbrance_checks: tuple[str, ...] = ()
    # The effects run when the feature is granted while a character is created, in the order
    # they are given. See FEATURE_EFFECT_OPS.
    effects: tuple[Mapping[str, Any], ...] = ()
    # The reactions the feature may take to moves, checks, and reaction rolls. See Reaction.
    reactions: tuple[Reaction, ...] = ()
    # A passive defense floor, optionally restricted by equipped armor or shields.
    defense_floor: DefenseFloor | None = None
    # A feature that may be taken again as a distinct choice.
    repeatable: bool = False
    # Extra maximum hit points this feature adds for every character level, such as Die Hard's 2.
    level_hp_bonus: int = 0
    # The structured choices the feature asks the player to make.
    picks: tuple[FeaturePick, ...] = ()
    # The icon the feature shows, resolved from `icon` or `looks_like` when the content loads.
    icon: str | None = None
    # The definition or icon whose looks this one borrows. Kept for authors and lookups.
    looks_like: str | None = None


@dataclass(frozen=True)
class Granted:
    """A feature a template gives, with its count: a whole number or a dice formula."""

    feature: str
    quantity: str


@dataclass(frozen=True)
class Choose:
    """Features a template lets the character pick: `count` of `feature_type` whose tag is the
    value of the field `tag_field`. A pick left out is rolled from the same pool. Tags in
    `skip_tags` do not require a pick."""

    count: int
    feature_type: str
    tag_field: str
    skip_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillPick:
    """Skills a template lets the character pick from a table: `count` of the table's option
    values, which are skill ids, save the ones in `exclude`. A pick left out is rolled. No skill
    is raised above `max_level` by a rolling pick; `label` identifies the choice on the form."""

    id: str
    count: int
    table: str
    max_level: int
    exclude: tuple[str, ...] = ()
    label: str = "Skills"


@dataclass(frozen=True)
class FeatPick:
    """Features a template lets the character pick: `count` of the features whose tag is one of
    `tags` (any tag when it is empty) and whose rank is at most `max_rank` when one is given, save
    the ones in `exclude`. A pick left out is rolled from the same pool."""

    id: str
    count: int
    tag: str = ""
    max_rank: int | None = None
    exclude: tuple[str, ...] = ()
    # Every tag the pick accepts. Empty means any tag; `tag` is kept for older content.
    tags: tuple[str, ...] = ()
    # The feature type the pick offers, such as "spell" or "feat". Empty means any type.
    feature_type: str = ""
    # Restrict this pick to the tags of the features chosen in another pick of its template,
    # such as spells that belong to the character's chosen subjects.
    tags_from: str = ""
    # A feature taken by one pick cannot be taken by another, except to advance its rank.
    unique: bool = False
    # A repeated ranked feature can advance to its next rank instead of adding another copy.
    advance_repeated_rank: bool = False


@dataclass(frozen=True)
class TemplateRequirement:
    """Selections required by a template, optionally distinct by their choice keys."""

    kinds: tuple[str, ...]
    distinct: bool = False
    missing_message: str | None = None
    duplicate_message: str | None = None


@dataclass(frozen=True)
class TemplateGrant:
    """What a template hands the character at creation beyond stats, skills, and features: items,
    money, and the equipment already worn or wielded. Names are the rule set's catalogue names, so
    a starting kit reads the way the book's equipment list does."""

    items: tuple[str, ...] = ()
    silver: int = 0
    equip: tuple[str, ...] = ()


@dataclass(frozen=True)
class StartingSilver:
    """Money a character starts with instead of a template of `kind`, such as rolling 3d6 × 10
    silver when no equipment package is chosen."""

    kind: str
    label: str
    description: str
    dice: str
    multiplier: int = 1


@dataclass(frozen=True)
class Template:
    id: str
    kind: str
    name: str
    description: str
    features: tuple[Granted, ...]
    tables: tuple[str, ...]
    # Suggested character names for this template. They feed the name dice, not the schema.
    names: tuple[str, ...]
    # Bonuses added to stats, by stat id, when the template is chosen.
    stats: Mapping[str, int] = field(default_factory=dict)
    # Amounts added to counters' starting values, by counter id, when the template is chosen.
    counters: Mapping[str, int] = field(default_factory=dict)
    # Features the character picks when the template is chosen.
    choose: Choose | None = None
    # Skill levels added to the unskilled base, by skill id, when the template is chosen.
    skills: Mapping[str, int] = field(default_factory=dict)
    # Skills the character picks from a table when the template is chosen.
    skill_picks: tuple[SkillPick, ...] = ()
    # Features the character picks by tag when the template is chosen.
    feat_picks: tuple[FeatPick, ...] = ()
    # Other template kinds and the choices this template can be combined with.
    compatible_with: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    template_requirements: tuple[TemplateRequirement, ...] = ()
    # Selections with the same key represent the same choice across different kinds.
    choice_key: str = ""
    # Items, money, and equipment handed over at creation, such as a starting equipment kit.
    grant: TemplateGrant = field(default_factory=TemplateGrant)


@dataclass(frozen=True)
class Restriction:
    """Limits a field's options to `groups` while the field `field_id` takes one of `values`."""

    field_id: str
    values: tuple[str, ...]
    groups: tuple[str, ...]


@dataclass(frozen=True)
class Field:
    """A text field when `table` is None, otherwise a choice from that table. A `group_roll` field
    rolls a group first and then an option within it. A `restriction` limits its options. A `knows`
    field names a feature type: a character knows every feature of that type whose tag is an
    earlier option of the table than the field's value."""

    id: str
    name: str
    description: str
    table: str | None
    group_roll: bool = False
    restriction: Restriction | None = None
    knows: str | None = None


@dataclass(frozen=True)
class CounterRef:
    """A counter a template can give a starting value to, with the bounds it keeps."""

    id: str
    label: str
    initial: int
    minimum: int | None
    maximum: int | None


@dataclass(frozen=True)
class CharacterSpec:
    stats: Mapping[str, Stat]
    templates: Mapping[str, Template]
    features: Mapping[str, Feature]
    fields: Mapping[str, Field]
    tables: Mapping[str, tuple[TableOption, ...]]
    table_names: Mapping[str, str]
    counters: Mapping[str, CounterRef] = field(default_factory=dict)
    # The skills a character of this rule set may have a level in.
    skills: Mapping[str, Skill] = field(default_factory=dict)
    skill_progression: SkillProgression | None = None
    ordered_feature_tags: tuple[str, ...] = ()
    # Template kinds a character may leave unchosen, such as a magical tradition only mages need.
    optional_kinds: tuple[str, ...] = ()
    # Money a character rolls when a template kind, such as a starting kit, is left unchosen.
    starting_silver: StartingSilver | None = None
    # The feature types this rule set offers, keyed by id. Defaults cover the seven core types.
    feature_types: Mapping[str, FeatureTypeRules] = field(
        default_factory=lambda: dict(DEFAULT_FEATURE_TYPES)
    )

    def kinds(self) -> list[str]:
        return sorted({t.kind for t in self.templates.values()})

    def modifier(self, stats: Mapping[str, int], stat: str) -> int:
        """The modifier a stat's score gives, as the stat's bands read it. Raises CharacterError
        when the stat or its score is missing."""
        found = self.stats.get(stat)
        if found is None:
            raise CharacterError(f"{stat!r} is no stat of this rule set")
        if stat not in stats:
            raise CharacterError(f"no score was given for the stat {stat!r}")
        return found.modifier(stats[stat])

    def skill_level(self, skills: Mapping[str, int], skill: str) -> int:
        """A character's level in a skill, including the ruleset's untrained value."""
        if self.skill_progression is None:
            raise CharacterError("the rule set has no skill progression")
        return skills.get(skill, self.skill_progression.untrained)

    def skill_trained(self, skills: Mapping[str, int], skill: str) -> bool:
        """Training is stored separately from its level, which may equal the untrained value."""
        return skill in skills

    def stat_bounds(self, stat: str) -> tuple[int, int]:
        """The lowest and highest a submitted score for a stat may be. A stat that adds another
        stat's modifier, such as hit points adding Constitution, widens its die's range by every
        modifier that stat can give."""
        rules = self.stats[stat]
        low, high = rules.low, rules.high
        if rules.minimum is not None:
            low = max(low, rules.minimum)
        source = self.stats.get(rules.plus_modifier) if rules.plus_modifier else None
        if source is not None:
            modifiers = [modifier for _, modifier in source.modifier_bands] or [0]
            low += min(modifiers)
            high += max(modifiers)
        return low, high


def build_character(
    definitions: list[Definition],
    tables: Mapping[str, tuple[TableOption, ...]],
    table_names: Mapping[str, str],
    template: str,
    *,
    skill_progression: object = None,
    ordered_feature_tags: object = (),
    icon_entries: Mapping[str, tuple[str | None, str | None]] | None = None,
    starting_silver: StartingSilver | None = None,
    feature_types: Mapping[str, FeatureTypeRules] | None = None,
    template_features: object = None,
) -> CharacterSpec:
    """Build the character spec from a rule set's definitions. `tables` are already loaded.
    `icon_entries` maps a feature id to its raw (icon, looks_like) so one feature may borrow
    another's looks."""
    feature_types = dict(feature_types or DEFAULT_FEATURE_TYPES)
    stats = _each(definitions, "stat", lambda d: _stat(d, template))
    _check_groups(stats, template)
    skills = _each(definitions, "skill", lambda d: _skill(d, template))
    levels = _skill_progression(skill_progression, skills, template)
    if skills and levels is None:
        raise ContentError(f"{template}: skills need a 'skill_progression' definition")
    counter_ids = frozenset(d.id for d in definitions if d.type == "counter")
    features = _each(
        definitions,
        "feature",
        lambda d: load_feature(
            d,
            template,
            stats=stats,
            skills=skills,
            counters=counter_ids,
            icon_entries=icon_entries,
            feature_types=feature_types,
        ),
    )
    if ordered_feature_tags == ():
        ordered_feature_tags = []
    ordered_tags = _text_ids(ordered_feature_tags, "ordered_feature_tags", template)
    for tag in ordered_tags:
        if not any(feature.tag == tag and feature.rank > 0 for feature in features.values()):
            raise ContentError(f"{template}: ordered feature tag {tag!r} has no ranked features")
    for feature in features.values():
        floor = feature.defense_floor
        if floor is not None and floor.counter and floor.counter not in counter_ids:
            raise ContentError(
                f"{template}: feature {feature.id!r} defense_floor names no counter "
                f"({floor.counter!r})"
            )
    fields = _each(
        definitions, "character_field", lambda d: _field(d, tables, "character_field", template)
    )
    default_features = _template_features(template_features, features, template)
    templates = _each(
        definitions,
        "template",
        lambda d: _template(
            d,
            features,
            tables,
            skills,
            levels,
            template,
            defaults=default_features,
            feature_types=feature_types,
        ),
    )
    _check_stat_links(stats, skills, counter_ids, template)
    _check_derived_skill_picks(stats, templates, fields, template)
    kinds = {item.kind for item in templates.values()}
    for item in templates.values():
        label = f"{template}: template {item.id!r}"
        for kind, allowed in item.compatible_with.items():
            if kind not in kinds:
                raise ContentError(f"{label}: 'compatible_with' names no kind ({kind!r})")
            for ident in allowed:
                target = templates.get(ident)
                if target is None or target.kind != kind:
                    raise ContentError(
                        f"{label}: 'compatible_with' names no {kind!r} template ({ident!r})"
                    )
        for requirement in item.template_requirements:
            for kind in requirement.kinds:
                if kind not in kinds:
                    raise ContentError(f"{label}: 'template_requirements' names no kind ({kind!r})")
    for stat in stats.values():
        if stat.derived is None:
            continue
        for name in (*stat.derived.minus_templates, *stat.derived.requires_templates):
            if name not in templates:
                raise ContentError(
                    f"{template}: stat {stat.id!r} 'derived' names no template ({name!r})"
                )
    field_ids = set(fields)
    for item in templates.values():
        for table in item.tables:
            if table in field_ids:
                raise ContentError(
                    f"{template}: template {item.id!r} table {table!r} clashes with a field"
                )
    _check_fields(fields, tables, template)
    _check_pick_fields(templates, fields, tables, template)
    used_feature_types = {feature.feature_type for feature in features.values()}
    for item in fields.values():
        if item.knows is not None and item.knows not in used_feature_types:
            raise ContentError(
                f"{template}: character_field {item.id!r} knows {item.knows!r}, "
                "which is no feature type"
            )
    if starting_silver is not None and starting_silver.kind not in {
        item.kind for item in templates.values()
    }:
        raise ContentError(
            f"{template}: starting_silver names no template kind ({starting_silver.kind!r})"
        )
    return CharacterSpec(
        stats=stats,
        templates=templates,
        features=features,
        fields=fields,
        tables=tables,
        table_names=table_names,
        skills=skills,
        skill_progression=levels,
        ordered_feature_tags=ordered_tags,
        starting_silver=starting_silver,
        feature_types=feature_types,
    )


def _derived_skill_pick_ids(stats: Mapping[str, Stat]) -> set[str]:
    return {
        pick_id
        for stat in stats.values()
        if stat.derived is not None
        for pick_id in stat.derived.plus_skill_picks
    }


def _check_derived_skill_picks(
    stats: Mapping[str, Stat],
    templates: Mapping[str, Template],
    fields: Mapping[str, Field],
    source: str,
) -> None:
    for pick_id in _derived_skill_pick_ids(stats):
        owners = [
            (item, pick)
            for item in templates.values()
            for pick in item.skill_picks
            if pick.id == pick_id
        ]
        if not owners:
            raise ContentError(f"{source}: derived stat names no skill pick ({pick_id!r})")
        if any(pick.count != 1 for _, pick in owners):
            raise ContentError(f"{source}: derived skill pick {pick_id!r} must choose one skill")
        if len({item.kind for item, _ in owners}) != 1:
            raise ContentError(f"{source}: derived skill pick {pick_id!r} spans template kinds")
        if pick_id in fields or any(pick_id in item.tables for item in templates.values()):
            raise ContentError(f"{source}: derived skill pick {pick_id!r} clashes with a field")


def _check_pick_fields(
    templates: Mapping[str, Template],
    fields: Mapping[str, Field],
    tables: Mapping[str, tuple[TableOption, ...]],
    template: str,
) -> None:
    for item in templates.values():
        if item.choose is None:
            continue
        gate = fields.get(item.choose.tag_field)
        if gate is None or gate.table is None:
            raise ContentError(
                f"{template}: template {item.id!r} 'choose' needs a table field named "
                f"{item.choose.tag_field!r}"
            )
        values = {option.value for option in tables[gate.table]}
        for tag in item.choose.skip_tags:
            if tag not in values:
                raise ContentError(
                    f"{template}: template {item.id!r} 'choose' skip_tags names no "
                    f"{item.choose.tag_field!r} option ({tag!r})"
                )


def _check_fields(
    fields: Mapping[str, Field], tables: Mapping[str, tuple[TableOption, ...]], template: str
) -> None:
    for item in fields.values():
        label = f"{template}: character_field {item.id}"
        if item.group_roll and not all(option.group for option in tables[item.table or ""]):
            raise ContentError(f"{label}: 'group_roll' needs every option to have a 'group'")
        restriction = item.restriction
        if restriction is None:
            continue
        gate = fields.get(restriction.field_id)
        if item.table is None or gate is None or gate.table is None:
            raise ContentError(
                f"{label}: 'restricted_by' needs a table field named {restriction.field_id!r}"
            )
        if gate.restriction is not None:
            raise ContentError(f"{label}: the field {gate.id!r} it names may not be restricted")
        if not set(restriction.values) <= {option.value for option in tables[gate.table]}:
            raise ContentError(f"{label}: 'restricted_by' values must be options of {gate.id!r}")
        if not set(restriction.groups) <= {option.group for option in tables[item.table]}:
            raise ContentError(f"{label}: 'restricted_by' groups must be groups of its options")


def _each(definitions: list[Definition], kind: str, make: Any) -> dict[str, Any]:
    return {d.id: make(d) for d in definitions if d.type == kind}


def _stat(definition: Definition, template: str) -> Stat:
    label = f"{template}: stat {definition.id}"
    name = required_text(definition.data, "name", label)
    if "derived" in definition.data:
        return _derived_stat(definition, label, name)
    swap = definition.data.get("swap_group")
    if swap is not None and (not isinstance(swap, str) or not swap.strip()):
        raise ContentError(f"{label}: 'swap_group' must be non-empty text")
    bands = _modifier_bands(definition.data.get("modifier"), label)
    plus_modifier = definition.data.get("plus_modifier", "")
    if not isinstance(plus_modifier, str) or (plus_modifier and not plus_modifier.strip()):
        raise ContentError(f"{label}: 'plus_modifier' must be the id of a stat")
    minimum = definition.data.get("minimum")
    if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int)):
        raise ContentError(f"{label}: 'minimum' must be a whole number")
    if "values" in definition.data:
        return _assigned_stat(definition, label, name, swap, bands, plus_modifier, minimum)
    formula = required_text(definition.data, "formula", label)
    match = FORMULA.fullmatch(formula)
    if match is None:
        raise ContentError(f"{label}: 'formula' must be a dice formula such as '3d6'")
    count, sides = int(match.group(1)), int(match.group(2))
    return Stat(
        id=definition.id,
        name=name,
        formula=formula,
        swap_group=swap,
        low=count,
        high=count * sides,
        modifier_bands=bands,
        plus_modifier=plus_modifier,
        minimum=minimum,
    )


def _modifier_bands(value: object, label: str) -> tuple[tuple[int, int], ...]:
    """A stat's modifier bands: a non-empty list of `[min, modifier]` pairs or objects with a
    whole `min` and `modifier`, each `min` above the one before it."""
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise ContentError(f"{label}: 'modifier' must be a non-empty list of bands")
    bands: list[tuple[int, int]] = []
    for band in value:
        if isinstance(band, list) and len(band) == 2:
            minimum, modifier = band
        elif isinstance(band, Mapping) and set(band) == {"min", "modifier"}:
            minimum, modifier = band["min"], band["modifier"]
        else:
            raise ContentError(f"{label}: each 'modifier' band needs a 'min' and a 'modifier'")
        for amount in (minimum, modifier):
            if not isinstance(amount, int) or isinstance(amount, bool):
                raise ContentError(f"{label}: 'modifier' bands must be whole numbers")
        if bands and minimum <= bands[-1][0]:
            raise ContentError(f"{label}: 'modifier' bands must have a strictly rising 'min'")
        bands.append((minimum, modifier))
    return tuple(bands)


def _derived_stat(definition: Definition, label: str, name: str) -> Stat:
    data = definition.data
    for key in ("formula", "values", "swap_group", "modifier", "plus_modifier"):
        if key in data:
            raise ContentError(f"{label}: a stat with 'derived' takes no {key!r}")
    return Stat(
        id=definition.id,
        name=name,
        formula=None,
        swap_group=None,
        low=0,
        high=0,
        derived=_derived(data["derived"], label),
    )


def _derived(value: object, label: str) -> DerivedStat:
    """The parts of a derived stat. Every amount is a whole number, and every list may be empty."""
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'derived' must be an object")
    unknown = set(value) - {
        "base",
        "plus",
        "minus_counters",
        "best_modifier",
        "best_score",
        "plus_skills",
        "plus_skill_picks",
        "minus_best_modifier",
        "minus_templates",
        "requires_templates",
        "minimum",
    }
    if unknown:
        raise ContentError(f"{label}: 'derived' has unknown keys {sorted(unknown)}")
    return DerivedStat(
        base=_whole_number(value, "base", label, 0),
        plus=_whole_number(value, "plus", label, 0),
        minus_counters=_id_list(value.get("minus_counters", []), "minus_counters", label),
        best_modifier=_id_list(value.get("best_modifier", []), "best_modifier", label),
        best_score=_id_list(value.get("best_score", []), "best_score", label),
        plus_skills=_id_list(value.get("plus_skills", []), "plus_skills", label),
        plus_skill_picks=_id_list(value.get("plus_skill_picks", []), "plus_skill_picks", label),
        minus_best_modifier=_id_list(
            value.get("minus_best_modifier", []), "minus_best_modifier", label
        ),
        minus_templates=_id_list(value.get("minus_templates", []), "minus_templates", label),
        requires_templates=_id_list(
            value.get("requires_templates", []), "requires_templates", label
        ),
        minimum=None if "minimum" not in value else _whole_number(value, "minimum", label, 0),
    )


def _whole_number(data: Mapping[str, Any], key: str, label: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContentError(f"{label}: {key!r} must be a whole number")
    return value


def _id_list(value: object, key: str, label: str) -> tuple[str, ...]:
    """A list of ids: text, with no repeats. It may be empty."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ContentError(f"{label}: 'derived' {key!r} must be a list of ids")
    if len(set(value)) != len(value):
        raise ContentError(f"{label}: 'derived' {key!r} repeats an id")
    return tuple(value)


def _check_stat_links(
    stats: Mapping[str, Stat],
    skills: Mapping[str, Skill],
    counter_ids: frozenset[str],
    template: str,
) -> None:
    """A stat's `plus_modifier` and a derived stat's parts must name things the rule set defines."""
    for stat in stats.values():
        label = f"{template}: stat {stat.id}"
        if stat.plus_modifier:
            target = stats.get(stat.plus_modifier)
            if target is None or target.derived is not None:
                raise ContentError(
                    f"{label}: 'plus_modifier' names no rolled stat ({stat.plus_modifier!r})"
                )
        derived = stat.derived
        if derived is None:
            continue
        for name in (*derived.best_modifier, *derived.best_score, *derived.minus_best_modifier):
            if name not in stats:
                raise ContentError(f"{label}: 'derived' names no stat ({name!r})")
        for name in derived.plus_skills:
            if name not in skills:
                raise ContentError(f"{label}: 'derived' names no skill ({name!r})")
        for name in derived.minus_counters:
            if counter_ids and name not in counter_ids:
                raise ContentError(f"{label}: 'derived' names no counter ({name!r})")


def _skill(definition: Definition, template: str) -> Skill:
    label = f"{template}: skill {definition.id}"
    description = definition.data.get("description", "")
    if not isinstance(description, str):
        raise ContentError(f"{label}: 'description' must be text")
    return Skill(
        id=definition.id,
        name=required_text(definition.data, "name", label),
        description=description,
    )


def _skill_progression(
    value: object, skills: Mapping[str, Skill], template: str
) -> SkillProgression | None:
    if value is None:
        return None
    label = f"{template}: 'skill_progression'"
    if not isinstance(value, Mapping):
        raise ContentError(f"{label} must be an object")
    keys = {"untrained", "first_level", "creation_maximum", "maximum"}
    if not keys <= set(value) or set(value) - keys - {"creation_replacements"}:
        raise ContentError(
            f"{label} needs {', '.join(sorted(keys))} and optional 'creation_replacements'"
        )
    levels: dict[str, int] = {}
    for key in keys:
        level = value[key]
        if not isinstance(level, int) or isinstance(level, bool):
            raise ContentError(f"{label} {key!r} must be a whole number")
        levels[key] = level
    if not levels["first_level"] <= levels["creation_maximum"] <= levels["maximum"]:
        raise ContentError(f"{label} limits must be in increasing order")
    replacement_data = value.get("creation_replacements")
    replacements = None
    if "creation_replacements" in value:
        if not isinstance(replacement_data, Mapping) or set(replacement_data) - {
            "roll_exclude",
            "feature_exclude",
        }:
            raise ContentError(
                f"{label} 'creation_replacements' must contain only "
                "'roll_exclude' and 'feature_exclude'"
            )
        roll_exclude = _text_ids(replacement_data.get("roll_exclude", []), "roll_exclude", label)
        feature_exclude = _text_ids(
            replacement_data.get("feature_exclude", []), "feature_exclude", label
        )
        for skill_id in (*roll_exclude, *feature_exclude):
            if skill_id not in skills:
                raise ContentError(f"{label} excludes unknown skill {skill_id!r}")
        replacements = SkillReplacements(roll_exclude, feature_exclude)
    return SkillProgression(
        untrained=levels["untrained"],
        first_level=levels["first_level"],
        creation_maximum=levels["creation_maximum"],
        maximum=levels["maximum"],
        creation_replacements=replacements,
    )


def _assigned_stat(
    definition: Definition,
    label: str,
    name: str,
    swap: str | None,
    bands: tuple[tuple[int, int], ...],
    plus_modifier: str,
    minimum: int | None = None,
) -> Stat:
    if "formula" in definition.data:
        raise ContentError(f"{label}: give 'formula' or 'values', not both")
    values = definition.data["values"]
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in values)
    ):
        raise ContentError(f"{label}: 'values' must be a non-empty list of whole numbers")
    if swap is None:
        raise ContentError(f"{label}: a stat with 'values' needs a 'swap_group' to share them")
    return Stat(
        id=definition.id,
        name=name,
        formula=None,
        swap_group=swap,
        low=min(values),
        high=max(values),
        values=tuple(values),
        modifier_bands=bands,
        plus_modifier=plus_modifier,
        minimum=minimum,
    )


def _check_groups(stats: Mapping[str, Stat], template: str) -> None:
    """Every stat in a swap group must be rolled, or every one assigned from the same pool with a
    pool as big as the group."""
    groups: dict[str | None, list[Stat]] = {}
    for stat in stats.values():
        if stat.swap_group is not None:
            groups.setdefault(stat.swap_group, []).append(stat)
    for group, members in groups.items():
        assigned = {bool(m.values) for m in members}
        if len(assigned) > 1:
            raise ContentError(f"{template}: stats in swap group {group!r} mix rolled and assigned")
        if not members[0].values:
            continue
        pool = sorted(members[0].values)
        if any(sorted(m.values) != pool for m in members):
            raise ContentError(f"{template}: stats in swap group {group!r} must share one pool")
        if len(members) != len(pool):
            raise ContentError(
                f"{template}: swap group {group!r} has {len(members)} stats for {len(pool)} values"
            )


def load_feature(
    definition: Definition,
    template: str,
    *,
    stats: Collection[str] = (),
    skills: Collection[str] = (),
    counters: Collection[str] = (),
    icon_entries: Mapping[str, tuple[str | None, str | None]] | None = None,
    feature_types: Mapping[str, FeatureTypeRules] | None = None,
) -> Feature:
    """Read a feature. `stats`, `skills`, and `counters` are the ids the rule set defines; when one
    is empty the ids of that kind are not checked, as for a world whose rule set is not known.
    `icon_entries` maps feature ids to their raw (icon, looks_like) so `looks_like` may borrow
    another feature's looks. `feature_types` is the rule set's feature-type registry; without
    one, the engine's seven defaults apply."""
    label = f"{template}: feature {definition.id}"
    data = definition.data
    kind = required_text(data, "feature_type", label)
    allowed_types = feature_types or DEFAULT_FEATURE_TYPES
    if kind not in allowed_types:
        raise ContentError(f"{label}: 'feature_type' must be one of {', '.join(allowed_types)}")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ContentError(f"{label}: 'description' must be text")
    size = data.get("size", "normal")
    if not isinstance(size, str) or not size:
        raise ContentError(f"{label}: 'size' must name an item size")
    uses = data.get("uses", 0)
    if isinstance(uses, bool) or not isinstance(uses, int) or uses < 0:
        raise ContentError(f"{label}: 'uses' must be a whole number, 0 or more")
    armor = data.get("armor", 0)
    if isinstance(armor, bool) or not isinstance(armor, int) or armor < 0:
        raise ContentError(f"{label}: 'armor' must be a whole number, 0 or more")
    damage = data.get("damage", "")
    if not isinstance(damage, str) or (damage and not is_formula(damage)):
        raise ContentError(f"{label}: 'damage' must be a dice formula such as 'd6'")
    tag = data.get("tag", "")
    if not isinstance(tag, str):
        raise ContentError(f"{label}: 'tag' must be text")
    rank = data.get("rank", 0)
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
        raise ContentError(f"{label}: 'rank' must be a whole number, 0 or more")
    skill = data.get("skill", "")
    if not isinstance(skill, str) or (skill and not skill.strip()):
        raise ContentError(f"{label}: 'skill' must be the id of a skill")
    if skill and skills and skill not in skills:
        raise ContentError(f"{label}: 'skill' names no skill ({skill!r})")
    shock = _amount(data, "shock", label)
    shock_ac = _amount(data, "shock_ac", label)
    reach = data.get("reach", 0)
    if not isinstance(reach, int) or isinstance(reach, bool) or reach < 0:
        raise ContentError(f"{label}: 'reach' must be a whole number of feet, at least 0")
    ranges = range_bands(data.get("ranges", {}), f"{label}: ranges")
    repeatable = data.get("repeatable", False)
    if not isinstance(repeatable, bool):
        raise ContentError(f"{label}: 'repeatable' must be a boolean")
    level_hp_bonus = data.get("level_hp_bonus", 0)
    if (
        isinstance(level_hp_bonus, bool)
        or not isinstance(level_hp_bonus, int)
        or level_hp_bonus < 0
    ):
        raise ContentError(f"{label}: 'level_hp_bonus' must be a whole number, 0 or more")
    if shock and "shock_ac" not in data:
        raise ContentError(f"{label}: 'shock' needs a 'shock_ac'")
    icon = data.get("icon")
    if icon is not None and (not isinstance(icon, str) or not icon.strip()):
        raise ContentError(f"{label}: 'icon' must be the name of an icon")
    looks_like = data.get("looks_like")
    if looks_like is not None and (not isinstance(looks_like, str) or not looks_like.strip()):
        raise ContentError(f"{label}: 'looks_like' must name an icon or another definition")
    # A carried feature's kind sets its fallback: a weapon shows the weapon icon, other items the
    # generic item icon. Non-item features have no fallback picture.
    fallback = None
    if kind == "item":
        fallback = FALLBACK_WEAPON if damage else FALLBACK_ITEM
    resolved = resolve_icon(
        icon,
        looks_like,
        label=label,
        entries=icon_entries,
        fallback=fallback,
    )
    return Feature(
        id=definition.id,
        name=required_text(data, "name", label),
        feature_type=kind,
        description=description,
        size=size,
        uses=uses,
        armor=armor,
        damage=damage,
        rules=rule_ids(data, label),
        tag=tag,
        rank=rank,
        skill=skill,
        attributes=_stat_ids(data.get("attributes", []), "attributes", stats, label),
        shock=shock,
        shock_ac=shock_ac,
        reach=reach,
        ranges=dict(ranges),
        flags=_text_list(data.get("flags", []), "flags", label),
        encumbrance_checks=_text_list(
            data.get("encumbrance_checks", []), "encumbrance_checks", label
        ),
        effects=_feature_effects(data.get("effects", []), label, stats, skills, counters),
        reactions=_feature_reactions(data.get("reactions", []), label),
        defense_floor=_defense_floor(data.get("defense_floor"), counters, label),
        repeatable=repeatable,
        level_hp_bonus=level_hp_bonus,
        picks=_feature_picks(data.get("picks", []), stats, skills, label),
        icon=resolved,
        looks_like=looks_like,
    )


def _feature_picks(
    value: object, stats: Collection[str], skills: Mapping[str, Any], label: str
) -> tuple[FeaturePick, ...]:
    """The structured choices a feature asks for."""
    if not isinstance(value, list):
        raise ContentError(f"{label}: 'picks' must be a list")
    picks: list[FeaturePick] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ContentError(f"{label}: each pick must be an object")
        pick_id = required_text(entry, "id", f"{label}: pick")
        kind = required_text(entry, "kind", f"{label}: pick {pick_id}")
        if kind not in ("stat", "skill", "text"):
            raise ContentError(f"{label}: pick {pick_id!r} kind must be 'stat', 'skill', or 'text'")
        label_text = required_text(entry, "label", f"{label}: pick {pick_id}")
        choices = _text_list(entry.get("choices", []), "choices", f"{label}: pick {pick_id}")
        exclude = _text_list(entry.get("exclude", []), "exclude", f"{label}: pick {pick_id}")
        allowed = set(stats) if kind == "stat" else set(skills) if kind == "skill" else set()
        unknown = [name for name in (*choices, *exclude) if allowed and name not in allowed]
        if unknown:
            raise ContentError(f"{label}: pick {pick_id!r} names no {kind} ({unknown[0]!r})")
        max_value = entry.get("max_value", 0)
        if isinstance(max_value, bool) or not isinstance(max_value, int) or max_value < 0:
            raise ContentError(f"{label}: pick {pick_id!r} 'max_value' must be 0 or more")
        count = entry.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ContentError(f"{label}: pick {pick_id!r} 'count' must be at least 1")
        grant = entry.get("grant", "")
        if grant not in ("", "trained", "modifier", "check_best"):
            raise ContentError(
                f"{label}: pick {pick_id!r} 'grant' must be '', 'trained', 'modifier', or "
                "'check_best'"
            )
        if grant == "trained" and kind != "skill":
            raise ContentError(f"{label}: pick {pick_id!r} can grant training only to a skill")
        if grant == "modifier" and kind != "stat":
            raise ContentError(f"{label}: pick {pick_id!r} can grant a modifier only to a stat")
        if grant == "check_best" and kind != "skill":
            raise ContentError(f"{label}: pick {pick_id!r} can improve checks only with a skill")
        dice = _pick_number(entry, "dice", label, pick_id)
        keep = _pick_number(entry, "keep", label, pick_id)
        if grant == "check_best":
            if keep < 1 or dice < keep:
                raise ContentError(f"{label}: pick {pick_id!r} needs 'keep' between 1 and 'dice'")
        elif dice or keep:
            raise ContentError(
                f"{label}: pick {pick_id!r} 'dice' and 'keep' need grant 'check_best'"
            )
        picks.append(
            FeaturePick(
                id=pick_id,
                kind=kind,
                label=label_text,
                choices=choices,
                exclude=exclude,
                max_value=max_value,
                count=count,
                grant=grant,
                dice=dice,
                keep=keep,
            )
        )
    return tuple(picks)


def _pick_number(entry: Mapping[str, Any], key: str, label: str, pick_id: str) -> int:
    """A whole number of 0 or more on a feature pick, such as the dice a check keeps."""
    value = entry.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContentError(f"{label}: pick {pick_id!r} {key!r} must be 0 or more")
    return value


def _text_list(value: object, key: str, label: str) -> tuple[str, ...]:
    """A list of short names, with no repeats."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ContentError(f"{label}: {key!r} must be a list of names")
    if len(set(value)) != len(value):
        raise ContentError(f"{label}: {key!r} repeats a name")
    return tuple(value)


def _amount(data: Mapping[str, Any], key: str, label: str) -> int:
    """A whole number of 0 or more, such as a weapon's Shock. Missing is 0."""
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContentError(f"{label}: {key!r} must be a whole number, 0 or more")
    return value


def _stat_ids(value: object, key: str, stats: Collection[str], label: str) -> tuple[str, ...]:
    """A list of stat ids, with no repeats."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ContentError(f"{label}: {key!r} must be a list of stat ids")
    if len(set(value)) != len(value):
        raise ContentError(f"{label}: {key!r} names a stat more than once")
    unknown = [name for name in value if stats and name not in stats]
    if unknown:
        raise ContentError(f"{label}: {key!r} names no stat ({unknown[0]!r})")
    return tuple(value)


def _defense_floor(value: object, counters: Collection[str], label: str) -> DefenseFloor | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'defense_floor' must be an object")
    base = value.get("base")
    if isinstance(base, bool) or not isinstance(base, int) or base < 0:
        raise ContentError(f"{label}: defense_floor 'base' must be a whole number, 0 or more")
    counter = value.get("counter", "")
    if not isinstance(counter, str) or (counter and not counter.strip()):
        raise ContentError(f"{label}: defense_floor 'counter' must be a counter id")
    if counter and counters and counter not in counters:
        raise ContentError(f"{label}: defense_floor names no counter ({counter!r})")
    divisor = value.get("divisor", 1)
    if isinstance(divisor, bool) or not isinstance(divisor, int) or divisor < 1:
        raise ContentError(f"{label}: defense_floor 'divisor' must be a whole number, 1 or more")
    round_up = value.get("round_up", True)
    if not isinstance(round_up, bool):
        raise ContentError(f"{label}: defense_floor 'round_up' must be true or false")
    requires_no_armor = value.get("requires_no_armor", False)
    if not isinstance(requires_no_armor, bool):
        raise ContentError(f"{label}: defense_floor 'requires_no_armor' must be true or false")
    requires_no_shield = value.get("requires_no_shield", False)
    if not isinstance(requires_no_shield, bool):
        raise ContentError(f"{label}: defense_floor 'requires_no_shield' must be true or false")
    if not counter and ("divisor" in value or "round_up" in value):
        raise ContentError(f"{label}: defense_floor division needs a 'counter'")
    return DefenseFloor(
        base=base,
        counter=counter,
        divisor=divisor,
        round_up=round_up,
        requires_no_armor=requires_no_armor,
        requires_no_shield=requires_no_shield,
    )


def _feature_effects(
    ops: object,
    label: str,
    stats: Collection[str],
    skills: Collection[str],
    counters: Collection[str],
) -> tuple[Mapping[str, Any], ...]:
    """The effects a feature runs when it is granted: objects with an `op` from
    FEATURE_EFFECT_OPS, in the order they run."""
    if not isinstance(ops, list):
        raise ContentError(f"{label}: 'effects' must be a list")
    known = {"stat": stats, "skill": skills, "counter": counters}
    checked: list[Mapping[str, Any]] = []
    for op in ops:
        if not isinstance(op, Mapping):
            raise ContentError(f"{label}: each effect must be an object")
        name = required_text(op, "op", label)
        if name not in FEATURE_EFFECT_OPS:
            raise ContentError(
                f"{label}: effect op {name!r} must be one of {', '.join(FEATURE_EFFECT_OPS)}"
            )
        if name == "note":
            required_text(op, "text", label)
        elif name == "armor":
            _effect_delta(op, label)
        else:
            ident = required_text(op, "id", label)
            _effect_delta(op, label)
            if name == "stat" and "cap" in op:
                cap = op["cap"]
                if not isinstance(cap, int) or isinstance(cap, bool):
                    raise ContentError(f"{label}: a stat effect 'cap' must be a whole number")
            if known[name] and ident not in known[name]:
                raise ContentError(f"{label}: effect op {name!r} names no {name} ({ident!r})")
        checked.append(op)
    return tuple(checked)


def _feature_reactions(entries: object, label: str) -> tuple[Reaction, ...]:
    """Read a feature's reactions. The trigger-specific filters and effect shape are checked
    here; the band a `raise_band` names is checked once the rule set's moves are loaded."""
    if not isinstance(entries, list):
        raise ContentError(f"{label}: 'reactions' must be a list")
    checked: list[Reaction] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ContentError(f"{label}: each reaction must be an object")
        _check_reaction_keys(
            entry,
            {
                "id",
                "trigger",
                "when",
                "effect",
                "uses",
                "reset",
                "text",
                "sheet",
                "optional",
                "shared",
            },
            f"{label}: reaction",
        )
        ident = required_text(entry, "id", f"{label}: reaction")
        if ":" in ident:
            raise ContentError(f"{label}: reaction id must not contain ':'")
        if ident in seen:
            raise ContentError(f"{label}: reaction id {ident!r} is repeated")
        seen.add(ident)
        trigger = required_text(entry, "trigger", f"{label}: reaction {ident}")
        if trigger not in REACTION_TRIGGERS:
            raise ContentError(
                f"{label}: reaction {ident} trigger must be one of {', '.join(REACTION_TRIGGERS)}"
            )
        when = _reaction_when(entry.get("when", {}), trigger, f"{label}: reaction {ident}")
        effect = _reaction_effect(entry.get("effect"), trigger, f"{label}: reaction {ident}")
        uses = entry.get("uses", 0)
        if isinstance(uses, bool) or not isinstance(uses, int) or uses < 0:
            raise ContentError(
                f"{label}: reaction {ident} 'uses' must be a whole number, 0 or more"
            )
        reset = entry.get("reset", "")
        if not isinstance(reset, str):
            raise ContentError(f"{label}: reaction {ident} 'reset' must be text")
        if uses and not reset.strip():
            raise ContentError(f"{label}: reaction {ident} needs a 'reset' when it has 'uses'")
        if ":" in reset:
            raise ContentError(f"{label}: reaction {ident} 'reset' must not contain ':'")
        text = entry.get("text", "")
        if not isinstance(text, str):
            raise ContentError(f"{label}: reaction {ident} 'text' must be text")
        sheet = entry.get("sheet", "")
        if not isinstance(sheet, str):
            raise ContentError(f"{label}: reaction {ident} 'sheet' must be text")
        optional = entry.get("optional", False)
        if not isinstance(optional, bool):
            raise ContentError(f"{label}: reaction {ident} 'optional' must be true or false")
        shared = entry.get("shared", "")
        if not isinstance(shared, str):
            raise ContentError(f"{label}: reaction {ident} 'shared' must be text")
        if ":" in shared:
            raise ContentError(f"{label}: reaction {ident} 'shared' must not contain ':'")
        checked.append(
            Reaction(
                id=ident,
                trigger=trigger,
                when=when,
                effect=effect,
                uses=uses,
                reset=reset,
                text=text,
                sheet=sheet,
                optional=optional,
                shared=shared,
            )
        )
    return tuple(checked)


def _reaction_when(data: object, trigger: str, label: str) -> Mapping[str, Any]:
    """Read the filters a reaction applies to a roll. Every trigger understands the total
    bounds; each also understands the fields of the roll it watches."""
    if not isinstance(data, Mapping):
        raise ContentError(f"{label}: 'when' must be an object")
    allowed = {"min_total", "max_total"}
    if trigger == "move":
        allowed |= {"moves", "move_tags", "bands", "stats"}
    elif trigger == "check":
        allowed |= {"checks", "skills", "stats", "success", "cause_contains", "skill_choice"}
    elif trigger == "save":
        allowed |= {"stats", "success", "cause_contains"}
    elif trigger in ("attack", "damage"):
        allowed |= {"cause_contains"}
    _check_reaction_keys(data, allowed, f"{label}: when")
    checked: dict[str, Any] = {}
    for key in ("moves", "move_tags", "bands", "stats", "checks", "skills", "cause_contains"):
        if key in data:
            values = _text_list(data[key], f"when.{key}", label)
            if not values:
                raise ContentError(f"{label}: 'when.{key}' must not be empty")
            checked[key] = values
    for key in ("min_total", "max_total"):
        if key in data:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContentError(f"{label}: 'when.{key}' must be a whole number")
            checked[key] = value
    if "success" in data:
        if not isinstance(data["success"], bool):
            raise ContentError(f"{label}: 'when.success' must be true or false")
        checked["success"] = data["success"]
    if "skill_choice" in data:
        checked["skill_choice"] = required_text(data, "skill_choice", f"{label}: when")
    return checked


def _reaction_effect(data: object, trigger: str, label: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ContentError(f"{label}: 'effect' must be an object")
    op = required_text(data, "op", f"{label}: effect")
    if op not in REACTION_EFFECT_OPS:
        raise ContentError(
            f"{label}: effect op {op!r} must be one of {', '.join(REACTION_EFFECT_OPS)}"
        )
    if op == "raise_band":
        if trigger != "move":
            raise ContentError(f"{label}: raise_band only applies to a move")
        _check_reaction_keys(data, {"op", "to"}, f"{label}: effect")
        required_text(data, "to", f"{label}: effect")
    elif op == "add_total":
        _check_reaction_keys(data, {"op", "amount"}, f"{label}: effect")
        amount = data.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
            raise ContentError(f"{label}: add_total 'amount' must be a non-zero whole number")
    elif op == "set_reaction":
        if trigger != "reaction":
            raise ContentError(f"{label}: set_reaction only applies to a reaction roll")
        _check_reaction_keys(data, {"op", "to"}, f"{label}: effect")
        required_text(data, "to", f"{label}: effect")
    elif op == "reroll_ones":
        if trigger != "check":
            raise ContentError(f"{label}: reroll_ones only applies to a check")
        _check_reaction_keys(data, {"op"}, f"{label}: effect")
    elif op == "best_of":
        if trigger != "check":
            raise ContentError(f"{label}: best_of only applies to a check")
        _check_reaction_keys(data, {"op", "dice", "keep"}, f"{label}: effect")
        dice = data.get("dice")
        keep = data.get("keep")
        if not isinstance(dice, int) or isinstance(dice, bool) or dice < 2:
            raise ContentError(f"{label}: best_of 'dice' must be a whole number of 2 or more")
        if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep < dice:
            raise ContentError(f"{label}: best_of 'keep' must be between 1 and 'dice' - 1")
    else:
        if trigger not in ("check", "save", "attack", "damage", "reaction", "move"):
            raise ContentError(
                f"{label}: reroll only applies to a check, save, attack, damage, reaction, "
                "or move roll"
            )
        _check_reaction_keys(data, {"op", "keep"}, f"{label}: effect")
        if data.get("keep", "better") not in ("better", "worse"):
            raise ContentError(f"{label}: reroll 'keep' must be 'better' or 'worse'")
    return dict(data)


def _check_reaction_keys(data: Mapping[str, Any], allowed: Collection[str], label: str) -> None:
    unknown = sorted(set(data) - set(allowed), key=str)
    if unknown:
        names = ", ".join(repr(key) for key in unknown)
        raise ContentError(f"{label}: unknown key(s) {names}")


def _effect_delta(op: Mapping[str, Any], label: str) -> int:
    delta = op.get("delta")
    if not isinstance(delta, int) or isinstance(delta, bool):
        raise ContentError(f"{label}: each effect needs a whole-number 'delta'")
    return delta


def reaction_effect_text(reaction: Reaction, feature: Feature) -> str:
    """What the log says when a reaction fires, in the rule set's own words when it supplies
    `text`, and otherwise a short sentence naming the feature and its effect."""
    if reaction.text:
        return reaction.text
    effect = reaction.effect
    if effect["op"] == "raise_band":
        return f"{feature.name} raises the result to {effect['to']}."
    if effect["op"] == "add_total":
        return f"{feature.name} adds {effect['amount']:+d} to the total."
    if effect["op"] == "set_reaction":
        return f"{feature.name} makes the reaction {effect['to']}."
    if effect["op"] == "reroll_ones":
        return f"{feature.name} rerolls 1s on the check."
    if effect["op"] == "best_of":
        return f"{feature.name} rolls {effect['dice']} dice and keeps the best {effect['keep']}."
    return f"{feature.name} rerolls the check."


def rule_ids(data: Mapping[str, Any], label: str) -> tuple[str, ...]:
    """The rule ids attached to an entity: a list of text, or none."""
    rules = data.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(r, str) and r.strip() for r in rules):
        raise ContentError(f"{label}: 'rules' must be a list of rule ids")
    if len(set(rules)) != len(rules):
        raise ContentError(f"{label}: 'rules' names a rule more than once")
    return tuple(rules)


def _field(definition: Definition, tables: Mapping[str, object], kind: str, template: str) -> Field:
    label = f"{template}: {kind} {definition.id}"
    table = definition.data.get("table")
    if table is not None:
        table = required_text(definition.data, "table", label)
        if table not in tables:
            raise ContentError(f"{label}: unknown table {table!r}")
    description = definition.data.get("description", "")
    if not isinstance(description, str):
        raise ContentError(f"{label}: 'description' must be text")
    group_roll = definition.data.get("group_roll", False)
    if not isinstance(group_roll, bool):
        raise ContentError(f"{label}: 'group_roll' must be true or false")
    if group_roll and table is None:
        raise ContentError(f"{label}: 'group_roll' needs a 'table'")
    knows = definition.data.get("knows")
    if knows is not None and (not isinstance(knows, str) or table is None):
        raise ContentError(f"{label}: 'knows' needs a table and the name of a feature type")
    return Field(
        id=definition.id,
        name=required_text(definition.data, "name", label),
        description=description,
        table=table,
        group_roll=group_roll,
        restriction=_restriction(definition.data.get("restricted_by"), label),
        knows=knows,
    )


def _restriction(data: object, label: str) -> Restriction | None:
    if data is None:
        return None
    if not isinstance(data, dict) or set(data) != {"field", "values", "groups"}:
        raise ContentError(f"{label}: 'restricted_by' needs 'field', 'values', and 'groups'")
    values, groups = data["values"], data["groups"]
    for key, items in (("values", values), ("groups", groups)):
        if not isinstance(items, list) or not items or not all(isinstance(i, str) for i in items):
            raise ContentError(f"{label}: 'restricted_by' {key} must be a non-empty list of text")
    return Restriction(
        field_id=required_text(data, "field", label),
        values=tuple(values),
        groups=tuple(groups),
    )


def _template(
    definition: Definition,
    features: Mapping[str, Feature],
    tables: Mapping[str, Sequence[TableOption]],
    skills: Mapping[str, Skill],
    skill_progression: SkillProgression | None,
    template: str,
    defaults: Mapping[str, tuple[Granted, ...]] | None = None,
    feature_types: Mapping[str, FeatureTypeRules] | None = None,
) -> Template:
    label = f"{template}: template {definition.id}"
    data = definition.data
    if "classes" in data:
        raise ContentError(f"{label}: use 'compatible_with' instead of 'classes'")
    granted = data.get("features", [])
    if not isinstance(granted, list):
        raise ContentError(f"{label}: 'features' must be a list")
    kind = required_text(data, "kind", label)
    grants = _with_default_grants(
        tuple(_granted(item, features, label) for item in granted),
        (defaults or {}).get(kind, ()),
    )
    table_ids = data.get("tables", [])
    if not isinstance(table_ids, list) or not all(isinstance(t, str) for t in table_ids):
        raise ContentError(f"{label}: 'tables' must be a list of table ids")
    for table in table_ids:
        if table not in tables:
            raise ContentError(f"{label}: unknown table {table!r}")
    names = _names(data.get("names"), tables, label)
    choice_key = data.get("choice_key", definition.id)
    if not isinstance(choice_key, str) or not choice_key.strip():
        raise ContentError(f"{label}: 'choice_key' must be non-empty text")
    return Template(
        id=definition.id,
        kind=kind,
        name=required_text(data, "name", label),
        description=required_text(data, "description", label),
        features=grants,
        tables=tuple(table_ids),
        names=names,
        stats=_amounts(data.get("stats", {}), "stats", label),
        counters=_amounts(data.get("counters", {}), "counters", label, minimum=0),
        choose=_choose(data.get("choose"), label, feature_types),
        skills=_skill_grants(data.get("skills", {}), skills, label),
        skill_picks=_skill_picks(
            data.get("skill_picks", []), tables, skills, skill_progression, label
        ),
        feat_picks=_feat_picks(data.get("feat_picks", []), label, feature_types),
        compatible_with=_template_compatibility(data.get("compatible_with", {}), label),
        template_requirements=_template_requirements(data.get("template_requirements", []), label),
        choice_key=choice_key,
        grant=_template_grant(data.get("grant"), label),
    )


def _template_grant(value: object, label: str) -> TemplateGrant:
    """The items, money, and equipment a template hands over at creation."""
    if value is None:
        return TemplateGrant()
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'grant' must be an object")
    unknown = sorted(set(value) - {"items", "silver", "equip"})
    if unknown:
        raise ContentError(f"{label}: 'grant' has unknown keys: {', '.join(unknown)}")
    silver = value.get("silver", 0)
    if not isinstance(silver, int) or isinstance(silver, bool) or silver < 0:
        raise ContentError(f"{label}: 'grant.silver' must be a whole number of 0 or more")
    return TemplateGrant(
        items=_text_items(value.get("items", []), f"{label}: 'grant.items'"),
        silver=silver,
        equip=_text_items(value.get("equip", []), f"{label}: 'grant.equip'"),
    )


def _text_items(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ContentError(f"{label} must be a list of item names")
    return tuple(item.strip() for item in value)


def load_starting_silver(value: object, label: str) -> StartingSilver | None:
    """Money rolled instead of choosing a template of `kind`, such as a starting kit."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'starting_silver' must be an object")
    unknown = sorted(set(value) - {"kind", "label", "description", "dice", "multiplier"})
    if unknown:
        raise ContentError(f"{label}: 'starting_silver' has unknown keys: {', '.join(unknown)}")
    kind = required_text(value, "kind", f"{label}: starting_silver")
    dice = required_text(value, "dice", f"{label}: starting_silver")
    if not is_formula(dice) or "+" in dice or "-" in dice:
        raise ContentError(f"{label}: starting_silver 'dice' must be one dice term such as '3d6'")
    multiplier = value.get("multiplier", 1)
    if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier < 1:
        raise ContentError(f"{label}: starting_silver 'multiplier' must be a whole number")
    label_text = value.get("label", "Starting silver")
    description = value.get("description", "")
    for key, text in (("label", label_text), ("description", description)):
        if not isinstance(text, str):
            raise ContentError(f"{label}: starting_silver {key!r} must be text")
    return StartingSilver(
        kind=kind,
        label=label_text.strip() or "Starting silver",
        description=description.strip(),
        dice=dice,
        multiplier=multiplier,
    )


# The feature picks one compact `starting_years` entry expands into, in the order the creation
# form shows them.
STARTING_YEAR_PICKS = ("subjects", "spells", "potions", "enchantments", "training")
STARTING_YEAR_KEYS = frozenset({"subjects", "forbidden_spells", "years"})
STARTING_YEAR_ENTRY_KEYS = frozenset({"description", *STARTING_YEAR_PICKS})


def load_starting_years(
    value: object, label: str, existing: Collection[str] = ()
) -> list[Definition]:
    """The `starting_year` templates a rule set head's `starting_years` block describes.

    One compact entry per year expands into the template a rule set would otherwise write out
    by hand: a subject pick, its spells limited to those subjects, and the potion, enchantment,
    and training picks. A year whose id an explicit template already uses is left to it."""
    if value is None:
        return []
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'starting_years' must be an object")
    block = f"{label}: starting_years"
    unknown = sorted(set(value) - STARTING_YEAR_KEYS)
    if unknown:
        raise ContentError(f"{block} has unknown keys: {', '.join(unknown)}")
    subjects = _text_ids(value.get("subjects"), "subjects", block)
    if not subjects:
        raise ContentError(f"{block}: 'subjects' must not be empty")
    forbidden = _text_ids(value.get("forbidden_spells"), "forbidden_spells", block)
    if not forbidden:
        raise ContentError(f"{block}: 'forbidden_spells' must not be empty")
    years = value.get("years")
    if not isinstance(years, list) or not years:
        raise ContentError(f"{block}: 'years' must be a non-empty list")
    definitions: list[Definition] = []
    for number, entry in enumerate(years, start=1):
        year_label = f"{block}: year {number}"
        ident = f"year-{number}"
        if not isinstance(entry, Mapping):
            raise ContentError(f"{year_label} must be an object")
        unknown = sorted(set(entry) - STARTING_YEAR_ENTRY_KEYS)
        if unknown:
            raise ContentError(f"{year_label} has unknown keys: {', '.join(unknown)}")
        counts = {
            key: _starting_year_count(entry.get(key, 0), key, year_label)
            for key in STARTING_YEAR_PICKS
        }
        if counts["spells"] and not counts["subjects"]:
            raise ContentError(f"{year_label}: 'spells' needs a 'subjects' count of 1 or more")
        description = required_text(entry, "description", year_label)
        if ident in existing:
            continue
        definitions.append(
            Definition(
                type="template",
                id=ident,
                data={
                    "type": "template",
                    "id": ident,
                    "kind": "starting_year",
                    "name": f"Year {number}",
                    "description": description,
                    "counters": {"year": number - 1},
                    "feat_picks": _starting_year_picks(number, counts, subjects, forbidden),
                },
            )
        )
    return definitions


def _starting_year_count(value: object, key: str, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContentError(f"{label}: {key!r} must be a whole number, 0 or more")
    return value


def _starting_year_picks(
    number: int,
    counts: Mapping[str, int],
    subjects: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> list[dict[str, Any]]:
    """The feature picks one starting year expands to. A count of zero omits its pick."""
    picks: list[dict[str, Any]] = []
    if counts["subjects"]:
        picks.append(
            {
                "id": "subjects",
                "count": counts["subjects"],
                "tags": list(subjects),
                "feature_type": "subject",
            }
        )
    if counts["spells"]:
        picks.append(
            {
                "id": "spells",
                "count": counts["spells"],
                "tags": list(subjects),
                "tags_from": "subjects",
                "feature_type": "spell",
                "exclude": list(forbidden),
            }
        )
    if counts["potions"]:
        picks.append(
            {
                "id": "potions",
                "count": counts["potions"],
                "tag": "Potions",
                "feature_type": "potion",
            }
        )
    if counts["enchantments"]:
        picks.append(
            {
                "id": "enchantments",
                "count": counts["enchantments"],
                "tag": "Enchantments",
                "feature_type": "enchantment",
            }
        )
    if counts["training"]:
        picks.append(
            {
                "id": "training",
                "count": counts["training"],
                "tag": _training_tag(number),
                "feature_type": "trait",
            }
        )
    return picks


def _training_tag(number: int) -> str:
    """The trait tag a starting year draws its training picks from."""
    if number <= 3:
        return "training-1-3"
    if number <= 6:
        return "training-4-6"
    return "training-7"


def _template_compatibility(value: object, label: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'compatible_with' must map kinds to template ids")
    result: dict[str, tuple[str, ...]] = {}
    for kind, ids in value.items():
        if not isinstance(kind, str) or not kind.strip():
            raise ContentError(f"{label}: 'compatible_with' kinds must be non-empty text")
        allowed = _text_ids(ids, "compatible_with", label)
        if not allowed:
            raise ContentError(f"{label}: 'compatible_with' needs at least one {kind!r} template")
        result[kind] = allowed
    return result


def _template_requirements(value: object, label: str) -> tuple[TemplateRequirement, ...]:
    if not isinstance(value, list):
        raise ContentError(f"{label}: 'template_requirements' must be a list")
    requirements: list[TemplateRequirement] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ContentError(f"{label}: each template requirement must be an object")
        unknown = set(entry) - {"kinds", "distinct", "missing_message", "duplicate_message"}
        if unknown:
            raise ContentError(f"{label}: unknown template requirement fields {sorted(unknown)}")
        kinds = _text_ids(entry.get("kinds"), "kinds", label)
        if not kinds:
            raise ContentError(f"{label}: a template requirement needs at least one kind")
        distinct = entry.get("distinct", False)
        if not isinstance(distinct, bool):
            raise ContentError(f"{label}: template requirement 'distinct' must be a boolean")
        if distinct and len(kinds) < 2:
            raise ContentError(f"{label}: distinct selections need at least two kinds")
        messages = {}
        for key in ("missing_message", "duplicate_message"):
            messages[key] = required_text(entry, key, label) if key in entry else None
        requirements.append(
            TemplateRequirement(
                kinds=kinds,
                distinct=distinct,
                missing_message=messages["missing_message"],
                duplicate_message=messages["duplicate_message"],
            )
        )
    return tuple(requirements)


def _skill_grants(value: object, skills: Mapping[str, Skill], label: str) -> dict[str, int]:
    """Skill levels a template adds to the unskilled base, by skill id."""
    grants = _amounts(value, "skills", label, minimum=1)
    for skill_id in grants:
        if skill_id not in skills:
            raise ContentError(f"{label}: 'skills' names no skill ({skill_id!r})")
    return grants


def _skill_picks(
    value: object,
    tables: Mapping[str, Sequence[TableOption]],
    skills: Mapping[str, Skill],
    progression: SkillProgression | None,
    label: str,
) -> tuple[SkillPick, ...]:
    """The skills a template lets the character pick from a table. Every option value the pick
    does not exclude must be a skill id."""
    if not isinstance(value, list):
        raise ContentError(f"{label}: 'skill_picks' must be a list")
    picks: list[SkillPick] = []
    ids: set[str] = set()
    for item in value:
        pick_label = f"{label}: skill pick"
        if progression is None:
            raise ContentError(f"{pick_label}: the rule set has no skill progression")
        if not isinstance(item, Mapping):
            raise ContentError(f"{pick_label}: must be an object")
        ident = required_text(item, "id", pick_label)
        if ident in ids:
            raise ContentError(f"{label}: skill pick {ident!r} is listed twice")
        ids.add(ident)
        count = item.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ContentError(f"{pick_label} {ident!r}: 'count' must be a whole number, 1 or more")
        table = required_text(item, "table", pick_label)
        if table not in tables:
            raise ContentError(f"{pick_label} {ident!r}: unknown table {table!r}")
        exclude = _text_ids(item.get("exclude", []), "exclude", pick_label)
        name = item.get("label", "Skills")
        if not isinstance(name, str) or not name.strip():
            raise ContentError(f"{pick_label} {ident!r}: 'label' must be nonempty text")
        max_level = item.get("max_level", progression.creation_maximum)
        if (
            isinstance(max_level, bool)
            or not isinstance(max_level, int)
            or not progression.first_level <= max_level <= progression.creation_maximum
        ):
            raise ContentError(
                f"{pick_label} {ident!r}: 'max_level' must be between "
                f"{progression.first_level} and {progression.creation_maximum}"
            )
        for option in tables[table]:
            if option.value in exclude:
                continue
            if option.value not in skills:
                raise ContentError(
                    f"{pick_label} {ident!r}: table {table!r} value {option.value!r} is no skill"
                )
        picks.append(
            SkillPick(
                id=ident,
                count=count,
                table=table,
                exclude=exclude,
                max_level=max_level,
                label=name.strip(),
            )
        )
    return tuple(picks)


def _feat_picks(
    value: object,
    label: str,
    feature_types: Mapping[str, FeatureTypeRules] | None = None,
) -> tuple[FeatPick, ...]:
    """The features a template lets the character pick by tag and rank."""
    allowed_types = feature_types or DEFAULT_FEATURE_TYPES
    if not isinstance(value, list):
        raise ContentError(f"{label}: 'feat_picks' must be a list")
    picks: list[FeatPick] = []
    ids: set[str] = set()
    order: list[str] = []
    for item in value:
        pick_label = f"{label}: feature pick"
        if not isinstance(item, Mapping):
            raise ContentError(f"{pick_label}: must be an object")
        ident = required_text(item, "id", pick_label)
        if ident in ids:
            raise ContentError(f"{label}: feature pick {ident!r} is listed twice")
        ids.add(ident)
        order.append(ident)
        count = item.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ContentError(f"{pick_label} {ident!r}: 'count' must be a whole number, 1 or more")
        tag = item.get("tag", "")
        if not isinstance(tag, str):
            raise ContentError(f"{pick_label} {ident!r}: 'tag' must be text")
        tags = _text_ids(item.get("tags", []), "tags", pick_label)
        if not tag and not tags:
            raise ContentError(f"{pick_label} {ident!r}: give a 'tag' or 'tags'")
        if tag and tags:
            raise ContentError(f"{pick_label} {ident!r}: give 'tag' or 'tags', not both")
        feature_type = item.get("feature_type", "")
        if not isinstance(feature_type, str):
            raise ContentError(f"{pick_label} {ident!r}: 'feature_type' must be text")
        if feature_type and feature_type not in allowed_types:
            raise ContentError(
                f"{pick_label} {ident!r}: 'feature_type' must be one of {', '.join(allowed_types)}"
            )
        tags_from = item.get("tags_from", "")
        if not isinstance(tags_from, str):
            raise ContentError(f"{pick_label} {ident!r}: 'tags_from' must be text")
        if tags_from and tags_from not in order[:-1]:
            raise ContentError(f"{pick_label} {ident!r}: 'tags_from' must name an earlier pick")
        # A unique feature, such as a spell, is learned once unless the pick says otherwise.
        unique_default = (
            allowed_types[feature_type].unique if feature_type in allowed_types else False
        )
        unique = item.get("unique", unique_default)
        if not isinstance(unique, bool):
            raise ContentError(f"{pick_label} {ident!r}: 'unique' must be boolean")
        max_rank = item.get("max_rank")
        if max_rank is not None and (
            isinstance(max_rank, bool) or not isinstance(max_rank, int) or max_rank < 0
        ):
            raise ContentError(
                f"{pick_label} {ident!r}: 'max_rank' must be a whole number, 0 or more"
            )
        advance_repeated_rank = item.get("advance_repeated_rank", False)
        if not isinstance(advance_repeated_rank, bool):
            raise ContentError(f"{pick_label} {ident!r}: 'advance_repeated_rank' must be boolean")
        picks.append(
            FeatPick(
                id=ident,
                count=count,
                tag=tag,
                max_rank=max_rank,
                exclude=_text_ids(item.get("exclude", []), "exclude", pick_label),
                tags=tags,
                feature_type=feature_type,
                tags_from=tags_from,
                unique=unique,
                advance_repeated_rank=advance_repeated_rank,
            )
        )
    return tuple(picks)


def _text_ids(value: object, key: str, label: str) -> tuple[str, ...]:
    """A list of ids given as text, with no repeats."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ContentError(f"{label}: {key!r} must be a list of ids")
    if len(set(value)) != len(value):
        raise ContentError(f"{label}: {key!r} repeats an id")
    return tuple(value)


def _choose(
    data: object,
    label: str,
    feature_types: Mapping[str, FeatureTypeRules] | None = None,
) -> Choose | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ContentError(f"{label}: 'choose' must be an object")
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ContentError(f"{label}: 'choose' count must be a whole number, 1 or more")
    kind = required_text(data, "feature_type", f"{label}: 'choose'")
    allowed_types = feature_types or DEFAULT_FEATURE_TYPES
    if kind not in allowed_types:
        raise ContentError(
            f"{label}: 'choose' feature_type must be one of {', '.join(allowed_types)}"
        )
    tag_field = required_text(data, "tag_field", f"{label}: 'choose'")
    skip_tags = _text_ids(data.get("skip_tags", []), "skip_tags", f"{label}: 'choose'")
    return Choose(count=count, feature_type=kind, tag_field=tag_field, skip_tags=skip_tags)


def _amounts(value: object, key: str, label: str, minimum: int | None = None) -> dict[str, int]:
    """A map of ids to whole numbers, such as a template's stat bonuses."""
    if not isinstance(value, dict):
        raise ContentError(f"{label}: {key!r} must map ids to whole numbers")
    amounts: dict[str, int] = {}
    for item_id, amount in value.items():
        if not isinstance(item_id, str) or not isinstance(amount, int) or isinstance(amount, bool):
            raise ContentError(f"{label}: {key!r} must map ids to whole numbers")
        if minimum is not None and amount < minimum:
            raise ContentError(f"{label}: {key!r} amounts must be at least {minimum}")
        amounts[item_id] = amount
    return amounts


def _names(
    table: object, tables: Mapping[str, Sequence[TableOption]], label: str
) -> tuple[str, ...]:
    """The names a template suggests, from a table of plain names. The table is not a choice."""
    if table is None:
        return ()
    if not isinstance(table, str) or table not in tables:
        raise ContentError(f"{label}: 'names' must be the id of a table")
    return tuple(option.value for option in tables[table])


def _with_default_grants(
    own: tuple[Granted, ...], defaults: tuple[Granted, ...]
) -> tuple[Granted, ...]:
    """A template's own grants with its kind's defaults put first. A default whose feature the
    template grants itself is dropped, so the template's grant replaces it rather than repeating
    it."""
    own_features = {grant.feature for grant in own}
    return (*(grant for grant in defaults if grant.feature not in own_features), *own)


def apply_stat_presets(
    definitions: list[Definition], value: object, label: str
) -> list[Definition]:
    """Expand stat `preset` references from the rule-set head before the character spec is
    built. A preset supplies shared stat fields; the stat's own fields win."""
    if value is None:
        return definitions
    if not isinstance(value, Mapping) or not value:
        raise ContentError(f"{label}: 'stat_presets' must map names to stat fields")
    presets: dict[str, dict[str, object]] = {}
    for name, fields in value.items():
        preset_label = f"{label}: stat preset {name!r}"
        if not isinstance(name, str) or not name.strip():
            raise ContentError(f"{label}: 'stat_presets' names must be non-empty text")
        if not isinstance(fields, Mapping) or not fields:
            raise ContentError(f"{preset_label}: a preset must be a non-empty object")
        for key in ("type", "id", "name", "preset"):
            if key in fields:
                raise ContentError(f"{preset_label}: a preset cannot set {key!r}")
        presets[name] = dict(fields)
    expanded: list[Definition] = []
    for definition in definitions:
        if definition.type != "stat" or "preset" not in definition.data:
            expanded.append(definition)
            continue
        name = definition.data["preset"]
        if not isinstance(name, str) or not name.strip():
            raise ContentError(f"{label}: stat {definition.id!r} 'preset' must be text")
        if name not in presets:
            raise ContentError(f"{label}: stat {definition.id!r} names no preset ({name!r})")
        data = {**presets[name], **{k: v for k, v in definition.data.items() if k != "preset"}}
        expanded.append(Definition(definition.type, definition.id, data))
    return expanded


def load_training_features(
    value: object, definitions: list[Definition], label: str
) -> list[Definition]:
    """Expand a `training` head block into the tiered training features it describes. A
    feature definition with `generate: "training"` marks where the generated features go;
    without a marker they are appended."""
    markers = [
        d for d in definitions if d.type == "feature" and d.data.get("generate") == "training"
    ]
    if value is None:
        if markers:
            raise ContentError(f"{label}: a generate 'training' feature needs a 'training' block")
        return definitions
    training_label = f"{label}: training"
    if not isinstance(value, Mapping):
        raise ContentError(f"{training_label}: must be an object")
    unknown = set(value) - {"stats", "tiers"}
    if unknown:
        raise ContentError(f"{training_label} has unknown keys: {', '.join(sorted(unknown))}")
    stats = value.get("stats")
    if not isinstance(stats, Mapping) or not stats:
        raise ContentError(f"{training_label}: 'stats' must map stat ids to text")
    tiers = value.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise ContentError(f"{training_label}: 'tiers' must be a non-empty list")
    stat_names = {
        d.id: d.data.get("name")
        for d in definitions
        if d.type == "stat" and isinstance(d.data.get("name"), str)
    }
    generated: list[Definition] = []
    used = {d.id for d in definitions if d.type == "feature"}
    for index, tier in enumerate(tiers, 1):
        tier_label = f"{training_label}: tier {index}"
        if not isinstance(tier, Mapping):
            raise ContentError(f"{tier_label}: must be an object")
        unknown = set(tier) - {"tag", "name", "prefix", "cap"}
        if unknown:
            raise ContentError(f"{tier_label} has unknown keys: {', '.join(sorted(unknown))}")
        tag = required_text(tier, "tag", tier_label)
        name = required_text(tier, "name", tier_label)
        prefix = required_text(tier, "prefix", tier_label)
        cap = tier.get("cap")
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            raise ContentError(f"{tier_label}: 'cap' must be a whole number, zero or more")
        for stat, phrase in stats.items():
            stat_label = f"{training_label}: stat {stat!r}"
            if not isinstance(stat, str) or not stat.strip():
                raise ContentError(f"{training_label}: 'stats' ids must be non-empty text")
            if not isinstance(phrase, str) or not phrase.strip():
                raise ContentError(f"{stat_label}: the phrase must be non-empty text")
            stat_name = stat_names.get(stat)
            if stat_name is None:
                raise ContentError(f"{stat_label}: names no stat")
            feature_id = f"training-{stat}-{index}"
            if feature_id in used and not markers:
                continue
            generated.append(
                Definition(
                    "feature",
                    feature_id,
                    {
                        "type": "feature",
                        "id": feature_id,
                        "name": f"{stat_name} Training ({name})",
                        "feature_type": "trait",
                        "tag": tag,
                        "description": (
                            f"{prefix} {phrase}. Raise {stat_name} by 1, up to +{cap}."
                        ),
                        "effects": [{"op": "stat", "id": stat, "delta": 1, "cap": cap}],
                    },
                )
            )
    if not markers:
        return [*definitions, *generated]
    expanded: list[Definition] = []
    placed = False
    for definition in definitions:
        if definition.type == "feature" and definition.data.get("generate") == "training":
            if placed:
                continue
            expanded.extend(generated)
            placed = True
        else:
            expanded.append(definition)
    return expanded


def _template_features(
    value: object, features: Mapping[str, Feature], label: str
) -> dict[str, tuple[Granted, ...]]:
    """The rule set's default feature grants, by template kind. Every template of a kind begins
    with them; its own grants follow, replacing any default it grants itself."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'template_features' must map kinds to feature grants")
    defaults: dict[str, tuple[Granted, ...]] = {}
    for kind, grants in value.items():
        if not isinstance(kind, str) or not kind.strip():
            raise ContentError(f"{label}: 'template_features' kinds must be non-empty text")
        if not isinstance(grants, list):
            raise ContentError(
                f"{label}: 'template_features' {kind!r} must be a list of feature grants"
            )
        defaults[kind] = tuple(
            _granted(grant, features, f"{label}: template_features {kind!r}") for grant in grants
        )
    return defaults


def _granted(item: object, features: Mapping[str, Feature], label: str) -> Granted:
    """A grant is a feature id, or an object naming a feature and a quantity other than one."""
    if isinstance(item, str):
        item = {"feature": item}
    if not isinstance(item, dict):
        raise ContentError(f"{label}: each feature must be an id or an object")
    feature = required_text(item, "feature", label)
    if feature not in features:
        raise ContentError(f"{label}: unknown feature {feature!r}")
    quantity = item.get("quantity", "1")
    if isinstance(quantity, int) and not isinstance(quantity, bool):
        quantity = str(quantity)
    if not isinstance(quantity, str) or not (_is_count(quantity) or is_formula(quantity)):
        raise ContentError(f"{label}: 'quantity' must be a whole number or a dice formula")
    return Granted(feature=feature, quantity=quantity)


def _is_count(text: str) -> bool:
    return text.isdigit() and text[0] != "0"


def character_schema(spec: CharacterSpec) -> dict[str, Any]:
    """The JSON schema for a character's options. Every part is optional: a GM may give any
    combination, and anything left out is rolled or left blank. A field that takes a table value
    lists its values with `choice_options`, not here; `check_choices` checks them."""
    properties: dict[str, Any] = {}
    rolled = [s for s in spec.stats.values() if s.derived is None]
    if rolled:
        properties["stats"] = _object(
            {
                s.id: {
                    "type": "integer",
                    "minimum": spec.stat_bounds(s.id)[0],
                    "maximum": spec.stat_bounds(s.id)[1],
                    "description": s.name,
                }
                for s in rolled
            }
        )
    if spec.templates:
        properties["templates"] = _object(
            {
                kind: {
                    "type": "string",
                    "enum": [t.id for t in spec.templates.values() if t.kind == kind],
                    "description": f"The {kind} of the character",
                }
                for kind in spec.kinds()
            }
        )
    if spec.features:
        properties["features"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(spec.features)},
            "uniqueItems": True,
            "description": _features_description(spec),
        }
    picks = _feature_pick_schemas(spec)
    if picks:
        properties["feature_picks"] = _object(picks)
    repeat_picks = _feature_repeat_schemas(spec)
    if repeat_picks:
        properties["feature_repeats"] = _object(repeat_picks)
    feature_choices = _feature_choice_schemas(spec)
    if feature_choices:
        properties["feature_choices"] = _object(feature_choices)
    skill_picks = _skill_pick_schemas(spec)
    if skill_picks:
        properties["skill_picks"] = _object(skill_picks)
    progression = spec.skill_progression
    if progression is not None and progression.creation_replacements is not None:
        replacements = progression.creation_replacements
        properties["skill_replacements"] = _object(
            {
                kind: {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["", *(skill for skill in spec.skills if skill not in excluded)],
                    },
                    "description": (
                        f"Skills to use, in order, for excess {kind} grants; "
                        "an empty entry or omitted choice is rolled"
                    ),
                }
                for kind, excluded in (
                    ("roll", replacements.roll_exclude),
                    ("feature", replacements.feature_exclude),
                )
            }
        )
    fields = {
        f.id: {"type": "string", "description": f.description or f.name}
        for f in spec.fields.values()
    }
    for template in spec.templates.values():
        for table in template.tables:
            fields[table] = {"type": "string", "description": spec.table_names.get(table, table)}
    properties["fields"] = _object(fields)
    return {
        "type": "object",
        "description": (
            "Character options. Anything left out is rolled or left blank. A field that takes "
            "a table value must use one of the values from the character_options lookup."
        ),
        "properties": properties,
        "additionalProperties": False,
    }


def _feature_pick_schemas(spec: CharacterSpec) -> dict[str, Any]:
    """The `feature_picks` schema: a pick id to the features it may take. A pick a template
    shares with another keeps every option either offers."""
    choices: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for template in spec.templates.values():
        for pick in template.feat_picks:
            options = choices.setdefault(pick.id, [])
            options.extend(f.id for f in feature_pick_options(spec, pick) if f.id not in options)
            counts[pick.id] = max(counts.get(pick.id, 0), pick.count)
    return {
        pick_id: {
            "type": "array",
            "items": {"type": "string", "enum": options},
            "maxItems": counts[pick_id],
            "uniqueItems": True,
            "description": (
                f"Features the character picks, up to {counts[pick_id]}; remaining picks are rolled"
            ),
        }
        for pick_id, options in choices.items()
    }


def _feature_choice_schemas(spec: CharacterSpec) -> dict[str, Any]:
    """The `feature_choices` schema: `feature:pick` to the choices that pick allows."""
    schemas: dict[str, Any] = {}
    for feature in spec.features.values():
        for pick in feature.picks:
            key = f"{feature.id}:{pick.id}"
            if pick.kind == "text":
                schemas[key] = {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"{feature.name}: {pick.label}",
                }
                continue
            options = list(_feature_choice_options(spec, pick))
            schemas[key] = {
                "type": "array",
                "items": {"type": "string", "enum": options},
                "uniqueItems": True,
                "description": f"{feature.name}: {pick.label}",
            }
    return schemas


def _skill_pick_schemas(spec: CharacterSpec) -> dict[str, Any]:
    """The `skill_picks` schema: a pick id to the skills it may take. A pick a template shares
    with another keeps every option either offers."""
    choices: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for template in spec.templates.values():
        for pick in template.skill_picks:
            options = choices.setdefault(pick.id, [])
            options.extend(
                skill_id
                for skill_id, _label in skill_pick_options(spec, pick)
                if skill_id not in options
            )
            counts[pick.id] = max(counts.get(pick.id, 0), pick.count)
    return {
        pick_id: {
            "type": "array",
            "items": {"type": "string", "enum": options},
            "maxItems": counts[pick_id],
            "description": (
                f"Skills the character picks, up to {counts[pick_id]}; "
                "a skill may be picked again while its level stays within the pick's maximum"
            ),
        }
        for pick_id, options in choices.items()
    }


def _feature_repeat_schemas(spec: CharacterSpec) -> dict[str, Any]:
    choices: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for template in spec.templates.values():
        for pick in template.feat_picks:
            if not pick.advance_repeated_rank:
                continue
            options = {
                feature.id for feature in feature_pick_options(spec, pick) if feature.repeatable
            }
            if options:
                choices.setdefault(pick.id, set()).update(options)
                counts[pick.id] = max(counts.get(pick.id, 0), pick.count)
    return {
        pick_id: {
            "type": "array",
            "items": {"type": "string", "enum": sorted(options)},
            "uniqueItems": True,
            "maxItems": counts[pick_id],
            "description": (
                "Selected repeatable features to take as new choices instead of advancing rank"
            ),
        }
        for pick_id, options in choices.items()
    }


def option_fields(spec: CharacterSpec) -> dict[str, tuple[str, str]]:
    """The fields that take a table value, each with its label and table. A template's table is
    keyed by the table's id, as it is in the character's fields."""
    found = {
        f.id: (f.description or f.name, f.table)
        for f in spec.fields.values()
        if f.table is not None
    }
    for template in spec.templates.values():
        for table in template.tables:
            found[table] = (spec.table_names.get(table, table), table)
    return found


def choice_options(spec: CharacterSpec, field: str | None = None) -> dict[str, Any]:
    """The fields that take a table value when `field` is None. Otherwise, the values that field
    can take, with a description for each option that has a name, and any restriction on them."""
    fields = option_fields(spec)
    if field is None:
        return {"fields": [{"field": key, "name": label} for key, (label, _) in fields.items()]}
    if field not in fields:
        return {"error": f"no field {field!r}; the fields with choices are {', '.join(fields)}"}
    result: dict[str, Any] = {
        "field": field,
        "options": [_option_entry(option) for option in spec.tables[fields[field][1]]],
    }
    restriction = spec.fields[field].restriction if field in spec.fields else None
    if restriction is not None:
        values = ", ".join(repr(value) for value in restriction.values)
        groups = ", ".join(repr(group) for group in restriction.groups)
        result["note"] = (
            f"While {restriction.field_id!r} is {values}, only options in {groups} are allowed."
        )
    return result


def _option_entry(option: TableOption) -> dict[str, str]:
    if option.name is None:
        entry = {"value": option.description}
    else:
        entry = {"value": option.name, "description": option.description}
    if option.group is not None:
        entry["group"] = option.group
    return entry


def allowed_options(
    spec: CharacterSpec, field_spec: Field, fields: Mapping[str, Any]
) -> tuple[TableOption, ...]:
    """The options a field may take. A restriction applies while the field it names takes one of
    its values, or while that field is not yet known, since then it applies whatever that is."""
    options = spec.tables[field_spec.table or ""]
    restriction = field_spec.restriction
    if restriction is None:
        return options
    gate = fields.get(restriction.field_id)
    if gate is not None and gate not in restriction.values:
        return options
    return tuple(option for option in options if option.group in restriction.groups)


def check_choices(spec: CharacterSpec, submitted: Mapping[str, Any]) -> None:
    """Raise CharacterError when a field that takes a table value is given a value that is not
    one of the table's options, or one a restriction rules out, or when a template's picks are
    not features of its tag, or when a skill or feature pick is not one the pick offers.
    `submitted` is the character as given: its fields, templates, features, and picks."""
    _check_templates(spec, submitted)
    _check_field_values(spec, submitted.get("fields", {}))
    _check_picks(spec, submitted)
    _check_skill_picks(spec, submitted)
    _check_feature_picks(spec, submitted)
    _check_feature_repeats(spec, submitted)
    _check_feature_choices(spec, submitted)


def _check_templates(spec: CharacterSpec, submitted: Mapping[str, Any]) -> None:
    """Check template kinds, compatibility, and any required selections."""
    given = submitted.get("templates", {})
    if not isinstance(given, Mapping):
        raise CharacterError("'templates' must map kinds to template ids")
    chosen: dict[str, str] = {}
    for kind, template_id in given.items():
        if not isinstance(kind, str) or not isinstance(template_id, str):
            raise CharacterError("'templates' must map kinds to template ids")
        template = spec.templates.get(template_id)
        if template is None:
            raise CharacterError(f"there is no template {template_id!r}")
        if template.kind != kind:
            raise CharacterError(f"{template.name!r} is not a {kind}")
        chosen[kind] = template_id
    for template_id in chosen.values():
        template = spec.templates[template_id]
        for kind, allowed in template.compatible_with.items():
            selected = chosen.get(kind, "")
            if not selected:
                raise CharacterError(f"{template.name} requires a {kind} choice")
            if selected not in allowed:
                selected_name = (
                    spec.templates[selected].name if selected in spec.templates else selected
                )
                raise CharacterError(
                    f"{template.name} cannot be chosen by a {selected_name} character"
                )
        for requirement in template.template_requirements:
            selected = [chosen[kind] for kind in requirement.kinds if kind in chosen]
            if len(selected) != len(requirement.kinds):
                raise CharacterError(
                    requirement.missing_message
                    or f"{template.name} requires selections for {', '.join(requirement.kinds)}"
                )
            keys = [spec.templates[ident].choice_key for ident in selected]
            if requirement.distinct and len(set(keys)) != len(keys):
                raise CharacterError(
                    requirement.duplicate_message
                    or f"{template.name} requires different selections for "
                    f"{', '.join(requirement.kinds)}"
                )


def _check_picks(spec: CharacterSpec, submitted: Mapping[str, Any]) -> None:
    fields = submitted.get("fields", {})
    given = submitted.get("features", [])
    picks_by_kind: dict[str, list[str]] = {}
    for feature_id in given:
        kind = spec.features[feature_id].feature_type
        picks_by_kind.setdefault(kind, []).append(feature_id)
    groups: dict[str, list[tuple[Template, Choose, str | None]]] = {}
    for template_id in submitted.get("templates", {}).values():
        template = spec.templates[template_id]
        choose = template.choose
        if choose is None or not picks_by_kind.get(choose.feature_type):
            continue
        kind = choose.feature_type
        tag = fields.get(choose.tag_field) or None
        groups.setdefault(kind, []).append((template, choose, tag))
    for kind, choices in groups.items():
        active: dict[str, tuple[Template, Choose]] = {}
        skipped: dict[str, Template] = {}
        missing: list[tuple[Template, Choose]] = []
        for template, choose, tag in choices:
            if tag is None:
                missing.append((template, choose))
            elif tag in choose.skip_tags:
                skipped[tag] = template
            elif tag not in active or choose.count > active[tag][1].count:
                active[tag] = (template, choose)
        expected = " or ".join(
            repr(tag) for tag in dict.fromkeys(tag for _, _, tag in choices if tag is not None)
        )
        for feature_id in picks_by_kind[kind]:
            feature = spec.features[feature_id]
            if feature.tag in active:
                continue
            if missing:
                template, choose = missing[0]
                raise CharacterError(
                    f"give {choose.tag_field!r} to pick the {kind}s for {template.name}"
                )
            if feature.tag in skipped:
                raise CharacterError(
                    f"{skipped[feature.tag].name} does not pick {kind}s for {feature.tag!r}"
                )
            raise CharacterError(f"{feature.name!r} is not a {kind} from {expected}")
        for tag, (template, choose) in active.items():
            count = sum(spec.features[feature].tag == tag for feature in picks_by_kind[kind])
            if count > choose.count:
                raise CharacterError(f"{template.name} picks {choose.count} {kind}s, not {count}")


def _check_skill_picks(spec: CharacterSpec, submitted: Mapping[str, Any]) -> None:
    """The skill ids a submitted `skill_picks` object gives must be offered by the pick of a
    chosen template, and no more may be given than its count allows."""
    given = _pick_object(submitted.get("skill_picks", {}), "skill_picks")
    seen: set[str] = set()
    for template_id in submitted.get("templates", {}).values():
        template = spec.templates.get(template_id)
        if template is None:
            continue
        for pick in template.skill_picks:
            skills = given.get(pick.id, [])
            seen.add(pick.id)
            if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
                raise CharacterError(f"skill pick {pick.id!r} must be a list of skill ids")
            if len(skills) > pick.count:
                raise CharacterError(
                    f"skill pick {pick.id!r} takes {pick.count} skills, not {len(skills)}"
                )
            offered = {skill_id for skill_id, _ in skill_pick_options(spec, pick)}
            for skill_id in skills:
                if skill_id not in offered:
                    raise CharacterError(
                        f"{skill_id!r} is not offered by the skill pick {pick.id!r}"
                    )
    unknown = set(given) - seen
    if unknown:
        raise CharacterError(f"no skill pick named {min(unknown)!r}")


def _check_feature_picks(spec: CharacterSpec, submitted: Mapping[str, Any]) -> None:
    """The feature ids a submitted `feature_picks` object gives must be features of the pick's
    tag and rank, may not repeat, and may not outnumber the pick's count. A feature already
    granted by a template, or already chosen by another pick, may only be taken again when it
    is repeatable or when this pick can advance it to its next rank."""
    given = _pick_object(submitted.get("feature_picks", {}), "feature_picks")
    taken: dict[str, int] = {}
    templates = [
        spec.templates[template_id]
        for template_id in submitted.get("templates", {}).values()
        if template_id in spec.templates
    ]
    for template in templates:
        for granted in template.features:
            taken[granted.feature] = taken.get(granted.feature, 0) + 1
    seen: set[str] = set()
    chosen_by_pick: dict[str, list[str]] = {}
    for template in templates:
        for pick in template.feat_picks:
            features = given.get(pick.id, [])
            seen.add(pick.id)
            allowed_tags = None
            if pick.tags_from and features:
                source = chosen_by_pick.get(pick.tags_from)
                if not source:
                    raise CharacterError(
                        f"feature pick {pick.id!r} needs a choice for {pick.tags_from!r} first"
                    )
                allowed_tags = {spec.features[feature_id].tag for feature_id in source}
            chosen = _selected_feature_pick(spec, pick, features, allowed_tags=allowed_tags)
            chosen_by_pick[pick.id] = chosen
            for feature_id in chosen:
                feature = spec.features[feature_id]
                if taken.get(feature_id, 0) > 0:
                    can_advance = pick.advance_repeated_rank and bool(
                        feature_next_ranks(spec, pick, feature)
                    )
                    if pick.unique and not feature.repeatable and not can_advance:
                        raise CharacterError(f"{feature.name!r} is already part of this character")
                taken[feature_id] = taken.get(feature_id, 0) + 1
    unknown = set(given) - seen
    if unknown:
        raise CharacterError(f"no feature pick named {min(unknown)!r}")


def _check_feature_repeats(spec: CharacterSpec, submitted: Mapping[str, Any]) -> None:
    repeats = _pick_object(submitted.get("feature_repeats", {}), "feature_repeats")
    selected = _pick_object(submitted.get("feature_picks", {}), "feature_picks")
    active = {
        pick.id: pick
        for template_id in submitted.get("templates", {}).values()
        if (template := spec.templates.get(template_id)) is not None
        for pick in template.feat_picks
    }
    for pick_id, values in repeats.items():
        pick = active.get(pick_id)
        if pick is None or not pick.advance_repeated_rank:
            raise CharacterError(f"feature pick {pick_id!r} cannot repeat features as new choices")
        if (
            not isinstance(values, list)
            or not all(isinstance(value, str) for value in values)
            or len(set(values)) != len(values)
        ):
            raise CharacterError(f"feature repeats for {pick_id!r} must be a list of distinct ids")
        chosen = _selected_feature_pick(spec, pick, selected.get(pick_id, []))
        for feature_id in values:
            if feature_id not in chosen or not spec.features[feature_id].repeatable:
                raise CharacterError(
                    f"{feature_id!r} is not a selected repeatable feature of pick {pick_id!r}"
                )


def _selected_feature_pick(
    spec: CharacterSpec,
    pick: FeatPick,
    chosen: object,
    *,
    allowed_tags: Collection[str] | None = None,
) -> list[str]:
    if not isinstance(chosen, list) or not all(isinstance(item, str) for item in chosen):
        raise CharacterError(f"feature pick {pick.id!r} must be a list of feature ids")
    if len(chosen) > pick.count:
        raise CharacterError(
            f"feature pick {pick.id!r} takes {pick.count} features, not {len(chosen)}"
        )
    if len(set(chosen)) != len(chosen):
        raise CharacterError(f"feature pick {pick.id!r} repeats a feature")
    offered = {
        feature.id for feature in feature_pick_options(spec, pick, allowed_tags=allowed_tags)
    }
    for feature_id in chosen:
        if feature_id not in offered:
            raise CharacterError(_not_offered(spec, pick, feature_id))
    return chosen


def _pick_object(value: object, key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CharacterError(f"{key!r} must map pick ids to lists of ids")
    return value


def _not_offered(spec: CharacterSpec, pick: FeatPick, feature_id: str) -> str:
    feature = spec.features.get(feature_id)
    if feature is None:
        return f"no feature {feature_id!r}"
    rank = f"rank {pick.max_rank} or less" if pick.max_rank is not None else "any rank"
    tags = pick.tags or ((pick.tag,) if pick.tag else ())
    names = ", ".join(repr(tag) for tag in tags) or "any tag"
    kind = f" {pick.feature_type}" if pick.feature_type else " feature"
    return f"{feature.name!r} is no{kind} tagged {names} of {rank}"


def _check_feature_choices(spec: CharacterSpec, submitted: Mapping[str, Any]) -> None:
    """Check the choices given for the features a character may hold. A feature chosen through a
    `feature_picks` entry is allowed even before `resolve` turns it into a held feature."""
    given = submitted.get("feature_choices", {})
    if not isinstance(given, Mapping):
        raise CharacterError("'feature_choices' must map feature picks to lists of choices")
    possible = set(submitted.get("features", []))
    for values in _pick_object(submitted.get("feature_picks", {}), "feature_picks").values():
        if isinstance(values, list):
            possible.update(values)
    for key, values in given.items():
        feature_id, separator, pick_id = str(key).partition(":")
        feature = spec.features.get(feature_id)
        pick = (
            next((pick for pick in feature.picks if pick.id == pick_id), None)
            if feature is not None and separator
            else None
        )
        if feature is None or pick is None:
            raise CharacterError(f"there is no feature choice {key!r}")
        if feature_id not in possible:
            raise CharacterError(f"{feature.name!r} is not a feature this character may take")
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise CharacterError(f"feature choice {key!r} must be a list of choices")
        options = _feature_choice_options(spec, pick)
        for value in values:
            if pick.kind != "text" and value not in options:
                raise CharacterError(
                    f"{value!r} is not a valid {pick.label.lower()} for {feature.name!r}"
                )
        if pick.kind != "text" and len(set(values)) != len(values):
            raise CharacterError(f"{feature.name!r} chooses the same {pick.label.lower()} twice")


def _check_field_values(spec: CharacterSpec, fields: Mapping[str, Any]) -> None:
    """The table-value checks for `check_choices`."""
    tables = option_fields(spec)
    for key, value in fields.items():
        if key not in tables:
            continue
        field_spec = spec.fields.get(key)
        if field_spec is not None:
            allowed = allowed_options(spec, field_spec, fields)
        else:
            allowed = spec.tables[tables[key][1]]
        if value in [option.value for option in allowed]:
            continue
        restriction = field_spec.restriction if field_spec is not None else None
        if restriction is not None and value in [
            option.value for option in spec.tables[tables[key][1]]
        ]:
            raise CharacterError(
                f"{value!r} cannot be {key!r} while {restriction.field_id!r} is one of "
                f"{list(restriction.values)!r}"
            )
        raise CharacterError(
            f"{key!r} must be one of the values from character_options for {key!r}"
        )


def _features_description(spec: CharacterSpec) -> str:
    picks = [
        f"{template.name}: pick {template.choose.count} {template.choose.feature_type}(s) for "
        f"the {template.choose.tag_field} given, or they are rolled"
        for template in spec.templates.values()
        if template.choose is not None
    ]
    base = "Features the character has besides any a template grants"
    return "; ".join([base, *picks])


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


@dataclass(frozen=True)
class CampaignSpec:
    """Options every character in a campaign shares, chosen once when the campaign is created."""

    fields: Mapping[str, Field]
    tables: Mapping[str, tuple[TableOption, ...]]
    table_names: Mapping[str, str]


def build_campaign(
    definitions: list[Definition],
    tables: Mapping[str, tuple[TableOption, ...]],
    table_names: Mapping[str, str],
    template: str,
) -> CampaignSpec:
    fields = _each(
        definitions, "campaign_field", lambda d: _field(d, tables, "campaign_field", template)
    )
    return CampaignSpec(fields=fields, tables=tables, table_names=table_names)


def campaign_schema(spec: CampaignSpec) -> dict[str, Any]:
    """The JSON schema for a campaign's options. Every option is required: the campaign cannot
    start until each one has a value."""
    properties = {f.id: _field_schema(f, spec.tables) for f in spec.fields.values()}
    return {
        "type": "object",
        "description": "Campaign options, chosen once for the whole campaign.",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _field_schema(field: Field, tables: Mapping[str, Sequence[TableOption]]) -> dict[str, Any]:
    description = field.description or field.name
    if field.table is None:
        return {"type": "string", "description": description}
    return _choice_schema(description, tables[field.table])


def _choice_schema(description: str, options: Sequence[TableOption]) -> dict[str, Any]:
    """A choice from a table. When some options are named, each is listed with its description,
    so the GM can choose by what the option does."""
    schema: dict[str, Any] = {
        "type": "string",
        "enum": [option.value for option in options],
        "description": description,
    }
    if any(option.name is not None for option in options):
        lines = [
            f"- {option.name}: {option.description}"
            if option.name is not None
            else f"- {option.description}"
            for option in options
        ]
        schema["description"] = description + "\n" + "\n".join(lines)
    return schema


NO_CHARACTER = CharacterSpec(
    stats={}, templates={}, features={}, fields={}, tables={}, table_names={}
)

NO_CAMPAIGN = CampaignSpec(fields={}, tables={}, table_names={})


class CharacterError(ValueError):
    """A character a player or the GM chose breaks a rule the spec sets."""


@dataclass(frozen=True)
class Sheet:
    """A character with every option resolved. This is what an event stores."""

    stats: dict[str, int]
    templates: dict[str, str]
    # Feature id to how many the character has.
    features: dict[str, int]
    fields: dict[str, str]
    # Counter id to its starting value, for the counters the templates set.
    counters: dict[str, int] = field(default_factory=dict)
    # Skill id to the character's level in it, for the skills they have a level in.
    skills: dict[str, int] = field(default_factory=dict)
    # The armor feature effects add to the armor of the features the character carries.
    armor: int = 0
    # Structured feature choices, keyed "feature:pick", in the order taken.
    feature_choices: dict[str, list[str]] = field(default_factory=dict)
    # Extra modifiers from feature choices, by stat id.
    stat_modifiers: dict[str, int] = field(default_factory=dict)


def fixed_stats(spec: CharacterSpec) -> dict[str, int]:
    """The starting value of each stat that is assigned from a pool: its group's pool in stat
    order. A derived stat has no pool."""
    fixed: dict[str, int] = {}
    groups: dict[str | None, list[Stat]] = {}
    for stat in spec.stats.values():
        if stat.formula is None and stat.derived is None:
            groups.setdefault(stat.swap_group, []).append(stat)
    for members in groups.values():
        for stat, value in zip(members, members[0].values):
            fixed[stat.id] = value
    return fixed


def roll_stats(spec: CharacterSpec, rng: random.Random, *, plus: str) -> dict[str, int]:
    """Roll each stat that is rolled, and assign each that is given from a pool. A derived stat
    has no value of its own, and is left out."""
    fixed = fixed_stats(spec)
    stats = {
        s.id: fixed[s.id] if s.formula is None else roll_total(s.formula, rng, plus=plus)
        for s in spec.stats.values()
        if s.derived is None
    }
    _apply_plus_modifiers(spec, stats)
    return stats


def _apply_plus_modifiers(spec: CharacterSpec, stats: dict[str, int]) -> None:
    """Add the modifier of the stat a stat names to its value, such as Constitution to hit points,
    then lift every stat to its minimum."""
    for stat in spec.stats.values():
        if stat.plus_modifier:
            stats[stat.id] += spec.modifier(stats, stat.plus_modifier)
        if stat.minimum is not None:
            stats[stat.id] = max(stat.minimum, stats[stat.id])


def check_swaps(spec: CharacterSpec, rolled: Mapping[str, int], chosen: Mapping[str, int]) -> None:
    """A player keeps the rolled stats, except that each swap group may swap one pair of values.
    Stats outside a group must keep their rolled value. A derived stat is computed, not chosen."""
    groups: dict[str | None, list[str]] = {}
    assigned = {s.id for s in spec.stats.values() if s.formula is None and s.derived is None}
    for stat in spec.stats.values():
        if stat.derived is None:
            groups.setdefault(stat.swap_group, []).append(stat.id)
    for group, ids in groups.items():
        before = [rolled[i] for i in ids]
        after = [chosen[i] for i in ids]
        if group is None:
            if before != after:
                names = ", ".join(ids)
                raise CharacterError(f"stats {names} cannot be changed from their rolled values")
            continue
        if sorted(before) != sorted(after):
            raise CharacterError(f"stats in group {group!r} must be the rolled values, reordered")
        if ids[0] in assigned:
            continue
        if sum(b != a for b, a in zip(before, after)) > 2:
            raise CharacterError(f"stats in group {group!r} allow one swap")


def resolve(
    spec: CharacterSpec, submitted: Mapping[str, Any], rng: random.Random, *, plus: str
) -> Sheet:
    """Fill in what a GM or player left out, and roll what is random. `submitted` must already
    match `character_schema(spec)` and pass `check_choices`. Stats left out are rolled; text left
    out is blank; a table left out is rolled; a template's features are added with their rolled
    counts, and its skill and feature picks are taken from `skill_picks` and `feature_picks` or
    rolled. Derived stats are computed, and each feature's effects are run in turn."""
    given_stats = submitted.get("stats", {})
    given_templates = submitted.get("templates", {})
    given_features = submitted.get("features", [])
    given_fields = submitted.get("fields", {})
    given_skill_picks = _pick_object(submitted.get("skill_picks", {}), "skill_picks")
    given_feature_picks = _pick_object(submitted.get("feature_picks", {}), "feature_picks")
    given_feature_choices = _pick_object(submitted.get("feature_choices", {}), "feature_choices")
    given_feature_repeats = _pick_object(submitted.get("feature_repeats", {}), "feature_repeats")
    _check_feature_repeats(spec, submitted)
    skill_replacements = _skill_replacement_choices(spec, submitted)
    fixed = fixed_stats(spec)
    stats = {
        s.id: given_stats[s.id]
        if s.id in given_stats
        else fixed[s.id]
        if s.formula is None
        else roll_total(s.formula, rng, plus=plus)
        for s in spec.stats.values()
        if s.derived is None
    }
    templates = dict(given_templates)
    features = {feature: 1 for feature in given_features}
    fields: dict[str, str] = {}
    restricted: list[Field] = []
    counter_amounts: dict[str, int] = {}
    increments: dict[str, int] = {}
    for template_id in templates.values():
        template = spec.templates[template_id]
        for stat_id, bonus in template.stats.items():
            stats[stat_id] += bonus
        for counter_id, amount in template.counters.items():
            counter_amounts[counter_id] = counter_amounts.get(counter_id, 0) + amount
        for skill_id, amount in template.skills.items():
            increments[skill_id] = increments.get(skill_id, 0) + amount
        for granted in template.features:
            count = (
                int(granted.quantity)
                if granted.quantity.isdigit()
                else roll_total(granted.quantity, rng, plus=plus)
            )
            if count < 0:
                raise CharacterError(f"feature {granted.feature!r} rolled a negative quantity")
            if count:
                features[granted.feature] = features.get(granted.feature, 0) + count
        for table in template.tables:
            fields[table] = given_fields.get(table) or rng.choice(spec.tables[table]).value
    _apply_plus_modifiers(spec, stats)
    for field_spec in spec.fields.values():
        if field_spec.table is None:
            fields[field_spec.id] = given_fields.get(field_spec.id, "")
        elif field_spec.restriction is not None:
            restricted.append(field_spec)
        else:
            fields[field_spec.id] = _field_value(spec, field_spec, given_fields, fields, rng)
    for field_spec in restricted:
        fields[field_spec.id] = _field_value(spec, field_spec, given_fields, fields, rng)
    feat_picks: list[tuple[FeatPick, list[str]]] = []
    selected_by_pick: dict[str, list[str]] = {}
    for template_id in templates.values():
        for pick in spec.templates[template_id].feat_picks:
            chosen_given = given_feature_picks.get(pick.id, [])
            allowed_tags = None
            if pick.tags_from and chosen_given:
                source = selected_by_pick.get(pick.tags_from)
                if not source:
                    raise CharacterError(
                        f"feature pick {pick.id!r} needs a choice for {pick.tags_from!r} first"
                    )
                allowed_tags = {spec.features[feature_id].tag for feature_id in source}
            chosen = _selected_feature_pick(spec, pick, chosen_given, allowed_tags=allowed_tags)
            selected_by_pick[pick.id] = chosen
            feat_picks.append((pick, chosen))
    reserved = {feature for _, chosen in feat_picks for feature in chosen}
    feature_slots: list[tuple[str, tuple[str, ...]]] = []
    tagged_planned: dict[tuple[str, str], int] = {}
    for template_id in templates.values():
        choose = spec.templates[template_id].choose
        if choose is not None:
            _plan_tagged_features(spec, choose, fields, features, tagged_planned, feature_slots)
    tagged_count = len(feature_slots)
    skill_picks: list[tuple[SkillPick, list[str]]] = []
    remembered_picks = _derived_skill_pick_ids(spec.stats)
    planned_increments = dict(increments)
    for template_id in templates.values():
        for pick in spec.templates[template_id].skill_picks:
            chosen = _take_skill_pick(
                spec, pick, planned_increments, given_skill_picks.get(pick.id, [])
            )
            skill_picks.append((pick, chosen))
    _replace_excess_template_skills(spec, increments, skill_replacements, rng, skill_picks)
    skill_index = 0
    for template_id in templates.values():
        template = spec.templates[template_id]
        for pick in template.skill_picks:
            chosen = skill_picks[skill_index][1]
            _take_skill_pick(spec, pick, increments, chosen)
            rolled_skills = _roll_skill_picks(
                spec,
                pick,
                increments,
                pick.count - len(chosen),
                rng,
                skill_picks[skill_index + 1 :],
                skill_replacements,
            )
            if pick.id in remembered_picks:
                selected_skills = chosen + rolled_skills
                if len(selected_skills) != 1:
                    raise CharacterError(f"skill pick {pick.id!r} needs one recorded choice")
                fields[pick.id] = selected_skills[0]
            skill_index += 1
    blocked = set(features)
    feature_plans: list[tuple[FeatPick, list[str], int, int]] = []
    for pick, chosen in feat_picks:
        blocked.update(chosen)
        pool = tuple(
            sorted(
                feature.id
                for feature in feature_pick_options(spec, pick)
                if feature.id not in blocked
            )
        )
        needed = pick.count - len(chosen)
        if len(pool) < needed:
            raise CharacterError(
                f"feature pick {pick.id!r} requires {pick.count} different options; "
                f"only {len(chosen) + len(pool)} available"
            )
        start = len(feature_slots)
        message = f"feature pick {pick.id!r} cannot be filled with distinct options"
        feature_slots.extend((message, pool) for _ in range(needed))
        feature_plans.append((pick, chosen, start, len(feature_slots)))
    rolled = _roll_feature_slots(feature_slots, reserved, rng)
    for feature_id in rolled[:tagged_count]:
        features[feature_id] = 1
    for pick, chosen, start, end in feature_plans:
        for feature_id in (*chosen, *rolled[start:end]):
            _grant_feature_pick(
                spec,
                features,
                pick,
                feature_id,
                repeat_as_new=feature_id in given_feature_repeats.get(pick.id, []),
            )
    _check_ordered_feature_ranks(spec, features)
    stat_modifiers: dict[str, int] = {}
    feature_choices = _feature_choice_values(
        spec, features, given_feature_choices, increments, stat_modifiers
    )
    counters = {
        counter_id: _within(spec.counters[counter_id], spec.counters[counter_id].initial + amount)
        for counter_id, amount in counter_amounts.items()
    }
    stats.update(
        derived_stats(
            spec,
            stats,
            counters,
            _final_skills(spec, increments),
            templates,
            fields=fields,
            modifier_bonus=stat_modifiers,
        )
    )
    armor = 0
    for feature_id, count in features.items():
        effects = spec.features[feature_id].effects
        if effects:
            for _ in range(count):
                for effect in effects:
                    armor += _run_effect(
                        spec, effect, stats, increments, counters, skill_replacements, rng
                    )
    skills = _final_skills(spec, increments)
    stats.update(
        derived_stats(
            spec,
            stats,
            counters,
            skills,
            templates,
            fields=fields,
            modifier_bonus=stat_modifiers,
        )
    )
    return Sheet(
        stats=stats,
        templates=templates,
        features=features,
        fields=fields,
        counters=counters,
        skills=skills,
        armor=armor,
        feature_choices=feature_choices,
        stat_modifiers=stat_modifiers,
    )


def _feature_choice_values(
    spec: CharacterSpec,
    features: Mapping[str, int],
    given: Mapping[str, list[str]],
    increments: dict[str, int],
    stat_modifiers: dict[str, int],
) -> dict[str, list[str]]:
    """Validate the choices the features the character holds ask for, filling in one when a
    rolled feature left the choice blank. A `trained` choice marks a level-0 skill as trained."""
    choices: dict[str, list[str]] = {}
    for feature_id, count in features.items():
        feature = spec.features[feature_id]
        for pick in feature.picks:
            key = f"{feature_id}:{pick.id}"
            values = list(given.get(key, []))
            allowed = pick.count * count
            if len(values) > allowed:
                raise CharacterError(
                    f"{feature.name!r} takes {allowed} {pick.label.lower()} choice"
                    f"{'' if allowed == 1 else 's'}; {len(values)} were given"
                )
            options = _feature_choice_options(spec, pick)
            if pick.kind == "text":
                if values:
                    choices[key] = values
                continue
            if not options:
                raise CharacterError(f"{feature.name!r} has no {pick.label.lower()} to choose")
            if len(values) < allowed and pick.kind == "skill":
                # A rolled skill feature still needs a usable choice. Unlike a stat modifier,
                # which the player or GM can pick later, a skill grant cannot be applied without
                # knowing the skill, so fill it deterministically.
                remaining = [option for option in options if option not in values]
                while len(values) < allowed and remaining:
                    values.append(remaining.pop(0))
            for value in values:
                if value not in options:
                    raise CharacterError(
                        f"{value!r} is not a valid {pick.label.lower()} for {feature.name!r}"
                    )
            if len(set(values)) != len(values):
                raise CharacterError(
                    f"{feature.name!r} chooses the same {pick.label.lower()} twice"
                )
            choices[key] = values
            if pick.grant == "trained":
                for value in values:
                    increments.setdefault(value, 0)
            elif pick.grant == "modifier":
                for value in values:
                    stat_modifiers[value] = stat_modifiers.get(value, 0) + 1
    return choices


def _feature_choice_options(spec: CharacterSpec, pick: FeaturePick) -> tuple[str, ...]:
    """The ids a feature pick offers, in the rule set's order."""
    if pick.kind == "stat":
        options = [stat.id for stat in spec.stats.values() if stat.derived is None]
    elif pick.kind == "skill":
        options = list(spec.skills)
    else:
        return ()
    if pick.choices:
        options = [option for option in options if option in pick.choices]
    return tuple(option for option in options if option not in pick.exclude)


def feature_choice_options(spec: CharacterSpec, pick: FeaturePick) -> tuple[str, ...]:
    """The ids a feature pick offers, in the rule set's order."""
    return _feature_choice_options(spec, pick)


def feature_choice_views(spec: CharacterSpec, feature: Feature) -> tuple[Mapping[str, Any], ...]:
    """A feature's structured picks, with the names of the options each offers, for a form."""
    views: list[Mapping[str, Any]] = []
    for pick in feature.picks:
        options = _feature_choice_options(spec, pick)
        names = spec.stats if pick.kind == "stat" else spec.skills
        views.append(
            {
                "id": pick.id,
                "kind": pick.kind,
                "label": pick.label,
                "count": pick.count,
                "options": [{"value": option, "title": names[option].name} for option in options],
            }
        )
    return tuple(views)


def _grant_feature_pick(
    spec: CharacterSpec,
    features: dict[str, int],
    pick: FeatPick,
    feature_id: str,
    *,
    repeat_as_new: bool = False,
) -> None:
    feature = spec.features[feature_id]
    if repeat_as_new:
        if not pick.advance_repeated_rank or not feature.repeatable or not features.get(feature_id):
            raise CharacterError(f"{feature.name!r} cannot be repeated as a new choice")
        features[feature_id] += 1
        return
    if pick.advance_repeated_rank and feature.rank > 0 and features.get(feature_id, 0):
        higher = feature_next_ranks(spec, pick, feature)
        if len(higher) > 1:
            raise CharacterError(f"{feature.name!r} has more than one next rank")
        if higher:
            target = higher[0].id
            if features.get(target, 0):
                raise CharacterError(f"{feature.name!r} has already reached its next rank")
            features[target] = 1
            return
        if feature.tag in spec.ordered_feature_tags and not feature.repeatable:
            raise CharacterError(f"{feature.name!r} is already at its maximum rank")
    features[feature_id] = features.get(feature_id, 0) + 1


def _check_ordered_feature_ranks(spec: CharacterSpec, features: Mapping[str, int]) -> None:
    for feature_id in features:
        check_ordered_feature_rank(spec, spec.features[feature_id], features)


def check_ordered_feature_rank(
    spec: CharacterSpec, feature: Feature, features: Mapping[str, int]
) -> None:
    """Validate a held feature against its ruleset's ordered ranks."""
    count = features.get(feature.id, 0)
    if not count or feature.tag not in spec.ordered_feature_tags or feature.rank <= 0:
        return
    if count > 1 and not feature.repeatable:
        raise CharacterError(
            f"{feature.name!r} cannot be taken more than once at rank {feature.rank}"
        )
    if feature.rank == 1:
        return
    previous = [
        candidate
        for candidate in spec.features.values()
        if candidate.name == feature.name
        and candidate.tag == feature.tag
        and candidate.feature_type == feature.feature_type
        and candidate.rank == feature.rank - 1
    ]
    if len(previous) != 1 or not features.get(previous[0].id):
        raise CharacterError(f"{feature.name!r} rank {feature.rank} requires its previous rank")


def _plan_tagged_features(
    spec: CharacterSpec,
    choose: Choose,
    fields: Mapping[str, str],
    features: Mapping[str, int],
    planned: dict[tuple[str, str], int],
    slots: list[tuple[str, tuple[str, ...]]],
) -> None:
    """Plan a template's tagged picks, accounting for features already held or planned."""
    tag = fields.get(choose.tag_field, "")
    if tag in choose.skip_tags:
        return
    held = [
        feature
        for feature in features
        if spec.features[feature].feature_type == choose.feature_type
        and spec.features[feature].tag == tag
    ]
    key = (choose.feature_type, tag)
    previous = planned.get(key, 0)
    needed = max(0, choose.count - len(held) - previous)
    pool = tuple(
        sorted(
            feature.id
            for feature in spec.features.values()
            if feature.feature_type == choose.feature_type
            and feature.tag == tag
            and feature.id not in features
        )
    )
    if len(pool) < previous + needed:
        raise CharacterError(
            f"{choose.feature_type} picks for {tag!r} require {choose.count} different options; "
            f"only {len(held) + len(pool)} available"
        )
    message = f"{choose.feature_type} picks for {tag!r} cannot be filled with distinct options"
    slots.extend((message, pool) for _ in range(needed))
    planned[key] = previous + needed


def _roll_feature_slots(
    slots: Sequence[tuple[str, tuple[str, ...]]], reserved: set[str], rng: random.Random
) -> tuple[str, ...]:
    """Fill overlapping random pools, reserving manual picks whenever possible."""
    options = [rng.sample(pool, len(pool)) for _, pool in slots]

    def assign(*, reserve: bool) -> tuple[tuple[str, ...] | None, str]:
        owners: dict[str, int] = {}
        assigned: dict[int, str] = {}
        for start, (message, _) in enumerate(slots):
            pending = [start]
            visited = {start}
            reached: dict[str, int] = {}
            free: str | None = None
            # Reassign earlier rolls along an alternating path when pools overlap.
            for slot in pending:
                for feature_id in options[slot]:
                    if feature_id in reached or (reserve and feature_id in reserved):
                        continue
                    reached[feature_id] = slot
                    owner = owners.get(feature_id)
                    if owner is None:
                        free = feature_id
                        break
                    if owner not in visited:
                        visited.add(owner)
                        pending.append(owner)
                if free is not None:
                    break
            if free is None:
                return None, message
            while free is not None:
                slot = reached[free]
                previous = assigned.get(slot)
                owners[free] = slot
                assigned[slot] = free
                free = previous
        return tuple(assigned[index] for index in range(len(slots))), ""

    result, message = assign(reserve=True)
    if result is None and reserved:
        result, message = assign(reserve=False)
    if result is None:
        raise CharacterError(message)
    return result


def _take_skill_pick(
    spec: CharacterSpec,
    pick: SkillPick,
    increments: dict[str, int],
    chosen: object,
) -> list[str]:
    """Apply a pick's explicitly chosen skills, respecting its maximum."""
    if not isinstance(chosen, list) or not all(isinstance(s, str) for s in chosen):
        raise CharacterError(f"skill pick {pick.id!r} must be a list of skill ids")
    if len(chosen) > pick.count:
        raise CharacterError(f"skill pick {pick.id!r} takes {pick.count} skills, not {len(chosen)}")
    offered = {skill_id for skill_id, _ in skill_pick_options(spec, pick)}
    for skill_id in chosen:
        if skill_id not in offered:
            raise CharacterError(f"{skill_id!r} is not offered by the skill pick {pick.id!r}")
        if not _skill_pick_allows(spec, pick, increments, skill_id):
            raise CharacterError(
                f"skill pick {pick.id!r} cannot raise {skill_id!r} above level {pick.max_level}"
            )
        increments[skill_id] = increments.get(skill_id, 0) + 1
    return chosen


def _skill_pick_allows(
    spec: CharacterSpec, pick: SkillPick, increments: Mapping[str, int], skill_id: str
) -> bool:
    return _skill_below_maximum(spec, increments, skill_id, pick.max_level)


def _skill_below_maximum(
    spec: CharacterSpec, increments: Mapping[str, int], skill_id: str, maximum: int
) -> bool:
    return increments.get(skill_id, 0) <= 0 or _skill_level_of(spec, increments, skill_id) < maximum


def _later_skill_picks_allow(
    spec: CharacterSpec,
    increments: Mapping[str, int],
    later: Sequence[tuple[SkillPick, list[str]]],
) -> bool:
    trial = dict(increments)
    for future_pick, chosen in later:
        for skill_id in chosen:
            if not _skill_pick_allows(spec, future_pick, trial, skill_id):
                return False
            trial[skill_id] = trial.get(skill_id, 0) + 1
    return True


def _skill_replacement_choices(
    spec: CharacterSpec, submitted: Mapping[str, Any]
) -> dict[str, deque[str]]:
    progression = spec.skill_progression
    rules = progression.creation_replacements if progression is not None else None
    given = _pick_object(submitted.get("skill_replacements", {}), "skill_replacements")
    if rules is None:
        if given:
            raise CharacterError("the rule set has no creation skill replacements")
        return {}
    if any(key not in {"roll", "feature"} for key in given):
        raise CharacterError("skill replacements may only specify 'roll' and 'feature' choices")
    choices: dict[str, deque[str]] = {}
    for kind, excluded in (("roll", rules.roll_exclude), ("feature", rules.feature_exclude)):
        values = given.get(kind, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise CharacterError(f"{kind} skill replacements must be a list of skill ids")
        for skill_id in values:
            if skill_id and (skill_id not in spec.skills or skill_id in excluded):
                raise CharacterError(f"{skill_id!r} is not an eligible {kind} replacement skill")
        choices[kind] = deque(values)
    return choices


def _replace_excess_skill(
    spec: CharacterSpec,
    increments: dict[str, int],
    original: str,
    kind: str,
    choices: Mapping[str, deque[str]],
    rng: random.Random,
    later: Sequence[tuple[SkillPick, list[str]]] = (),
    maximum: int | None = None,
) -> None:
    progression = spec.skill_progression
    if progression is None or progression.creation_replacements is None:
        raise CharacterError("the rule set has no creation skill replacements")
    rules = progression.creation_replacements
    excluded = rules.roll_exclude if kind == "roll" else rules.feature_exclude
    limit = progression.creation_maximum if maximum is None else maximum
    pool = []
    for skill_id in spec.skills:
        if (
            skill_id == original
            or skill_id in excluded
            or not _skill_below_maximum(spec, increments, skill_id, limit)
        ):
            continue
        trial = dict(increments)
        trial[skill_id] = trial.get(skill_id, 0) + 1
        if _later_skill_picks_allow(spec, trial, later):
            pool.append(skill_id)
    if not pool:
        raise CharacterError(
            f"no eligible replacement skill for excess {kind} grant of {original!r}"
        )
    selected = choices[kind].popleft() if choices[kind] else ""
    if selected and selected not in pool:
        raise CharacterError(
            f"{selected!r} cannot replace {original!r}: choose another uncapped skill "
            "not needed by a later pick"
        )
    skill_id = selected or rng.choice(pool)
    increments[skill_id] = increments.get(skill_id, 0) + 1


def _replace_excess_template_skills(
    spec: CharacterSpec,
    increments: dict[str, int],
    choices: Mapping[str, deque[str]],
    rng: random.Random,
    later: Sequence[tuple[SkillPick, list[str]]],
) -> None:
    progression = spec.skill_progression
    if progression is None or progression.creation_replacements is None:
        return
    capacity = progression.creation_maximum - progression.first_level + 1
    for skill_id, count in tuple(increments.items()):
        excess = max(0, count - capacity)
        if not excess:
            continue
        increments[skill_id] = capacity
        for _ in range(excess):
            _replace_excess_skill(spec, increments, skill_id, "roll", choices, rng, later)


def _roll_skill_picks(
    spec: CharacterSpec,
    pick: SkillPick,
    increments: dict[str, int],
    count: int,
    rng: random.Random,
    later: Sequence[tuple[SkillPick, list[str]]],
    choices: Mapping[str, deque[str]],
) -> list[str]:
    """Roll remaining picks without making a later explicit choice invalid."""
    rolled: list[str] = []
    for _ in range(count):
        pool = []
        progression = spec.skill_progression
        replace_excess = progression is not None and progression.creation_replacements is not None
        for skill_id, _ in skill_pick_options(spec, pick):
            if not _skill_pick_allows(spec, pick, increments, skill_id):
                if replace_excess:
                    pool.append(skill_id)
                continue
            trial = dict(increments)
            trial[skill_id] = trial.get(skill_id, 0) + 1
            if _later_skill_picks_allow(spec, trial, later):
                pool.append(skill_id)
        if not pool:
            if replace_excess:
                raise CharacterError(f"skill pick {pick.id!r} has no eligible roll or replacement")
            return rolled
        skill_id = rng.choice(pool)
        if _skill_pick_allows(spec, pick, increments, skill_id):
            increments[skill_id] = increments.get(skill_id, 0) + 1
        else:
            _replace_excess_skill(
                spec, increments, skill_id, "roll", choices, rng, later, maximum=pick.max_level
            )
        rolled.append(skill_id)
    return rolled


def _run_effect(
    spec: CharacterSpec,
    effect: Mapping[str, Any],
    stats: dict[str, int],
    increments: dict[str, int],
    counters: dict[str, int],
    choices: Mapping[str, deque[str]],
    rng: random.Random,
) -> int:
    """Run one feature effect in place. Returns the armor it adds, which the others do not."""
    op = effect["op"]
    if op == "stat":
        raised = stats[effect["id"]] + effect["delta"]
        cap = effect.get("cap")
        stats[effect["id"]] = min(raised, cap) if isinstance(cap, int) else raised
    elif op == "skill":
        skill_id, delta = effect["id"], effect["delta"]
        progression = spec.skill_progression
        if progression is not None and progression.creation_replacements is not None and delta > 0:
            for _ in range(delta):
                if _skill_below_maximum(spec, increments, skill_id, progression.creation_maximum):
                    increments[skill_id] = increments.get(skill_id, 0) + 1
                else:
                    _replace_excess_skill(spec, increments, skill_id, "feature", choices, rng)
        else:
            increments[skill_id] = increments.get(skill_id, 0) + delta
    elif op == "counter":
        counter_id, delta = effect["id"], effect["delta"]
        counter = spec.counters.get(counter_id)
        start = counters.get(counter_id, counter.initial if counter else 0)
        counters[counter_id] = _within(counter, start + delta) if counter else start + delta
    elif op == "armor":
        return effect["delta"]
    return 0


def _skill_level_of(spec: CharacterSpec, increments: Mapping[str, int], skill: str) -> int:
    progression = spec.skill_progression
    if progression is None:
        raise CharacterError("the rule set has no skill progression")
    count = increments.get(skill, 0)
    if count <= 0:
        return progression.untrained
    return min(progression.creation_maximum, progression.first_level + count - 1)


def _final_skills(spec: CharacterSpec, increments: Mapping[str, int]) -> dict[str, int]:
    """Resolve trained skills, including those whose level equals the untrained value. A
    `trained` feature choice adds the skill at the progression's first level, which may be 0
    even when the rule set's untrained value is lower."""
    skills: dict[str, int] = {}
    progression = spec.skill_progression
    for skill_id, count in increments.items():
        if count > 0:
            skills[skill_id] = _skill_level_of(spec, increments, skill_id)
        elif count == 0 and progression is not None:
            skills[skill_id] = progression.first_level
    return skills


def derived_stats(
    spec: CharacterSpec,
    stats: Mapping[str, int],
    counters: Mapping[str, int],
    skills: Mapping[str, int],
    templates: Mapping[str, str] | None = None,
    *,
    fields: Mapping[str, str] | None = None,
    modifier_bonus: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """The value of every derived stat the character has, by stat id. A stat whose
    `requires_templates` are not among the chosen templates is left out. A derived stat earlier
    in the spec may be used by a later one. Any value the mapping already holds for a derived
    stat is ignored, so the result is the same however often it is computed. `templates` are the
    chosen templates by kind, for a derived stat that depends on one; without them, a stat that
    requires a template is left out."""
    derived_ids = {stat.id for stat in spec.stats.values() if stat.derived is not None}
    scores = {key: value for key, value in stats.items() if key not in derived_ids}
    found: dict[str, int] = {}
    for stat in spec.stats.values():
        if stat.derived is None or not _derived_available(stat.derived, templates):
            continue
        found[stat.id] = _derived_value(
            spec, stat.derived, scores, counters, skills, templates, fields, modifier_bonus
        )
        scores[stat.id] = found[stat.id]
    return found


def _derived_available(derived: DerivedStat, templates: Mapping[str, str] | None) -> bool:
    """Whether the chosen templates make a derived stat available. A stat with no
    `requires_templates` is always available; one with them needs a matching chosen template."""
    if not derived.requires_templates:
        return True
    return any(template in (templates or {}).values() for template in derived.requires_templates)


def _derived_value(
    spec: CharacterSpec,
    derived: DerivedStat,
    stats: Mapping[str, int],
    counters: Mapping[str, int],
    skills: Mapping[str, int],
    templates: Mapping[str, str] | None,
    fields: Mapping[str, str] | None,
    modifier_bonus: Mapping[str, int] | None = None,
) -> int:
    value = derived.base + derived.plus
    value -= sum(_counter_start(spec, counters, name) for name in derived.minus_counters)
    value += max(
        (
            spec.modifier(stats, name) + (modifier_bonus or {}).get(name, 0)
            for name in derived.best_modifier
        ),
        default=0,
    )
    value -= max(
        (
            spec.modifier(stats, name) + (modifier_bonus or {}).get(name, 0)
            for name in derived.minus_best_modifier
        ),
        default=0,
    )
    value += max((_score(stats, name) for name in derived.best_score), default=0)
    value += sum(spec.skill_level(skills, name) for name in derived.plus_skills)
    value += sum(
        _chosen_skill_level(spec, pick_id, skills, templates, fields)
        for pick_id in derived.plus_skill_picks
    )
    if templates is not None and any(t in templates.values() for t in derived.minus_templates):
        value -= 1
    if derived.minimum is not None:
        value = max(derived.minimum, value)
    return value


def _chosen_skill_level(
    spec: CharacterSpec,
    pick_id: str,
    skills: Mapping[str, int],
    templates: Mapping[str, str] | None,
    fields: Mapping[str, str] | None,
) -> int:
    selected = next(
        (
            pick
            for template_id in (templates or {}).values()
            if (template := spec.templates.get(template_id)) is not None
            for pick in template.skill_picks
            if pick.id == pick_id
        ),
        None,
    )
    skill = (fields or {}).get(pick_id)
    if templates is not None and selected is None:
        if skill:
            raise CharacterError(f"skill pick {pick_id!r} is not available to this character")
        return 0
    if not skill:
        if selected is not None:
            raise CharacterError(f"skill pick {pick_id!r} has no recorded choice")
        return 0
    if skill not in spec.skills or (
        selected is not None
        and skill not in {value for value, _ in skill_pick_options(spec, selected)}
    ):
        raise CharacterError(f"{skill!r} is not offered by skill pick {pick_id!r}")
    return spec.skill_level(skills, skill)


def _counter_start(spec: CharacterSpec, counters: Mapping[str, int], name: str) -> int:
    """A counter's starting value: the one the templates gave it, or the counter's initial."""
    if name in counters:
        return counters[name]
    counter = spec.counters.get(name)
    if counter is None:
        raise CharacterError(f"no counter {name!r} to take a derived stat from")
    return counter.initial


def _score(stats: Mapping[str, int], name: str) -> int:
    if name not in stats:
        raise CharacterError(f"no score for the stat {name!r}")
    return stats[name]


def skill_pick_options(spec: CharacterSpec, pick: SkillPick) -> tuple[tuple[str, str], ...]:
    """The skills a pick offers, as (skill id, label) pairs: the option values of its table that
    it does not exclude and that are skills."""
    return tuple(
        (option.value, spec.skills[option.value].name)
        for option in spec.tables.get(pick.table, ())
        if option.value not in pick.exclude and option.value in spec.skills
    )


def feature_pick_options(
    spec: CharacterSpec, pick: FeatPick, *, allowed_tags: Collection[str] | None = None
) -> tuple[Feature, ...]:
    """The features a pick offers: those whose tag is one of the pick's, whose rank is at most
    its `max_rank` when one is given, and that it does not exclude. An `allowed_tags` set, from
    another pick, further narrows the tags the pick may offer."""
    tags = pick.tags or ((pick.tag,) if pick.tag else ())
    return tuple(
        feature
        for feature in spec.features.values()
        if (not tags or feature.tag in tags)
        and (allowed_tags is None or feature.tag in allowed_tags)
        and (not pick.feature_type or feature.feature_type == pick.feature_type)
        and (pick.max_rank is None or feature.rank <= pick.max_rank)
        and feature.id not in pick.exclude
    )


def feature_next_ranks(
    spec: CharacterSpec, pick: FeatPick, feature: Feature
) -> tuple[Feature, ...]:
    return tuple(
        candidate
        for candidate in spec.features.values()
        if candidate.name == feature.name
        and candidate.tag == feature.tag
        and candidate.feature_type == feature.feature_type
        and candidate.rank == feature.rank + 1
        and candidate.id not in pick.exclude
    )


def _field_value(
    spec: CharacterSpec,
    field_spec: Field,
    given_fields: Mapping[str, Any],
    fields: Mapping[str, Any],
    rng: random.Random,
) -> str:
    """The value of a table field: the one given, or else rolled. A group roll picks a group
    first, so the group picked is as likely as any other."""
    options = allowed_options(spec, field_spec, fields)
    if field_spec.id in given_fields:
        value = given_fields[field_spec.id]
        if field_spec.restriction is not None and value not in [o.value for o in options]:
            raise CharacterError(f"{value!r} is not allowed for {field_spec.id!r} here")
        return value
    if field_spec.group_roll:
        groups = list(dict.fromkeys(option.group for option in options))
        group = rng.choice(groups)
        options = tuple(option for option in options if option.group == group)
    return rng.choice(options).value


def _within(counter: CounterRef, value: int) -> int:
    if counter.minimum is not None:
        value = max(counter.minimum, value)
    if counter.maximum is not None:
        value = min(counter.maximum, value)
    return value


def _known_features(spec: CharacterSpec, field_id: str, value: str) -> tuple[str, str, str] | None:
    """The feature type a field's value knows, its table's name for the field's options, and the
    labels of the earlier options, or None when the field knows nothing."""
    field_spec = spec.fields.get(field_id)
    if field_spec is None or field_spec.knows is None or field_spec.table is None:
        return None
    options = spec.tables[field_spec.table]
    values = [option.value for option in options]
    if value not in values:
        return None
    earlier = [option.value for option in options[: values.index(value)]]
    if not earlier:
        return None
    return (
        field_spec.knows,
        spec.table_names.get(field_spec.table, field_spec.name),
        ", ".join(earlier),
    )


def _skill_name(spec: CharacterSpec, skill: str) -> str:
    """A skill's name, or its id when the spec does not define it."""
    found = spec.skills.get(skill)
    return found.name if found is not None else skill


def kind_label(kind: str) -> str:
    """A readable label for a template kind. A trailing number names a later selection of the
    same kind, so `class2` reads as "Second Class"."""
    base = kind.removesuffix("2")
    label = base.replace("_", " ").strip().title()
    return f"Second {label}" if base != kind else label


def describe(spec: CharacterSpec, sheet: Sheet) -> str:
    """The character as the GM and the player read it, one line per part. Empty parts are left
    out."""
    lines: list[str] = []
    if sheet.stats:
        lines.append(
            "Stats: " + ", ".join(f"{spec.stats[s].name} {v}" for s, v in sheet.stats.items())
        )
    for kind in spec.kinds():
        if kind in sheet.templates:
            template = spec.templates[sheet.templates[kind]]
            lines.append(f"{kind_label(kind)}: {template.name}")
    if sheet.skills:
        lines.append(
            "Skills: "
            + ", ".join(f"{_skill_name(spec, s)} {level}" for s, level in sheet.skills.items())
        )
    for counter_id, value in sheet.counters.items():
        if counter_id in spec.counters:
            lines.append(f"{spec.counters[counter_id].label}: {value}")
    for field_id, value in sheet.fields.items():
        known = _known_features(spec, field_id, value)
        if known:
            lines.append(f"Knows every {known[0]} tagged with an earlier {known[1]}: {known[2]}")
    if sheet.features:
        lines.append("Features:")
        for feature, count in sheet.features.items():
            found = spec.features[feature]
            name = found.name
            if found.tag in spec.ordered_feature_tags and found.rank > 0:
                name += f" (rank {found.rank})"
            if count != 1:
                name += f" x{count}"
            chosen = [
                value
                for key, values in sheet.feature_choices.items()
                if key.startswith(f"{feature}:")
                for value in values
            ]
            if chosen:
                chosen = [
                    spec.skills[value].name
                    if value in spec.skills
                    else spec.stats[value].name
                    if value in spec.stats
                    else value
                    for value in chosen
                ]
                name += f" — {', '.join(chosen)}"
            notes = [
                str(effect["text"])
                for effect in found.effects
                if effect.get("op") == "note" and effect.get("text")
            ]
            if notes:
                name += f": {'; '.join(notes)}"
            lines.append(f"• {name}")
    remembered_picks = _derived_skill_pick_ids(spec.stats)
    skill_pick_labels = {
        pick.id: pick.label
        for template_id in sheet.templates.values()
        for pick in spec.templates[template_id].skill_picks
        if pick.id in remembered_picks
    }
    for field_id, value in sheet.fields.items():
        if not value:
            continue
        if field_id in spec.fields:
            label = spec.fields[field_id].name
        elif field_id in skill_pick_labels:
            label = skill_pick_labels[field_id]
            value = _skill_name(spec, value)
        else:
            label = spec.table_names.get(field_id, field_id)
        lines.append(f"{label}: {value}")
    return "\n".join(lines)
