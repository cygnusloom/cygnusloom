"""A sourcebook-style compendium built from a rule set, a world, and a campaign's live state.

The compendium holds what a player could read in a sourcebook: the rule set's monsters, abilities,
items, character options, skills, conditions, and lore rules; a world's own monsters, features,
regions, and place names; and a campaign's discovered places, encountered creatures, and custom
items. It never reads GM-only text: NPC descriptions, notes, settings, the journal, encounter maps,
undiscovered places, world rules, and world tables all stay out.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from cygnusforge.content.character import CharacterSpec, Feature, kind_label
from cygnusforge.content.definitions import ContentError
from cygnusforge.content.library import (
    ConditionRules,
    ContentLibrary,
    MonsterTemplate,
    RuleSet,
    ShopItem,
    WorldTemplate,
)
from cygnusforge.services.events import (
    PC_KIND,
    CampaignState,
    Character,
    Item,
    name_key,
)

SECTION_NAMES = (
    ("world", "The World"),
    ("bestiary", "Bestiary"),
    ("abilities", "Abilities"),
    ("items", "Items"),
    ("characters", "Character Options"),
    ("skills", "Skills"),
    ("conditions", "Conditions"),
    ("rules", "Rules"),
)


@dataclass(frozen=True)
class CompendiumEntry:
    """One thing in the compendium, with a stable anchor id the UI can link to."""

    id: str
    name: str
    summary: str
    text: str
    source: str  # "rules", "world", or "campaign"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompendiumSection:
    """One chapter of the compendium, such as the bestiary or the item list."""

    id: str
    name: str
    entries: tuple[CompendiumEntry, ...]


@dataclass(frozen=True)
class Compendium:
    """Everything the player may read about a rule set, a world, and a campaign."""

    world: WorldTemplate | None
    rule_set: RuleSet | None
    sections: tuple[CompendiumSection, ...]
    # name_key and raw name -> entry id, for linking carried and shop items.
    item_links: Mapping[str, str] = field(default_factory=dict)
    # Feature id, feature name key, and raw name -> entry id.
    ability_links: Mapping[str, str] = field(default_factory=dict)
    # Monster id, name key, and raw name -> entry id.
    creature_links: Mapping[str, str] = field(default_factory=dict)
    # Place name key and raw name -> entry id.
    place_links: Mapping[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(section.entries) for section in self.sections)

    @property
    def empty(self) -> bool:
        return self.total == 0


def compendium_view(
    library: ContentLibrary,
    rule_set_id: str,
    world_id: str,
    state: CampaignState | None = None,
) -> Compendium:
    """Build the compendium for a rule set and world, plus the discovered parts of a campaign.

    An unknown rule set or world gives an empty compendium rather than an error, so a stale link
    or a handbook page with no world still renders.
    """
    rule_set = library.rule_sets.get(rule_set_id)
    world = library.world_templates.get(world_id)
    if rule_set is None or world is None:
        return Compendium(world=None, rule_set=None, sections=())
    return _build(library, rule_set, world, state)


@dataclass
class _Draft:
    """An entry before its anchor id is assigned."""

    name: str
    summary: str = ""
    text: str = ""
    source: str = "rules"
    tags: tuple[str, ...] = ()


def _build(
    library: ContentLibrary,
    rule_set: RuleSet,
    world: WorldTemplate,
    state: CampaignState | None,
) -> Compendium:
    spec = rule_set.character
    world_content = _world_content(library, world, rule_set)

    # The world chapter: the world, its regions, and its place names. Only a discovered place's
    # player-facing summary is ever added from live play; its GM description never is.
    world_drafts: dict[str, _Draft] = {
        "world": _Draft(
            name=world.name,
            summary=world.blurb or "",
            text=world.description,
            source="world",
        )
    }
    places: dict[str, _Draft] = {}
    place_aliases: list[tuple[str, str]] = []
    for region in world.regions:
        kind = rule_set.world_terrain.get(region.terrain)
        terrain = kind.name.casefold() if kind is not None else region.terrain
        world_drafts[f"region:{name_key(region.name)}"] = _Draft(
            name=region.name,
            text=f"A region of {terrain} ground.",
            source="world",
        )
    location_names = {location.id: location.name for location in world.locations}
    for location in world.locations:
        key = f"place:{name_key(location.name)}"
        if location.parent is None:
            text = f"A place in {world.name}."
        else:
            parent = location_names.get(location.parent, location.parent)
            text = f"Inside {parent}."
        places.setdefault(key, _Draft(name=location.name, text=text, source="world"))
        _alias(place_aliases, (location.name, name_key(location.name)), key)

    # The bestiary: the rule set's monsters and the world's own, keyed by name so a rule set
    # monster wins when a world names the same creature.
    bestiary: dict[str, _Draft] = {}
    creature_aliases: list[tuple[str, str]] = []
    creature_ids: dict[str, str] = {}
    for monster in rule_set.monsters.values():
        key = name_key(monster.name)
        bestiary.setdefault(key, _monster_draft(monster, spec))
        creature_ids.setdefault(monster.id, key)
        _alias(creature_aliases, _monster_aliases(monster), key)
    # A world monster built from an NPC is an NPC template, not a player-safe sourcebook
    # creature, so leave it out. The statblock's prose is a paraphrase of the NPC's GM record.
    npc_ids = {npc.id for npc in world.npcs}
    npc_keys = {name_key(npc.id) for npc in world.npcs} | {name_key(npc.name) for npc in world.npcs}
    if world_content is not None:
        for monster in world_content.monsters.values():
            if (
                monster.id in npc_ids
                or name_key(monster.id) in npc_keys
                or name_key(monster.name) in npc_keys
            ):
                continue
            key = name_key(monster.name)
            bestiary.setdefault(key, _monster_draft(monster, spec, source="world"))
            creature_ids.setdefault(monster.id, key)
            _alias(creature_aliases, _monster_aliases(monster), key)

    # Abilities and items: the rule set's features first, then the world's own features. A world
    # feature with a rule set feature's name is an override already covered by the rule set.
    abilities: dict[str, _Draft] = {}
    ability_aliases: list[tuple[str, str]] = []
    items: dict[str, _Draft] = {}
    item_aliases: list[tuple[str, str]] = []
    for feature in spec.features.values():
        _add_feature(feature, "rules", abilities, ability_aliases, items, item_aliases)
    if world_content is not None:
        for feature in world_content.features.values():
            _add_feature(feature, "world", abilities, ability_aliases, items, item_aliases)
    if rule_set.marketplace is not None:
        for shop_item in rule_set.marketplace.items:
            key = name_key(shop_item.name)
            items.setdefault(key, _shop_item_draft(shop_item))
            _alias(item_aliases, (shop_item.name, key), key)

    # The rest of the rule set's player-facing material.
    characters = [
        _template_draft(template.name, template.description, template.kind)
        for template in spec.templates.values()
    ]
    skills = [
        _Draft(
            name=skill.name,
            summary=_first_sentence(skill.description),
            text=skill.description,
        )
        for skill in spec.skills.values()
    ]
    conditions = [_condition_draft(condition, spec) for condition in rule_set.conditions.values()]
    rules = _rule_drafts(library, rule_set)

    if state is not None:
        _add_live_places(state, places, place_aliases)
        _add_live_creatures(state, spec, bestiary, creature_aliases, creature_ids)
        _add_live_items(state, items, item_aliases)

    world_drafts.update(places)

    used_ids: set[str] = set()
    section_drafts: list[tuple[str, list[tuple[str, _Draft]]]] = [
        ("world", list(world_drafts.items())),
        ("bestiary", list(bestiary.items())),
        ("abilities", list(abilities.items())),
        ("items", list(items.items())),
        ("characters", _numbered(characters)),
        ("skills", _numbered(skills)),
        ("conditions", _numbered(conditions)),
        ("rules", _numbered(rules)),
    ]
    sections: list[CompendiumSection] = []
    section_ids: dict[str, dict[str, str]] = {}
    for section_id, drafts in section_drafts:
        if not drafts:
            continue
        section, ids = _build_section(section_id, dict(SECTION_NAMES)[section_id], drafts, used_ids)
        sections.append(section)
        section_ids[section_id] = ids

    return Compendium(
        world=world,
        rule_set=rule_set,
        sections=tuple(sections),
        item_links=_links(item_aliases, section_ids.get("items", {})),
        ability_links=_links(ability_aliases, section_ids.get("abilities", {})),
        creature_links=_links(creature_aliases, section_ids.get("bestiary", {})),
        place_links=_links(place_aliases, section_ids.get("world", {})),
    )


def _world_content(library: ContentLibrary, world: WorldTemplate, rule_set: RuleSet):
    """A world's own content checked against the rule set, or None when it does not fit."""
    try:
        return library.world_content(world.id, rule_set.id)
    except ContentError:
        return None


def _alias(aliases: list[tuple[str, str]], names: Sequence[str], key: str) -> None:
    for name in names:
        if name:
            aliases.append((name, key))


def _monster_aliases(monster: MonsterTemplate) -> tuple[str, ...]:
    return (monster.id, monster.name, name_key(monster.name))


def _monster_draft(monster: MonsterTemplate, spec: CharacterSpec, source: str = "rules") -> _Draft:
    lines = [monster.description] if monster.description else []
    stats = _stats_line(monster.stats, spec)
    if stats:
        lines.append(stats)
    lines.append(f"Armor: {monster.armor}")
    attacks = _attacks_text(monster.attacks, monster.attack_flags, monster.attack_shock)
    if attacks:
        lines.append(attacks)
    if monster.attacks_per_action > 1:
        lines.append(f"Attacks per action: {monster.attacks_per_action}")
    if monster.large_group:
        lines.append("Fights as a large group.")
    for ability in monster.abilities:
        detail = [
            f"{label} {value}"
            for label, value in (
                ("trigger", ability.trigger),
                ("save", ability.save),
                ("condition", ability.condition),
                ("damage", ability.damage),
            )
            if value
        ]
        text = f"{ability.name}: {ability.description}"
        if detail:
            text += f" ({'; '.join(detail)})"
        lines.append(text)
    summary = _first_sentence(monster.description) or f"Armor {monster.armor}"
    return _Draft(name=monster.name, summary=summary, text="\n".join(lines), source=source)


def _stats_line(stats: Mapping[str, int], spec: CharacterSpec) -> str:
    if not stats:
        return ""
    parts = []
    for stat_id, value in stats.items():
        found = spec.stats.get(stat_id)
        name = found.name if found is not None else stat_id.upper()
        parts.append(f"{name} {value}")
    return f"Stats: {', '.join(parts)}"


def _attacks_text(
    attacks: Mapping[str, str],
    flags: Mapping[str, Sequence[str]],
    shock: Mapping[str, Sequence[int]],
) -> str:
    if not attacks:
        return ""
    parts = []
    for name, formula in attacks.items():
        text = f"{name} {formula}"
        attack_flags = flags.get(name) or ()
        if attack_flags:
            text += f" ({', '.join(attack_flags)})"
        pair = shock.get(name) or ()
        if len(pair) >= 2 and pair[0]:
            text += f" (Shock {pair[0]} / AC {pair[1]})"
        parts.append(text)
    return f"Attacks: {', '.join(parts)}"


def _add_feature(
    feature: Feature,
    source: str,
    abilities: dict[str, _Draft],
    ability_aliases: list[tuple[str, str]],
    items: dict[str, _Draft],
    item_aliases: list[tuple[str, str]],
) -> None:
    """Add a feature to the item list or the ability list, with its link aliases."""
    key = name_key(feature.name)
    if feature.feature_type == "item":
        items.setdefault(key, _feature_item_draft(feature, source))
        _alias(item_aliases, (feature.name, key), key)
        return
    tags = tuple(tag for tag in (feature.feature_type, feature.tag) if tag)
    abilities.setdefault(
        key,
        _Draft(
            name=feature.name,
            summary=_first_sentence(feature.description),
            text=_feature_text(feature, item=False),
            source=source,
            tags=tags,
        ),
    )
    _alias(ability_aliases, (feature.id, feature.name, key), key)


def _feature_text(feature: Feature, *, item: bool) -> str:
    lines = [feature.description] if feature.description else []
    details = [f"Type: {feature.feature_type}"]
    if feature.tag:
        details.append(f"Tag: {feature.tag}")
    if feature.rank:
        details.append(f"Rank: {feature.rank}")
    if feature.damage:
        details.append(f"Damage: {feature.damage}")
    if feature.armor:
        details.append(f"Armor: {feature.armor}")
    if feature.uses:
        details.append(f"Uses: {feature.uses}")
    if feature.shock:
        details.append(f"Shock: {feature.shock} / AC {feature.shock_ac}")
    if item and feature.size:
        details.append(f"Size: {feature.size}")
    return "\n".join(lines + details)


def _feature_item_draft(feature: Feature, source: str) -> _Draft:
    return _Draft(
        name=feature.name,
        summary=_first_sentence(feature.description),
        text=_feature_text(feature, item=True),
        source=source,
        tags=(feature.tag,) if feature.tag else (),
    )


def _shop_item_draft(item: ShopItem) -> _Draft:
    lines = [item.description] if item.description else []
    lines.append(f"Category: {item.category}")
    lines.append(f"Price: {item.price}")
    lines.append(f"Size: {item.size}")
    if item.armor:
        lines.append(f"Armor: {item.armor}")
    if item.damage:
        lines.append(f"Damage: {item.damage}")
    if item.uses:
        lines.append(f"Uses: {item.uses}")
    summary = _first_sentence(item.description) or item.category.title()
    return _Draft(
        name=item.name,
        summary=summary,
        text="\n".join(lines),
        tags=(item.category,) if item.category else (),
    )


def _template_draft(name: str, description: str, kind: str) -> _Draft:
    lines = [description] if description else []
    lines.append(f"Kind: {kind_label(kind)}")
    return _Draft(
        name=name,
        summary=_first_sentence(description),
        text="\n".join(lines),
        tags=(kind,),
    )


def _condition_draft(condition: ConditionRules, spec: CharacterSpec) -> _Draft:
    lines = [condition.description] if condition.description else []
    if condition.ends:
        lines.append(f"Ends: {', '.join(condition.ends)}")
    if condition.modifies:
        parts = []
        for stat, amount in condition.modifies.items():
            if stat == "*":
                name = "all stats"
            elif stat in spec.stats:
                name = spec.stats[stat].name
            else:
                name = stat
            parts.append(f"{name} {amount:+d}")
        lines.append(f"Modifies: {', '.join(parts)}")
    return _Draft(
        name=condition.label,
        summary=_first_sentence(condition.description),
        text="\n".join(lines),
    )


def _rule_drafts(library: ContentLibrary, rule_set: RuleSet) -> list[_Draft]:
    """The rule set's own lore rules. Core's rules are GM instructions, not player lore, so a rule
    whose id and text are core's is left out."""
    drafts = []
    for rule_id, text in rule_set.rules.items():
        if library.core.rules.get(rule_id) == text:
            continue
        drafts.append(
            _Draft(
                name=rule_id.replace("-", " ").title(),
                summary=_first_sentence(text),
                text=text,
            )
        )
    return drafts


def _add_live_places(
    state: CampaignState,
    places: dict[str, _Draft],
    aliases: list[tuple[str, str]],
) -> None:
    """Add only discovered places, and only the summary the player may see."""
    for place in state.places.values():
        if not place.discovered:
            continue
        key = f"place:{name_key(place.name)}"
        summary = place.summary or ""
        # The place's own description is the GM's record and may hold secrets, so a place the
        # player has discovered but not been told about still gets a readable, safe entry.
        places[key] = _Draft(
            name=place.name,
            summary=summary,
            text=summary or "A place you have discovered.",
            source="campaign",
        )
        _alias(aliases, (place.name, name_key(place.name)), key)


def _add_live_creatures(
    state: CampaignState,
    spec: CharacterSpec,
    bestiary: dict[str, _Draft],
    aliases: list[tuple[str, str]],
    static_ids: Mapping[str, str],
) -> None:
    """Add an encountered creature when no sourcebook statblock already covers it. The NPC's own
    description is a GM record, so the entry carries only what the player has been told."""
    for character in state.characters.values():
        if character.kind == PC_KIND or character.hireling:
            continue
        if not character.creature and not character.attacks:
            continue
        key = name_key(character.name)
        creature_key = name_key(character.creature) if character.creature else ""
        existing = key if key in bestiary else ""
        if not existing and creature_key and creature_key in bestiary:
            existing = creature_key
        if not existing and character.creature in static_ids:
            existing = static_ids[character.creature]
        if not existing:
            existing = key
            bestiary[key] = _live_creature_draft(character, state, spec)
        names = [character.name, key]
        if character.creature:
            names += [character.creature, creature_key]
        _alias(aliases, names, existing)


def _live_creature_draft(character: Character, state: CampaignState, spec: CharacterSpec) -> _Draft:
    known = state.known.get(name_key(character.name))
    description = known.description if known is not None else ""
    lines = [description] if description else []
    if character.creature:
        lines.append(f"Creature: {character.creature}.")
    stats = _stats_line(character.stats, spec)
    if stats:
        lines.append(stats)
    lines.append(f"Armor: {character.armor}")
    attacks = _attacks_text(character.attacks, character.attack_flags, character.attack_shock)
    if attacks:
        lines.append(attacks)
    summary = _first_sentence(description) or f"Armor {character.armor}"
    return _Draft(
        name=character.name,
        summary=summary,
        text="\n".join(lines),
        source="campaign",
    )


def _add_live_items(
    state: CampaignState,
    items: dict[str, _Draft],
    aliases: list[tuple[str, str]],
) -> None:
    """Add custom items the campaign carries, so gear the GM invented still links."""
    for character in state.characters.values():
        for item in character.items:
            key = name_key(item.name)
            items.setdefault(key, _live_item_draft(item))
            _alias(aliases, (item.name, key), key)


def _live_item_draft(item: Item) -> _Draft:
    lines = [item.text] if item.text else []
    if item.damage:
        lines.append(f"Damage: {item.damage}")
    if item.size:
        lines.append(f"Size: {item.size}")
    return _Draft(
        name=item.name,
        summary=_first_sentence(item.text),
        text="\n".join(lines),
        source="campaign",
    )


def _build_section(
    section_id: str,
    name: str,
    keyed: list[tuple[str, _Draft]],
    used_ids: set[str],
) -> tuple[CompendiumSection, dict[str, str]]:
    """Sort a section by name and give each entry a unique anchor id."""
    ordered = sorted(keyed, key=lambda pair: (pair[1].name.casefold(), _slug(pair[1].name)))
    entries = []
    ids: dict[str, str] = {}
    for key, draft in ordered:
        ident = _unique_id(f"compendium-{section_id}-{_slug(draft.name)}", used_ids)
        entries.append(
            CompendiumEntry(
                id=ident,
                name=draft.name,
                summary=draft.summary,
                text=draft.text,
                source=draft.source,
                tags=draft.tags,
            )
        )
        ids[key] = ident
    return CompendiumSection(id=section_id, name=name, entries=tuple(entries)), ids


def _links(aliases: list[tuple[str, str]], ids: Mapping[str, str]) -> dict[str, str]:
    links: dict[str, str] = {}
    for alias, key in aliases:
        ident = ids.get(key)
        if ident is not None:
            links.setdefault(alias, ident)
    return links


def _numbered(drafts: list[_Draft]) -> list[tuple[str, _Draft]]:
    """Key drafts that have no lookup aliases by their position."""
    return [(str(index), draft) for index, draft in enumerate(drafts)]


_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


def _first_sentence(text: str) -> str:
    """The first sentence of a description, or the whole short text when it has no stop."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    return _SENTENCE_BREAK.split(text, maxsplit=1)[0]


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return slug or "entry"


def _unique_id(base: str, used: set[str]) -> str:
    ident = base
    suffix = 2
    while ident in used:
        ident = f"{base}-{suffix}"
        suffix += 1
    used.add(ident)
    return ident
