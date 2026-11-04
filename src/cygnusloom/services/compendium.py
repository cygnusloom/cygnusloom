"""A sourcebook-style compendium built from a rule set, a world, and a campaign's live state.

The compendium holds what a player could read in a sourcebook: the rule set's monsters, abilities,
items, character options, skills, conditions, and lore rules; a world's own monsters, features,
regions, and place names; and a campaign's discovered places, encountered creatures, and custom
items. It never reads GM-only text: NPC descriptions, notes, settings, the journal, encounter maps,
undiscovered places, world rules, and world tables all stay out.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from cygnusloom.content.character import (
    CharacterSpec,
    Feature,
    Field,
    Stat,
    TableOption,
    Template,
    kind_label,
    reaction_effect_text,
)
from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import (
    AdvancementOption,
    CheckRules,
    ConditionRules,
    ContentLibrary,
    CounterRules,
    DiceTable,
    EncounterObject,
    EncounterTerrain,
    Hireling,
    InventoryRules,
    ItemKind,
    MagicRules,
    Marketplace,
    MonsterTemplate,
    MoveRules,
    PeriodRules,
    ProjectDefinition,
    RelationRules,
    RestLength,
    RuleSet,
    ShopItem,
    ShopService,
    TallyRules,
    Travel,
    Wilderness,
    WorldTemplate,
    WorldTerrain,
)
from cygnusloom.services import custom_features
from cygnusloom.services.events import (
    PC_KIND,
    CampaignState,
    Character,
    Item,
    name_key,
)

# The top-level tabs, in the order a reader opens them.
TABS = (
    ("world", "World"),
    ("characters", "Characters"),
    ("abilities", "Abilities"),
    ("items", "Items"),
    ("bestiary", "Bestiary"),
    ("rules", "Rules"),
    ("tables", "Tables"),
)

# The order of subheadings inside a chapter. Unlisted groups follow alphabetically.
GROUP_ORDER = {
    "Overview": 0,
    "Monsters": 1,
    "Encountered": 2,
    "NPCs": 3,
    "Regions": 4,
    "Places": 5,
    "Discovered Places": 6,
    "World Terrain": 7,
    "Encounter Terrain": 8,
    "Encounter Objects": 9,
    "Encounter Maps": 10,
    "Map Scales": 11,
    "Factions": 12,
    "Spells": 20,
    "Arts": 21,
    "Foci": 22,
    "Traits": 23,
    "Feats": 24,
    "Counters": 30,
    "Advancements": 31,
    "Periods": 32,
    "Tallies": 33,
    "Relations": 34,
    "Rules": 40,
    "Attached Rules": 41,
    "Conditions": 42,
    "Checks": 43,
    "Moves": 44,
    "Projects": 45,
    "Rests": 46,
    "Travel": 47,
    "Wilderness": 48,
    "Wilderness Actions": 49,
    "Combat": 50,
    "Character Tables": 60,
    "Tables": 61,
    "Creative Tables": 62,
    "Random Tables": 63,
}

# Every chapter, and the tab it lives in, in reading order.
SECTION_ORDER = (
    ("world", "world", "The World"),
    ("places", "world", "Places"),
    ("factions", "world", "Factions"),
    ("terrain", "world", "Terrain"),
    ("options", "characters", "Character Options"),
    ("stats", "characters", "Attributes"),
    ("skills", "characters", "Skills"),
    ("fields", "characters", "Character Fields"),
    ("advancement", "characters", "Advancement"),
    ("abilities", "abilities", "Abilities"),
    ("items", "items", "Items"),
    ("bestiary", "bestiary", "Bestiary"),
    ("rules", "rules", "Rules"),
    ("conditions", "rules", "Conditions"),
    ("checks", "rules", "Checks"),
    ("moves", "rules", "Moves"),
    ("projects", "rules", "Projects"),
    ("rests", "rules", "Rests and Travel"),
    ("tables", "tables", "Tables"),
    ("dice", "tables", "Dice Tables"),
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
    # A subheading inside its section, such as a template kind or a spell's group.
    group: str = ""
    # An icon id from the registry, shown beside the entry's heading when it has one.
    icon: str = ""


@dataclass(frozen=True)
class CompendiumGroup:
    """A subheading inside a chapter, with the entries under it."""

    name: str
    entries: tuple[CompendiumEntry, ...]


@dataclass(frozen=True)
class CompendiumSection:
    """One chapter of the compendium, such as the bestiary or the item list."""

    id: str
    name: str
    entries: tuple[CompendiumEntry, ...]
    tab: str = "world"

    @property
    def groups(self) -> tuple[CompendiumGroup, ...]:
        """The entries split by their subheading, in the section's own order."""
        grouped: dict[str, list[CompendiumEntry]] = {}
        for entry in self.entries:
            grouped.setdefault(entry.group, []).append(entry)
        return tuple(
            CompendiumGroup(name=name, entries=tuple(entries)) for name, entries in grouped.items()
        )


@dataclass(frozen=True)
class CompendiumTab:
    """A top-level page of the compendium, holding one or more chapters."""

    id: str
    name: str
    sections: tuple[CompendiumSection, ...]

    @property
    def count(self) -> int:
        return sum(len(section.entries) for section in self.sections)


@dataclass(frozen=True)
class Compendium:
    """Everything the player may read about a rule set, a world, and a campaign."""

    world: WorldTemplate | None
    rule_set: RuleSet | None
    sections: tuple[CompendiumSection, ...]
    # name_key and raw name -> entry id, for linking carried and shop items.
    item_links: Mapping[str, str] = dataclass_field(default_factory=dict)
    # Feature id, feature name key, and raw name -> entry id.
    ability_links: Mapping[str, str] = dataclass_field(default_factory=dict)
    # Monster id, name key, and raw name -> entry id.
    creature_links: Mapping[str, str] = dataclass_field(default_factory=dict)
    # Place name key and raw name -> entry id.
    place_links: Mapping[str, str] = dataclass_field(default_factory=dict)
    # Names a character sheet may print -> entry id, so its lines can link into the compendium.
    sheet_links: Mapping[str, str] = dataclass_field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(section.entries) for section in self.sections)

    @property
    def empty(self) -> bool:
        return self.total == 0

    @property
    def tabs(self) -> tuple[CompendiumTab, ...]:
        """The chapters grouped into their tabs, in reading order."""
        by_tab: dict[str, list[CompendiumSection]] = defaultdict(list)
        for section in self.sections:
            by_tab[section.tab].append(section)
        return tuple(
            CompendiumTab(id=tab_id, name=name, sections=tuple(by_tab[tab_id]))
            for tab_id, name in TABS
            if by_tab[tab_id]
        )


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
    group: str = ""
    icon: str = ""


def _build(
    library: ContentLibrary,
    rule_set: RuleSet,
    world: WorldTemplate,
    state: CampaignState | None,
) -> Compendium:
    spec = rule_set.character
    world_content = _world_content(library, world, rule_set)
    world_rules = world_content.rules if world_content is not None else {}
    safe_rule_text = {**rule_set.rules, **rule_set.attached_rules}
    rule_text = {**safe_rule_text, **world_rules}
    drafts: dict[str, dict[str, _Draft]] = {sid: {} for sid, _, _ in SECTION_ORDER}
    aliases: dict[str, list[tuple[str, str]]] = defaultdict(list)

    def add(
        section_id: str,
        key: str,
        draft: _Draft,
        category: str = "",
        names: Sequence[str] = (),
    ) -> None:
        drafts[section_id][key] = draft
        if category:
            _alias(aliases[category], names or (draft.name, name_key(draft.name)), key)

    # The world chapter: the world, its regions, its places, and the kinds of ground and scenery
    # its maps are built from.
    add(
        "world",
        "world",
        _Draft(
            name=world.name,
            summary=world.blurb or "",
            text=world.description,
            source="world",
            group="Overview",
        ),
    )
    location_names = {location.id: location.name for location in world.locations}
    for region in world.regions:
        terrain = rule_set.world_terrain.get(region.terrain)
        terrain_name = terrain.name.casefold() if terrain is not None else region.terrain
        add(
            "places",
            f"region:{region.id}",
            _Draft(
                name=region.name,
                text=f"A region of {terrain_name} ground.",
                source="world",
                group="Regions",
            ),
            "place",
        )
    for location in world.locations:
        if location.parent is None:
            text = f"A place in {world.name}."
        else:
            parent = location_names.get(location.parent, location.parent)
            text = f"Inside {parent}."
        add(
            "places",
            f"place:{name_key(location.name)}",
            _Draft(
                name=location.name,
                text=text,
                source="world",
                group="Places",
                icon=location.icon or "",
            ),
            "place",
        )
    for terrain in rule_set.world_terrain.values():
        add(
            "terrain",
            f"world:{terrain.id}",
            _Draft(name=terrain.name, text=_terrain_text(terrain), group="World Terrain"),
        )
    for terrain in rule_set.terrain.values():
        add(
            "terrain",
            f"encounter:{terrain.id}",
            _Draft(
                name=terrain.name,
                text=_encounter_terrain_text(terrain),
                group="Encounter Terrain",
            ),
        )
    for obj in rule_set.encounter_objects.values():
        add(
            "terrain",
            f"object:{obj.id}",
            _Draft(name=obj.name, text=_object_text(obj), group="Encounter Objects"),
        )
    for encounter in rule_set.encounter_templates.values():
        add(
            "terrain",
            f"map:{encounter.id}",
            _Draft(
                name=encounter.name,
                text=(
                    f"{encounter.columns} columns by {encounter.rows} rows. "
                    f"Background: {encounter.background}."
                ),
                group="Encounter Maps",
            ),
        )
    if rule_set.grid is not None:
        add(
            "terrain",
            "grid",
            _Draft(
                name="Encounter Grid",
                text=(
                    f"One cell is {rule_set.grid.cell_feet} feet. A character moves "
                    f"{rule_set.grid.move_feet} feet ({rule_set.grid.move_cells} cells) in one turn."
                ),
                group="Map Scales",
            ),
        )

    # Character options, attributes, skills, fields, and advancement.
    add("options", "creation", _character_creation_draft(spec, rule_set))
    for template in spec.templates.values():
        add(
            "options",
            f"template:{template.id}",
            _template_draft(template, spec),
            "template",
            (template.id, template.name, name_key(template.name)),
        )
    for stat in spec.stats.values():
        add("stats", f"stat:{stat.id}", _stat_draft(stat), "stat")
    for skill in spec.skills.values():
        add(
            "skills",
            f"skill:{skill.id}",
            _Draft(
                name=skill.name,
                summary=_first_sentence(skill.description),
                text=skill.description,
                group="Skills",
            ),
            "skill",
        )
    for character_field in spec.fields.values():
        add(
            "fields",
            f"character:{character_field.id}",
            _field_draft(character_field, "Character", spec),
            "field",
            (character_field.id, character_field.name, name_key(character_field.name)),
        )
    for campaign_field in rule_set.campaign.fields.values():
        add(
            "fields",
            f"campaign:{campaign_field.id}",
            _field_draft(campaign_field, "Campaign", spec),
            "field",
            (campaign_field.id, campaign_field.name, name_key(campaign_field.name)),
        )
    for counter in rule_set.progression.counters.values():
        add(
            "advancement",
            f"counter:{counter.id}",
            _counter_draft(counter),
            "counter",
            (counter.id, counter.label, name_key(counter.label)),
        )
    for advancement in rule_set.progression.advancements.values():
        for option_id, option in advancement.options.items():
            add(
                "advancement",
                f"option:{advancement.id}:{option_id}",
                _advancement_draft(advancement.label, option),
            )
    for period in rule_set.progression.periods.values():
        add("advancement", f"period:{period.id}", _period_draft(period))
    for tally in rule_set.progression.tallies.values():
        add("advancement", f"tally:{tally.id}", _tally_draft(tally))
    for relation in rule_set.progression.relations.values():
        add("advancement", f"relation:{relation.id}", _relation_draft(relation))

    # Abilities and items: the rule set's features first, then the world's own features. A world
    # feature with a rule set feature's name is an override already covered by the rule set.
    feature_names = {name_key(feature.name) for feature in spec.features.values()}
    for feature in spec.features.values():
        if feature.feature_type == "item":
            add(
                "items",
                f"feature:{feature.id}",
                _feature_item_draft(feature, "rules", rule_text),
                "item",
                (feature.id, feature.name, name_key(feature.name)),
            )
        else:
            add(
                "abilities",
                f"feature:{feature.id}",
                _feature_draft(feature, rule_set, rule_text, "rules"),
                "ability",
                (feature.id, feature.name, name_key(feature.name)),
            )
    if world_content is not None:
        for feature in world_content.features.values():
            if name_key(feature.name) in feature_names:
                continue
            if feature.feature_type == "item":
                add(
                    "items",
                    f"world-feature:{feature.id}",
                    _feature_item_draft(feature, "world", rule_text),
                    "item",
                    (feature.id, feature.name, name_key(feature.name)),
                )
            else:
                add(
                    "abilities",
                    f"world-feature:{feature.id}",
                    _feature_draft(feature, rule_set, rule_text, "world"),
                    "ability",
                    (feature.id, feature.name, name_key(feature.name)),
                )

    item_names: dict[str, str] = {}
    for key, draft in drafts["items"].items():
        if draft.name:
            item_names.setdefault(name_key(draft.name), key)
    if rule_set.marketplace is not None:
        marketplace = rule_set.marketplace
        add("items", "marketplace", _marketplace_draft(marketplace, spec))
        for shop_item in marketplace.items:
            key = name_key(shop_item.name)
            if key in item_names:
                continue
            item_names[key] = f"shop:{shop_item.id}"
            add(
                "items",
                f"shop:{shop_item.id}",
                _shop_item_draft(shop_item),
                "item",
                (shop_item.id, shop_item.name, key),
            )
        for service in marketplace.services:
            add("items", f"service:{service.id}", _service_draft(service))
        for hireling in marketplace.hirelings:
            add("items", f"hireling:{hireling.id}", _hireling_draft(hireling))
    if rule_set.magic is not None:
        for key, draft in _magic_drafts(rule_set.magic):
            add("items", key, draft)
    for size_id, slots in rule_set.item_sizes.items():
        add(
            "items",
            f"size:{size_id}",
            _Draft(
                name=size_id.replace("_", " ").title(),
                text=f"Takes {slots} inventory slot{'s' if slots != 1 else ''}.",
                group="Item Sizes",
            ),
        )
    if rule_set.inventory is not None:
        add("items", "inventory", _inventory_draft(rule_set.inventory))

    # The bestiary: the rule set's monsters and the world's own, keyed by name so a rule set
    # monster wins when a world names the same creature.
    bestiary_by_name: dict[str, str] = {}
    for monster in rule_set.monsters.values():
        key = f"monster:{monster.id}"
        add(
            "bestiary",
            key,
            _monster_draft(monster, spec, rule_text, "rules"),
            "creature",
            _monster_aliases(monster),
        )
        bestiary_by_name.setdefault(name_key(monster.name), key)
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
            key = f"world-monster:{monster.id}"
            add(
                "bestiary",
                key,
                _monster_draft(monster, spec, rule_text, "world"),
                "creature",
                _monster_aliases(monster),
            )
            bestiary_by_name.setdefault(name_key(monster.name), key)
    if rule_set.npc_hp:
        add("bestiary", "npc-hp", _npc_hp_draft(rule_set))

    # The rules chapter: the rule set's own rules, conditions, checks, moves, projects, rest and
    # travel procedures, and a summary of its combat system.
    add(
        "rules",
        "ruleset",
        _Draft(
            name=rule_set.name,
            summary=rule_set.blurb or "",
            text=rule_set.description,
            group="Overview",
        ),
    )
    for key, draft in _rule_drafts(library, rule_set):
        add("rules", key, draft)
    for condition in rule_set.conditions.values():
        add(
            "conditions",
            f"condition:{condition.id}",
            _condition_draft(condition, spec),
            "condition",
            (condition.id, condition.label, name_key(condition.label)),
        )
    for check in rule_set.checks.values():
        add("checks", f"check:{check.id}", _check_draft(check))
    for move in rule_set.moves.values():
        add("moves", f"move:{move.id}", _move_draft(move))
    for project in rule_set.projects.values():
        add("projects", f"project:{project.id}", _project_draft(project))
    for rest in rule_set.rests.values():
        add("rests", f"rest:{rest.id}", _rest_draft(rest))
    if rule_set.clock is not None:
        add(
            "rests",
            "clock",
            _Draft(
                name="The Day",
                text=(
                    "Parts of the day: "
                    + ", ".join(rule_set.clock.parts)
                    + f". Each part is {rule_set.clock.hours_per_part} hour"
                    + ("s" if rule_set.clock.hours_per_part != 1 else "")
                    + "."
                ),
                group="Time",
            ),
        )
    if rule_set.travel is not None:
        for key, draft in _travel_drafts(rule_set.travel):
            add("rests", key, draft)
    if rule_set.wilderness is not None:
        for key, draft in _wilderness_drafts(rule_set.wilderness):
            add("rests", key, draft)
    if rule_set.combat is not None:
        add("rests", "combat", _combat_draft(rule_set.combat, spec))

    # Tables: every character-facing option table and every dice table in the rule set.
    table_fields = {
        field.table
        for field in (*spec.fields.values(), *rule_set.campaign.fields.values())
        if field.table
    }
    template_tables = {table for template in spec.templates.values() for table in template.tables}
    for table_id, options in rule_set.tables.items():
        name = spec.table_names.get(table_id, table_id.replace("_", " ").title())
        group = "Character Tables" if table_id in table_fields | template_tables else "Tables"
        add(
            "tables",
            f"table:{table_id}",
            _table_draft(name, options, group),
            "table",
            (table_id, name, name_key(name)),
        )
    for table in rule_set.dice_tables.values():
        add("dice", f"dice:{table.id}", _dice_draft(table))

    if state is not None:
        _add_live_places(state, drafts["places"], aliases["place"])
        _add_live_creatures(
            state,
            spec,
            drafts["bestiary"],
            bestiary_by_name,
            aliases["creature"],
            safe_rule_text,
        )
        _add_live_items(state, drafts["items"], item_names, aliases["item"])
        _add_live_features(
            state,
            rule_set,
            rule_text,
            drafts["abilities"],
            aliases["ability"],
        )
        _add_live_factions(
            state,
            rule_set,
            drafts["factions"],
            aliases["faction"],
        )

    used_ids: set[str] = set()
    sections: list[CompendiumSection] = []
    section_ids: dict[str, dict[str, str]] = {}
    for section_id, tab, name in SECTION_ORDER:
        keyed = list(drafts[section_id].items())
        if not keyed:
            continue
        section, ids = _build_section(section_id, name, tab, keyed, used_ids)
        sections.append(section)
        section_ids[section_id] = ids

    section_for = {
        "item": "items",
        "ability": "abilities",
        "creature": "bestiary",
        "place": "places",
        "stat": "stats",
        "skill": "skills",
        "template": "options",
        "condition": "conditions",
        "counter": "advancement",
        "field": "fields",
        "table": "tables",
    }
    links = {
        category: _links(aliases[category], section_ids.get(section_id, {}))
        for category, section_id in section_for.items()
    }
    # A character sheet prints names, so its links follow the reader-facing names rather than the
    # ids used by tools. Fields win over tables when a field and a table share a name.
    sheet_links: dict[str, str] = {}
    for category in ("table", "stat", "skill", "template", "ability", "item", "condition"):
        sheet_links.update(links[category])
    sheet_links.update(links["counter"])
    sheet_links.update(links["field"])

    return Compendium(
        world=world,
        rule_set=rule_set,
        sections=tuple(sections),
        item_links=links["item"],
        ability_links=links["ability"],
        creature_links=links["creature"],
        place_links=links["place"],
        sheet_links=sheet_links,
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


def _monster_draft(
    monster: MonsterTemplate,
    spec: CharacterSpec,
    rule_text: Mapping[str, str],
    source: str = "rules",
) -> _Draft:
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
    lines.extend(_attached_rule_lines(monster.rules, monster.rule_text, rule_text))
    summary = _first_sentence(monster.description) or f"Armor {monster.armor}"
    return _Draft(
        name=monster.name,
        summary=summary,
        text="\n".join(lines),
        source=source,
        group="Monsters",
        icon=monster.icon,
    )


def _attached_rule_lines(
    rule_ids: Sequence[str],
    inline: Mapping[str, str] | None,
    rule_text: Mapping[str, str],
) -> list[str]:
    """The text of the rules a statblock or feature attaches, once each."""
    inline = inline or {}
    lines: list[str] = []
    seen: set[str] = set()
    for rule_id in rule_ids:
        text = inline.get(rule_id) or rule_text.get(rule_id)
        if text and text not in seen:
            seen.add(text)
            lines.append(f"{rule_id}: {text}")
    for rule_id, text in inline.items():
        if text and text not in seen:
            seen.add(text)
            lines.append(f"{rule_id}: {text}")
    return lines


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


def _feature_group(feature: Feature) -> str:
    """The subheading a feature belongs under, such as Spells or Foci."""
    tag = feature.tag.casefold().strip().replace("_", " ")
    if feature.feature_type == "spell":
        return f"Spells - {feature.tag.title()}" if feature.tag else "Spells"
    if tag in ("art", "arts"):
        return "Arts"
    if tag in ("focus", "foci"):
        return "Foci"
    if feature.feature_type == "trait":
        return "Traits"
    if feature.feature_type == "feat":
        return "Feats"
    return feature.feature_type.replace("_", " ").title() or "Abilities"


def _item_group(feature: Feature) -> str:
    return feature.tag.replace("_", " ").title() if feature.tag else "Equipment"


def _effect_text(values: Mapping[str, object]) -> str:
    return ", ".join(f"{key}={value}" for key, value in values.items())


def _feature_text(
    feature: Feature,
    rule_text: Mapping[str, str],
    *,
    item: bool,
) -> str:
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
    if feature.flags:
        details.append(f"Flags: {', '.join(feature.flags)}")
    if item and feature.size:
        details.append(f"Size: {feature.size}")
    lines.append("; ".join(details))
    for effect in feature.effects:
        lines.append(f"Effect: {_effect_text(effect)}")
    for reaction in feature.reactions:
        text = reaction_effect_text(reaction, feature)
        if reaction.uses:
            text += f" ({reaction.uses} per {reaction.reset})"
        lines.append(f"Reaction: {text}")
    for pick in feature.picks:
        choices = ", ".join(pick.choices) or "any"
        text = f"Pick: {pick.label} ({pick.count} from {choices}"
        if pick.exclude:
            text += f"; not {', '.join(pick.exclude)}"
        lines.append(text + ")")
    if feature.defense_floor is not None:
        floor = feature.defense_floor
        text = f"Defense floor: {floor.base}"
        if floor.counter:
            text += f" from {floor.counter}"
        if floor.divisor != 1:
            text += f" divided by {floor.divisor}"
        if floor.requires_no_armor:
            text += "; only without armor"
        if floor.requires_no_shield:
            text += "; only without a shield"
        lines.append(text)
    lines.extend(_attached_rule_lines(feature.rules, None, rule_text))
    return "\n".join(lines)


def _feature_draft(
    feature: Feature,
    rule_set: RuleSet,
    rule_text: Mapping[str, str],
    source: str,
) -> _Draft:
    tags = tuple(tag for tag in (feature.feature_type, feature.tag) if tag)
    return _Draft(
        name=feature.name,
        summary=_first_sentence(feature.description),
        text=_feature_text(feature, rule_text, item=False),
        source=source,
        tags=tags,
        group=_feature_group(feature),
    )


def _feature_item_draft(feature: Feature, source: str, rule_text: Mapping[str, str]) -> _Draft:
    return _Draft(
        name=feature.name,
        summary=_first_sentence(feature.description),
        text=_feature_text(feature, rule_text, item=True),
        source=source,
        tags=(feature.tag,) if feature.tag else (),
        group=_item_group(feature),
        icon=feature.icon or "",
    )


def _shop_item_draft(item: ShopItem) -> _Draft:
    lines = [item.description] if item.description else []
    lines.append(f"Category: {item.category}")
    lines.append(f"Price: {item.price}")
    lines.append(f"Size: {item.size}")
    if item.armor:
        lines.append(f"Armor: {item.armor} ({item.armor_mode})")
    if item.damage:
        lines.append(f"Damage: {item.damage}")
    if item.uses:
        lines.append(f"Uses: {item.uses}")
    if item.encumbrance_checks:
        lines.append(f"Encumbers checks: {', '.join(item.encumbrance_checks)}")
    summary = _first_sentence(item.description) or item.category.title()
    return _Draft(
        name=item.name,
        summary=summary,
        text="\n".join(lines),
        tags=(item.category,) if item.category else (),
        group=item.category.replace("_", " ").title() if item.category else "Equipment",
        icon=item.icon,
    )


def _service_draft(service: ShopService) -> _Draft:
    lines = [f"Price: {service.price}"]
    if service.fits != 1:
        lines.append(f"Fits: {service.fits} characters")
    if service.restores:
        lines.append(f"Restores: {', '.join(service.restores)}")
    lines.append(f"Effect: {service.effect}")
    return _Draft(
        name=service.name,
        summary=f"{service.effect.title()} service",
        text="\n".join(lines),
        group="Services",
    )


def _hireling_draft(hireling: Hireling) -> _Draft:
    return _Draft(
        name=hireling.name,
        summary=f"{hireling.wage_per_day} per day",
        text=f"Wage: {hireling.wage_per_day} per day.",
        group="Hirelings",
    )


def _marketplace_draft(marketplace: Marketplace, spec: CharacterSpec) -> _Draft:
    currency = spec.features.get(marketplace.currency)
    currency_name = currency.name if currency is not None else marketplace.currency
    return _Draft(
        name="Marketplace",
        summary=f"Prices in {currency_name}",
        text=(
            f"Prices are in {currency_name}. The catalogue lists {len(marketplace.items)} "
            f"items, {len(marketplace.services)} services, and {len(marketplace.hirelings)} "
            "hirelings."
        ),
        group="Marketplace",
    )


def _magic_drafts(magic: MagicRules) -> list[tuple[str, _Draft]]:
    drafts = []
    for role, kind in (
        ("reusable", magic.reusable),
        ("single_use", magic.single_use),
        ("charged", magic.charged),
    ):
        drafts.append(
            (
                f"magic:{role}",
                _Draft(
                    name=f"{role.replace('_', ' ').title()} Spell Items",
                    summary=f"Item kind: {kind.kind}",
                    text=_item_kind_text(kind),
                    group="Magic Items",
                ),
            )
        )
    drafts.append(
        (
            "magic:penalty",
            _Draft(
                name="Casting Cost",
                text=f"Each cast pays {magic.cast_penalty} item(s) of its kind.",
                group="Magic Items",
            ),
        )
    )
    return drafts


def _item_kind_text(kind: ItemKind) -> str:
    lines = [f"Kind: {kind.kind}"]
    if kind.size:
        lines.append(f"Size: {kind.size}")
    if kind.icon:
        lines.append(f"Icon: {kind.icon}")
    return "\n".join(lines)


def _inventory_draft(inventory: InventoryRules) -> _Draft:
    lines = [
        f"Capacity: {inventory.capacity} slots"
        if not inventory.capacity_stat
        else f"Capacity: {inventory.capacity_stat} x {inventory.capacity_multiplier}"
    ]
    lines.append(f"Penalty: {inventory.penalty_name}, lasting {inventory.penalty_lasts}")
    if inventory.full_stat:
        lines.append(f"A full inventory drains {inventory.full_stat}.")
    if inventory.pools:
        for pool in inventory.pools:
            if pool.capacity is not None:
                capacity = f"{pool.capacity} slots"
            else:
                capacity = f"{pool.stat} x {pool.multiplier} / {pool.divisor}"
            lines.append(f"{pool.name}: {capacity}")
    if inventory.readied_stat:
        lines.append(f"Readied slots: {inventory.readied_stat} / {inventory.readied_divisor}")
    return _Draft(
        name="Carrying",
        summary=f"{inventory.penalty_name} when full",
        text="\n".join(lines),
        group="Carrying",
    )


def _template_draft(template: Template, spec: CharacterSpec) -> _Draft:
    lines = [template.description] if template.description else []
    lines.append(f"Kind: {kind_label(template.kind)}")
    if template.features:
        lines.append(
            "Features: "
            + ", ".join(
                _feature_name(spec, granted.feature)
                + (f" x{granted.quantity}" if granted.quantity != "1" else "")
                for granted in template.features
            )
        )
    if template.stats:
        lines.append(
            "Stats: "
            + ", ".join(
                f"{value:+d} {_stat_name(spec, stat)}" for stat, value in template.stats.items()
            )
        )
    if template.counters:
        lines.append(
            "Counters: "
            + ", ".join(f"{value:+d} {counter}" for counter, value in template.counters.items())
        )
    if template.skills:
        lines.append(
            "Skills: "
            + ", ".join(
                f"{value:+d} {_skill_name(spec, skill)}" for skill, value in template.skills.items()
            )
        )
    if template.tables:
        lines.append(
            "Tables: "
            + ", ".join(
                spec.table_names.get(table, table.replace("_", " ").title())
                for table in template.tables
            )
        )
    if template.names:
        lines.append("Suggested names: " + ", ".join(template.names))
    if template.choose is not None:
        choose = template.choose
        lines.append(f"Choose: {choose.count} {choose.feature_type}(s) by {choose.tag_field}")
    for pick in template.skill_picks:
        lines.append(
            f"Skill pick: {pick.count} from "
            f"{spec.table_names.get(pick.table, pick.table.replace('_', ' ').title())}"
        )
    for pick in template.feat_picks:
        tags = ", ".join(pick.tags) or pick.tag or "any tag"
        lines.append(f"Feature pick: {pick.count} {pick.feature_type or 'feature'}(s) by {tags}")
    grant = template.grant
    if grant.items:
        lines.append("Starting items: " + ", ".join(grant.items))
    if grant.silver:
        lines.append(f"Starting money: {grant.silver}")
    if grant.equip:
        lines.append("Starting equipment: " + ", ".join(grant.equip))
    if template.compatible_with:
        for kind, ids in template.compatible_with.items():
            lines.append(f"Compatible with {kind_label(kind)}: {', '.join(ids)}")
    for requirement in template.template_requirements:
        lines.append("Requires: " + ", ".join(kind_label(kind) for kind in requirement.kinds))
    return _Draft(
        name=template.name,
        summary=_first_sentence(template.description),
        text="\n".join(lines),
        tags=(template.kind,),
        group=kind_label(template.kind),
    )


def _feature_name(spec: CharacterSpec, feature_id: str) -> str:
    feature = spec.features.get(feature_id)
    return feature.name if feature is not None else feature_id


def _stat_name(spec: CharacterSpec, stat_id: str) -> str:
    stat = spec.stats.get(stat_id)
    return stat.name if stat is not None else stat_id


def _skill_name(spec: CharacterSpec, skill_id: str) -> str:
    skill = spec.skills.get(skill_id)
    return skill.name if skill is not None else skill_id


def _character_creation_draft(spec: CharacterSpec, rule_set: RuleSet) -> _Draft:
    lines = [rule_set.description] if rule_set.description else []
    kinds = spec.kinds()
    if kinds:
        lines.append("Template kinds: " + ", ".join(kind_label(kind) for kind in kinds))
    if spec.optional_kinds:
        lines.append(
            "Optional selections: " + ", ".join(kind_label(kind) for kind in spec.optional_kinds)
        )
    if spec.skill_progression is not None:
        progression = spec.skill_progression
        lines.append(
            f"Skills: untrained {progression.untrained}, first level {progression.first_level}, "
            f"creation maximum {progression.creation_maximum}, maximum {progression.maximum}."
        )
    if spec.ordered_feature_tags:
        lines.append("Ranked feature tracks: " + ", ".join(spec.ordered_feature_tags))
    if rule_set.plus:
        lines.append(f"Dice plus mode: {rule_set.plus}.")
    if rule_set.campaign.fields:
        lines.append(
            "Campaign options: "
            + ", ".join(field.name for field in rule_set.campaign.fields.values())
        )
    return _Draft(
        name="Character Creation",
        summary="How characters are built with this rule set",
        text="\n".join(lines),
        group="Overview",
    )


def _stat_draft(stat: Stat) -> _Draft:
    lines = []
    if stat.derived is not None:
        lines.append(f"Derived: {_derived_text(stat.derived)}")
    else:
        if stat.formula:
            lines.append(f"Roll: {stat.formula}")
        if stat.values:
            lines.append("Assigned values: " + ", ".join(str(value) for value in stat.values))
        lines.append(f"Range: {stat.low}-{stat.high}")
    if stat.swap_group:
        lines.append(f"Swap group: {stat.swap_group}")
    if stat.plus_modifier:
        lines.append(f"Adds the {stat.plus_modifier} modifier")
    if stat.minimum is not None:
        lines.append(f"Minimum: {stat.minimum}")
    if stat.modifier_bands:
        lines.append(
            "Modifiers: "
            + ", ".join(f"{minimum}+: {modifier:+d}" for minimum, modifier in stat.modifier_bands)
        )
    return _Draft(
        name=stat.name,
        summary=_first_sentence(lines[0]) if lines else "",
        text="\n".join(lines),
        group="Attributes",
    )


def _derived_text(derived) -> str:
    parts = [str(derived.base)] if derived.base else []
    if derived.plus:
        parts.append(f"+ {derived.plus}")
    for counter in derived.minus_counters:
        parts.append(f"- {counter}")
    for stat in derived.best_modifier:
        parts.append(f"+ best {stat} modifier")
    for stat in derived.best_score:
        parts.append(f"+ best {stat} score")
    for stat in derived.minus_best_modifier:
        parts.append(f"- best {stat} modifier")
    for skill in derived.plus_skills:
        parts.append(f"+ {skill} level")
    for skill in derived.plus_skill_picks:
        parts.append(f"+ {skill} pick")
    for template in derived.minus_templates:
        parts.append(f"- 1 for {template}")
    if derived.requires_templates:
        parts.append("requires " + ", ".join(derived.requires_templates))
    if derived.minimum is not None:
        parts.append(f"minimum {derived.minimum}")
    return " ".join(parts) or "0"


def _field_draft(field: Field, group: str, spec: CharacterSpec) -> _Draft:
    lines = [field.description] if field.description else []
    if field.table is not None:
        table = spec.table_names.get(field.table, field.table.replace("_", " ").title())
        lines.append(f"Choose from the {table} table.")
    if field.group_roll:
        lines.append("Roll a group before choosing an option.")
    if field.restriction is not None:
        lines.append(
            f"Restricted while {field.restriction.field_id} is one of: "
            f"{', '.join(field.restriction.values)}."
        )
    if field.knows:
        lines.append(f"Picks a {field.knows} feature type.")
    return _Draft(
        name=field.name,
        summary=_first_sentence(field.description),
        text="\n".join(lines),
        group=group,
    )


def _counter_draft(counter: CounterRules) -> _Draft:
    maximum = "unlimited" if counter.maximum is None else str(counter.maximum)
    lines = [counter.description] if counter.description else []
    lines.append(f"Starts at {counter.initial}; range {counter.minimum} to {maximum}.")
    if counter.full:
        lines.append("When full: " + "; ".join(_effect_text(effect) for effect in counter.full))
    return _Draft(
        name=counter.label,
        summary=_first_sentence(counter.description),
        text="\n".join(lines),
        group="Counters",
    )


def _advancement_draft(advancement: str, option: AdvancementOption) -> _Draft:
    lines = [option.text]
    if option.limit is not None:
        lines.append(f"Limit: {option.limit}")
    if option.learn is not None:
        if option.learn.tag:
            detail = f" tagged {option.learn.tag}"
        elif option.learn.tag_field:
            detail = f" by {option.learn.tag_field}"
        else:
            detail = ""
        lines.append(f"Learn: {option.learn.feature_type}{detail}")
    if option.effects:
        lines.append("Effects: " + "; ".join(_effect_text(effect) for effect in option.effects))
    if option.picks:
        lines.append("The player picks the target.")
    return _Draft(
        name=option.label,
        summary=_first_sentence(option.text),
        text="\n".join(lines),
        tags=(advancement,),
        group="Advancements",
    )


def _period_draft(period: PeriodRules) -> _Draft:
    lines = []
    if period.effects:
        lines.append("Effects: " + "; ".join(_effect_text(effect) for effect in period.effects))
    if period.tallies:
        lines.append("Resets tallies: " + ", ".join(period.tallies))
    return _Draft(
        name=period.label,
        summary="A period that ends for every character at once",
        text="\n".join(lines),
        group="Periods",
    )


def _tally_draft(tally: TallyRules) -> _Draft:
    if tally.dynamic:
        return _Draft(
            name=tally.label,
            summary="Points kept by the campaign's factions",
            text="The campaign's factions are listed under Factions.",
            group="Tallies",
        )
    return _Draft(
        name=tally.label,
        summary=", ".join(tally.entries.values()),
        text="Entries: " + ", ".join(tally.entries.values()),
        group="Tallies",
    )


def _relation_draft(relation: RelationRules) -> _Draft:
    lines = [f"Limit: {relation.limit}" if relation.limit is not None else "No limit"]
    return _Draft(
        name=relation.label,
        summary="A named link between characters",
        text="\n".join(lines),
        group="Relations",
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
    if condition.derived is not None:
        derived = condition.derived
        lines.append(
            f"Shown while {derived.stat} is {derived.current}"
            + (" and its maximum is above 0" if derived.needs_max else "")
        )
    if condition.dying:
        lines.append("The character is dying.")
    if condition.fatal:
        lines.append("A failed save is fatal.")
    return _Draft(
        name=condition.label,
        summary=_first_sentence(condition.description),
        text="\n".join(lines),
        group="Conditions",
    )


def _check_draft(check: CheckRules) -> _Draft:
    return _Draft(
        name=check.label,
        summary=f"{check.dice} check",
        text=(
            f"Dice: {check.dice}\nUnskilled: {check.unskilled}\n"
            f"Default difficulty: {check.default_difficulty}\n"
            f"Difficulty range: {check.minimum_difficulty}-{check.maximum_difficulty}"
        ),
        group="Checks",
    )


def _move_draft(move: MoveRules) -> _Draft:
    lines = [move.description] if move.description else []
    stat = move.stat or "player's choice"
    if move.stat_choice:
        stat += f" ({', '.join(move.stat_choice)})"
    lines.append(f"Roll: {move.dice} + {stat}")
    if move.skill:
        lines.append(f"Skill: {move.skill}")
    for band in move.bands:
        threshold = "otherwise" if band.min is None else f"{band.min}+"
        lines.append(f"{band.label} ({threshold}): {band.text}")
    return _Draft(
        name=move.label,
        summary=_first_sentence(move.description),
        text="\n".join(lines),
        group="Moves",
    )


def _project_draft(project: ProjectDefinition) -> _Draft:
    lines = [project.description] if project.description else []
    lines.append(f"Target: {project.target}")
    if project.cost:
        lines.append(f"Cost: {project.cost} {project.currency}".strip())
    if project.skill:
        lines.append(f"Skill: {project.skill} at level {project.skill_level}")
    return _Draft(
        name=project.label,
        summary=_first_sentence(project.description),
        text="\n".join(lines),
        group="Projects",
    )


def _rest_draft(rest: RestLength) -> _Draft:
    length = []
    if rest.hours:
        length.append(f"{rest.hours} hour(s)")
    if rest.parts:
        length.append(f"{rest.parts} part(s) of a day")
    if rest.days:
        length.append(f"{rest.days} day(s)")
    if rest.until_morning:
        length.append("until morning")
    lines = ["Length: " + (", ".join(length) or "as called for")]
    if rest.full:
        lines.append("A full rest.")
    if rest.restores:
        lines.append("Restores: " + ", ".join(rest.restores))
    if rest.clears:
        lines.append("Clears: " + ", ".join(rest.clears))
    if rest.ends:
        lines.append("Counts as: " + ", ".join(rest.ends))
    if rest.upkeep:
        lines.append("Has upkeep.")
    return _Draft(
        name=rest.id.replace("_", " ").title(),
        summary=", ".join(length),
        text="\n".join(lines),
        group="Rests",
    )


def _travel_drafts(travel: Travel) -> list[tuple[str, _Draft]]:
    drafts = []
    for mode_id, mode in travel.modes.items():
        drafts.append(
            (
                f"travel:{mode_id}",
                _Draft(
                    name=mode.name,
                    summary=f"{mode.km_per_hour} km per hour",
                    text=f"{mode.km_per_hour} km per hour. {mode.note}".strip(),
                    group="Travel",
                ),
            )
        )
    drafts.append(
        (
            "travel:default",
            _Draft(
                name="An Unknown Leg",
                summary=f"{travel.default_leg_minutes} minutes",
                text=f"{travel.default_leg_minutes} minutes. {travel.default_leg_note}".strip(),
                group="Travel",
            ),
        )
    )
    return drafts


def _wilderness_drafts(wilderness: Wilderness) -> list[tuple[str, _Draft]]:
    drafts = [
        (
            "wilderness:procedure",
            _Draft(
                name="Wilderness Procedure",
                summary=f"Events: {wilderness.events_table}",
                text=(
                    f"Seasons: {', '.join(wilderness.seasons)}. A season lasts "
                    f"{wilderness.season_days} days. The events table is {wilderness.events_table}. "
                    f"Lost die: {wilderness.lost_die}."
                ),
                group="Wilderness",
            ),
        )
    ]
    for action in wilderness.actions.values():
        drafts.append(
            (
                f"wilderness:{action.id}",
                _Draft(
                    name=action.name,
                    summary=_first_sentence(action.text),
                    text=action.text,
                    group="Wilderness Actions",
                ),
            )
        )
    return drafts


def _combat_draft(combat, spec: CharacterSpec) -> _Draft:
    lines = [
        f"Check die: {combat.check_die}",
        f"Initiative stat: {_stat_name(spec, combat.initiative_stat)}",
        f"Hit mode: {combat.hit_mode}",
        f"Defense: {combat.defense_base}"
        + (f" + {_stat_name(spec, combat.defense_modifier)}" if combat.defense_modifier else ""),
        f"Armor maximum: {combat.armor_max}",
        f"Save: {combat.save.mode.replace('_', ' ')}",
    ]
    if combat.unarmed_damage:
        lines.append(f"Unarmed: {combat.unarmed_damage}")
    if combat.dying_hours:
        lines.append(f"Dying for {combat.dying_hours} hour(s)")
    if combat.dying_rounds:
        lines.append(f"Dying for {combat.dying_rounds} round(s) in an encounter")
    if combat.total_defense_bonus:
        lines.append(f"Total defense: +{combat.total_defense_bonus} AC")
    return _Draft(
        name="Combat",
        summary="How attacks, defense, initiative, and saves resolve",
        text="\n".join(lines),
        group="Combat",
    )


def _table_draft(name: str, options: Sequence[TableOption], group: str) -> _Draft:
    lines = []
    for option in options:
        if option.name is not None:
            lines.append(f"{option.name}: {option.description}")
        else:
            lines.append(option.description)
    return _Draft(
        name=name,
        summary=f"{len(options)} entries",
        text="\n".join(lines),
        group=group,
    )


def _dice_draft(table: DiceTable) -> _Draft:
    lines = [f"Roll: {table.dice}"]
    for row in table.rows:
        roll = str(row.low) if row.low == row.high else f"{row.low}-{row.high}"
        text = f"{roll}: {row.result}"
        if row.text:
            text += f" ({row.text})"
        lines.append(text)
    return _Draft(
        name=table.name,
        summary=f"{table.dice}, {len(table.rows)} entries",
        text="\n".join(lines),
        tags=("creative",) if table.creative else (),
        group="Creative Tables" if table.creative else "Random Tables",
    )


def _npc_hp_draft(rule_set: RuleSet) -> _Draft:
    tiers = ", ".join(f"{tier}: {value}" for tier, value in sorted(rule_set.npc_hp.items()))
    stat = _stat_name(rule_set.character, rule_set.npc_hp_stat) if rule_set.npc_hp_stat else "HP"
    return _Draft(
        name="NPC Hit Points",
        summary="Hit points for NPCs without a statblock",
        text=f"Use {stat}. Tiers: {tiers}.",
        group="NPCs",
    )


def _terrain_text(terrain: WorldTerrain) -> str:
    lines = [terrain.rule] if terrain.rule else []
    if terrain.pattern:
        lines.append(f"Pattern: {terrain.pattern}")
    lines.append(f"Colour: {terrain.color}")
    return "\n".join(lines)


def _encounter_terrain_text(terrain: EncounterTerrain) -> str:
    cost = f"{terrain.move_cost} movement per cell"
    return f"{terrain.rule}\nMove cost: {cost}.".strip()


def _object_text(obj: EncounterObject) -> str:
    lines = []
    if obj.blocks:
        lines.append("Blocks movement: no one can enter this cell.")
    if obj.solid:
        lines.append("Solid: drawn as a block of colour.")
    lines.append(f"Colour: {obj.color}")
    return "\n".join(lines)


def _rule_drafts(library: ContentLibrary, rule_set: RuleSet) -> list[tuple[str, _Draft]]:
    """The rule set's own lore rules and attached rules. Core's rules are GM instructions, not
    player lore, so a rule whose id and text are core's is left out."""
    drafts: list[tuple[str, _Draft]] = []
    for rule_id, text in rule_set.rules.items():
        if library.core.rules.get(rule_id) == text:
            continue
        drafts.append(
            (
                f"rule:{rule_id}",
                _Draft(
                    name=rule_id.replace("-", " ").title(),
                    summary=_first_sentence(text),
                    text=text,
                    group="Rules",
                ),
            )
        )
    for rule_id, text in rule_set.attached_rules.items():
        drafts.append(
            (
                f"attached:{rule_id}",
                _Draft(
                    name=rule_id.replace("-", " ").title(),
                    summary=_first_sentence(text),
                    text=text,
                    group="Attached Rules",
                ),
            )
        )
    return drafts


def _add_live_factions(
    state: CampaignState,
    rule_set: RuleSet,
    factions: dict[str, _Draft],
    aliases: list[tuple[str, str]],
) -> None:
    """Add the factions the GM invented, with their current cup points."""
    for faction in state.factions.values():
        tally = rule_set.tallies.get(faction.tally)
        points = state.tallies.get(faction.tally, {}).get(faction.entry, 0)
        key = f"faction:{faction.id}"
        lines = [faction.description] if faction.description else []
        if faction.trait:
            lines.append(f"Trait: {faction.trait}")
        if tally is not None:
            lines.append(f"{points} {tally.label}")
        if faction.assets:
            lines.append(f"Turn {faction.turn}")
            for asset in sorted(faction.assets.values(), key=lambda item: item.name.casefold()):
                readiness = "ready" if faction.asset_ready(asset) else "acted"
                lines.append(
                    f"{asset.name} ({asset.category} {asset.kind}): "
                    f"{asset.hp}/{asset.hp_max} HP, {readiness}"
                )
        factions[key] = _Draft(
            name=faction.name,
            summary=faction.trait or faction.description,
            text="\n".join(lines),
            source="campaign",
            group="Factions",
        )
        _alias(aliases, (faction.name, name_key(faction.name), faction.id), key)


def _add_live_features(
    state: CampaignState,
    rule_set: RuleSet,
    rule_text: Mapping[str, str],
    abilities: dict[str, _Draft],
    aliases: list[tuple[str, str]],
) -> None:
    """Add the features characters invented during play, such as researched spells."""
    for character in state.characters.values():
        for custom in character.custom_features.values():
            if custom.id not in character.features:
                continue
            key = f"custom:{name_key(character.name)}:{custom.id}"
            if key in abilities:
                continue
            abilities[key] = _feature_draft(
                custom_features.as_feature(custom), rule_set, rule_text, "campaign"
            )
            _alias(aliases, (custom.id, custom.name, name_key(custom.name)), key)


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
            group="Discovered Places",
            icon=place.icon or "",
        )
        _alias(aliases, (place.name, name_key(place.name)), key)


def _add_live_creatures(
    state: CampaignState,
    spec: CharacterSpec,
    bestiary: dict[str, _Draft],
    bestiary_by_name: Mapping[str, str],
    aliases: list[tuple[str, str]],
    rule_text: Mapping[str, str],
) -> None:
    """Add an encountered creature when no sourcebook statblock already covers it. The NPC's own
    description is a GM record, so the entry carries only what the player has been told."""
    for character in state.characters.values():
        if character.kind == PC_KIND or character.hireling:
            continue
        if not character.creature and not character.attacks:
            continue
        names = [character.name, name_key(character.name)]
        existing = ""
        if character.creature:
            existing = bestiary_by_name.get(name_key(character.creature), "")
        if not existing:
            existing = bestiary_by_name.get(name_key(character.name), "")
        if not existing:
            key = f"live:{name_key(character.name)}"
            bestiary.setdefault(key, _live_creature_draft(character, state, spec, rule_text))
            existing = key
        if character.creature:
            names += [character.creature, name_key(character.creature)]
        _alias(aliases, names, existing)


def _live_creature_draft(
    character: Character,
    state: CampaignState,
    spec: CharacterSpec,
    rule_text: Mapping[str, str],
) -> _Draft:
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
    lines.extend(_attached_rule_lines(character.rules, None, rule_text))
    summary = _first_sentence(description) or f"Armor {character.armor}"
    return _Draft(
        name=character.name,
        summary=summary,
        text="\n".join(lines),
        source="campaign",
        group="Encountered",
    )


def _add_live_items(
    state: CampaignState,
    items: dict[str, _Draft],
    items_by_name: dict[str, str],
    aliases: list[tuple[str, str]],
) -> None:
    """Add custom items the campaign carries, so gear the GM invented still links."""
    for character in state.characters.values():
        for item in character.items:
            key = name_key(item.name)
            existing = items_by_name.get(key)
            if existing is None:
                existing = f"live:{key}"
                items.setdefault(existing, _live_item_draft(item))
                items_by_name[key] = existing
            _alias(aliases, (item.name, key), existing)


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
        group="Encountered Loot",
    )


def _build_section(
    section_id: str,
    name: str,
    tab: str,
    keyed: list[tuple[str, _Draft]],
    used_ids: set[str],
) -> tuple[CompendiumSection, dict[str, str]]:
    """Sort a section by its group and name, then give each entry a unique anchor id."""
    ordered = sorted(
        keyed,
        key=lambda pair: (
            _group_rank(pair[1].group),
            pair[1].group.casefold(),
            pair[1].name.casefold(),
            _slug(pair[1].name),
        ),
    )
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
                group=draft.group,
                icon=draft.icon,
            )
        )
        ids[key] = ident
    return CompendiumSection(id=section_id, name=name, entries=tuple(entries), tab=tab), ids


def _group_rank(group: str) -> int:
    """The reading order of a subheading, with spell groups counted as Spells."""
    if group in GROUP_ORDER:
        return GROUP_ORDER[group]
    if group.startswith("Spells"):
        return GROUP_ORDER["Spells"]
    return 100


def _links(aliases: list[tuple[str, str]], ids: Mapping[str, str]) -> dict[str, str]:
    links: dict[str, str] = {}
    for alias, key in aliases:
        ident = ids.get(key)
        if ident is not None:
            links.setdefault(alias, ident)
    return links


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
