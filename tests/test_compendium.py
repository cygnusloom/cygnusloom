"""The compendium: player-facing sourcebook content, and the secrets it must never carry."""

from __future__ import annotations

import re
from collections.abc import Callable

from fastapi.testclient import TestClient

from cygnusforge.services.compendium import (
    Compendium,
    CompendiumSection,
    compendium_view,
)
from cygnusforge.services.events import name_key
from tests import factories
from tests.conftest import content_library


def _sections(compendium: Compendium) -> dict[str, CompendiumSection]:
    return {section.id: section for section in compendium.sections}


def _entry_names(compendium: Compendium, section_id: str) -> list[str]:
    section = _sections(compendium)[section_id]
    return [entry.name for entry in section.entries]


def _entry_ids(compendium: Compendium, section_id: str) -> set[str]:
    section = _sections(compendium)[section_id]
    return {entry.id for entry in section.entries}


def test_cairn_compendium_has_the_sourcebook_sections():
    library = content_library()
    rules = library.rule_sets["cairn2e"]
    compendium = compendium_view(library, "cairn2e", "twin-lakes")

    sections = _sections(compendium)
    assert {"world", "places", "terrain", "options", "stats", "items"} <= set(sections)
    assert {"bestiary", "rules", "conditions", "tables", "dice"} <= set(sections)
    monster = next(iter(rules.monsters.values()))
    assert monster.name in _entry_names(compendium, "bestiary")
    shop_item = rules.marketplace.items[0]
    assert shop_item.name in _entry_names(compendium, "items")


def test_compendium_tabs_group_the_sections():
    library = content_library()
    compendium = compendium_view(library, "cairn2e", "twin-lakes")

    assert [tab.id for tab in compendium.tabs] == [
        "world",
        "characters",
        "abilities",
        "items",
        "bestiary",
        "rules",
        "tables",
    ]
    assert all(
        tab.count == sum(len(section.entries) for section in tab.sections)
        for tab in compendium.tabs
    )
    assert sum(tab.count for tab in compendium.tabs) == compendium.total


def test_section_groups_keep_the_designed_reading_order():
    compendium = compendium_view(content_library(), "cairn2e", "twin-lakes")
    sections = _sections(compendium)

    assert [group.name for group in sections["world"].groups] == ["Overview"]
    assert [group.name for group in sections["places"].groups][:2] == ["Regions", "Places"]
    assert sections["options"].groups[0].name == "Overview"
    assert sections["bestiary"].groups[0].name == "Monsters"


def test_character_content_fills_the_character_and_table_tabs():
    library = content_library()
    rules = library.rule_sets["worldswithoutnumber"]
    compendium = compendium_view(library, "worldswithoutnumber", "unwritten-world")

    assert {
        "options",
        "stats",
        "skills",
        "fields",
        "advancement",
        "tables",
        "dice",
    } <= set(_sections(compendium))
    assert next(iter(rules.character.stats.values())).name in _entry_names(compendium, "stats")
    assert next(iter(rules.character.skills.values())).name in _entry_names(compendium, "skills")
    assert next(iter(rules.character.templates.values())).name in _entry_names(
        compendium, "options"
    )
    assert next(iter(rules.character.fields.values())).name in _entry_names(compendium, "fields")
    assert rules.character.tables
    assert next(iter(rules.character.table_names.values())) in _entry_names(compendium, "tables")
    assert next(iter(rules.dice_tables.values())).name in _entry_names(compendium, "dice")


def test_sheet_links_point_at_character_content():
    library = content_library()
    rules = library.rule_sets["rolltest"]
    compendium = compendium_view(library, "rolltest", "testhollow")
    template = next(iter(rules.character.templates.values()))
    feature = next(iter(rules.character.features.values()))
    stat = next(iter(rules.character.stats.values()))
    counter = next(iter(rules.progression.counters.values()))

    assert compendium.sheet_links[template.name] in _entry_ids(compendium, "options")
    assert compendium.sheet_links[feature.name] in _entry_ids(compendium, "abilities")
    assert compendium.sheet_links[stat.name] in _entry_ids(compendium, "stats")
    assert compendium.sheet_links[counter.label] in _entry_ids(compendium, "advancement")


def test_item_links_name_the_items_section_entries():
    library = content_library()
    shop_item = library.rule_sets["cairn2e"].marketplace.items[0]
    compendium = compendium_view(library, "cairn2e", "twin-lakes")

    entry_id = compendium.item_links[shop_item.name]
    assert compendium.item_links[name_key(shop_item.name)] == entry_id
    assert entry_id in _entry_ids(compendium, "items")


def test_ability_and_creature_links_use_ids_and_names():
    library = content_library()
    rules = library.rule_sets["cairn2e"]
    feature = next(
        feature for feature in rules.character.features.values() if feature.feature_type != "item"
    )
    monster = next(iter(rules.monsters.values()))
    compendium = compendium_view(library, "cairn2e", "twin-lakes")

    assert compendium.ability_links[feature.id] in _entry_ids(compendium, "abilities")
    assert compendium.ability_links[name_key(feature.name)] == compendium.ability_links[feature.id]
    assert compendium.creature_links[monster.id] in _entry_ids(compendium, "bestiary")


def test_world_secrets_stay_out_of_the_compendium():
    library = content_library()
    world = library.world_templates["twin-lakes"]
    compendium = compendium_view(library, "cairn2e", "twin-lakes")
    texts = "\n".join(entry.text for section in compendium.sections for entry in section.entries)
    names = {entry.name for section in compendium.sections for entry in section.entries}

    assert world.description in texts
    assert "outcome-vuk-reborn" not in texts
    assert "fake tithing record" not in texts
    for npc in world.npcs:
        assert npc.description not in texts
        assert npc.name not in names


def test_unknown_rule_set_or_world_is_empty_and_does_not_raise():
    compendium = compendium_view(content_library(), "does-not-exist", "nope")

    assert compendium.empty
    assert compendium.world is None
    assert compendium.rule_set is None
    assert compendium.sections == ()


def test_a_discovered_place_shows_its_summary_not_its_secret(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = factories.scene(
        client_factory, rule_set="rolltest", world_template="testhollow"
    )
    tools.create_location(name="Hidden Nook", description="A secret cave.", discovered=True)
    tools.discover_location("Hidden Nook", summary="A mossy nook behind the falls.")
    state = factories.open_campaign(app, campaign_id)

    compendium = compendium_view(content_library(), "rolltest", "testhollow", state)
    section = _sections(compendium)["places"]
    entry = next(entry for entry in section.entries if entry.name == "Hidden Nook")

    assert entry.summary == "A mossy nook behind the falls."
    assert entry.text == entry.summary
    assert entry.group == "Discovered Places"
    assert "A secret cave." not in entry.text
    assert compendium.place_links["Hidden Nook"] == entry.id

    # A discovered place with no summary still gets a readable entry, never the GM's description.
    tools.create_location(name="Bare Nook", description="Another secret cave.", discovered=True)
    state = factories.open_campaign(app, campaign_id)
    compendium = compendium_view(content_library(), "rolltest", "testhollow", state)
    bare = next(
        entry for entry in _sections(compendium)["places"].entries if entry.name == "Bare Nook"
    )
    assert bare.summary == ""
    assert bare.text == "A place you have discovered."
    assert "Another secret cave." not in bare.text


def test_the_standalone_page_falls_back_to_a_valid_world(
    client_factory: Callable[[], TestClient],
):
    client = factories.signed_in_client(client_factory)

    response = client.get("/compendium")
    assert response.status_code == 200
    assert "Compendium" in response.text

    # Twin Lakes is a Cairn world, so asking for it with Hogwarts must fall back to a world the
    # Hogwarts rule set offers rather than build a mismatched compendium.
    response = client.get("/compendium?rule_set=hogwarts-rpg&world_template=twin-lakes")
    assert response.status_code == 200
    selected = re.search(r'name="world_template".*?</select>', response.text, re.DOTALL)
    assert selected is not None
    chosen = re.search(r'<option value="([^"]+)"[^>]*selected', selected.group(0))
    assert chosen is not None
    library = content_library()
    world = library.world_templates[chosen.group(1)]
    assert world.rule_set is None or world.rule_set == "hogwarts-rpg"
