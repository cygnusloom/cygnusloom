"""The compendium: player-facing sourcebook content, and the secrets it must never carry."""

from __future__ import annotations

import re
from collections.abc import Callable

from fastapi.testclient import TestClient

from cygnusloom.services.compendium import (
    Compendium,
    CompendiumSection,
    compendium_view,
)
from cygnusloom.services.events import is_hidden, name_key
from cygnusloom.services.views import world_state_text
from tests import factories
from tests.conftest import content_library
from tests.test_world import NO_CAMPAIGN


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


def test_encounter_objects_and_maps_explain_themselves():
    compendium = compendium_view(content_library(), "cairn2e", "twin-lakes")
    terrain = _sections(compendium)["terrain"]
    chair = next(entry for entry in terrain.entries if entry.name == "Chair")
    assert chair.summary == "A plain chair for sitting."
    assert "A plain chair for sitting." in chair.text
    tavern = next(entry for entry in terrain.entries if entry.name == "Tavern")
    assert tavern.summary.startswith("A crowded taproom")
    assert "14 columns by 10 rows" in tavern.text


def test_authored_places_use_their_player_safe_summaries():
    compendium = compendium_view(content_library(), "cairn2e", "twin-lakes")
    place = next(
        entry for entry in _sections(compendium)["places"].entries if entry.name == "Isthmus Town"
    )
    assert place.text.startswith("A fading logging town")
    assert "fake tithing record" not in place.text


def test_world_regions_carry_the_terrain_lore():
    compendium = compendium_view(content_library(), "cairn2e", "twin-lakes")
    region = next(
        entry for entry in _sections(compendium)["places"].entries if entry.name == "West Lake"
    )
    assert "Open water" in region.text


def test_field_and_period_descriptions_reach_the_compendium():
    cairn = compendium_view(content_library(), "cairn2e", "twin-lakes")
    bond = next(entry for entry in _sections(cairn)["fields"].entries if entry.name == "Bond")
    assert bond.text.startswith("The person or promise they hold to.")
    academy = compendium_view(content_library(), "academia-arcana", "unwritten-academy")
    week = next(
        entry for entry in _sections(academy)["advancement"].entries if entry.name == "school week"
    )
    assert week.text.startswith("A week of lessons")


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


def test_a_discovered_place_can_be_created_with_its_player_text(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = factories.scene(
        client_factory, rule_set="rolltest", world_template="testhollow"
    )
    tools.create_location(
        name="Alchemy Lab",
        description="A locked cellar where the dean hides the illegal reagents.",
        discovered=True,
        summary="A bright workroom smelling of rosemary and hot glass.",
    )
    state = factories.open_campaign(app, campaign_id)
    compendium = compendium_view(content_library(), "rolltest", "testhollow", state)
    entry = next(
        entry for entry in _sections(compendium)["places"].entries if entry.name == "Alchemy Lab"
    )
    assert entry.summary == "A bright workroom smelling of rosemary and hot glass."
    assert entry.text == entry.summary
    assert "locked cellar" not in entry.text


def test_update_location_can_fill_in_a_missing_summary_without_touching_the_secret(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = factories.scene(
        client_factory, rule_set="rolltest", world_template="testhollow"
    )
    tools.create_location(
        name="Bare Nook",
        description="Another secret cave.",
        discovered=True,
    )
    tools.update_location(
        "Bare Nook", summary="A mossy shelf of rock, just tall enough to stand in."
    )
    state = factories.open_campaign(app, campaign_id)
    compendium = compendium_view(content_library(), "rolltest", "testhollow", state)
    entry = next(
        entry for entry in _sections(compendium)["places"].entries if entry.name == "Bare Nook"
    )
    assert entry.text == "A mossy shelf of rock, just tall enough to stand in."
    assert state.places["bare nook"].description == "Another secret cave."


def test_walking_into_an_undiscovered_place_keeps_its_player_text(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = factories.scene(
        client_factory, rule_set="rolltest", world_template="testhollow"
    )
    tools.create_location(
        name="Hidden Nook",
        description="A secret cave.",
        summary="A mossy nook behind the falls.",
    )
    tools.move_actor("Ada", "Hidden Nook")
    state = factories.open_campaign(app, campaign_id)
    assert state.places["hidden nook"].discovered is True
    compendium = compendium_view(content_library(), "rolltest", "testhollow", state)
    entry = next(
        entry for entry in _sections(compendium)["places"].entries if entry.name == "Hidden Nook"
    )
    assert entry.text == "A mossy nook behind the falls."


def test_the_gm_can_write_the_player_facing_world_overview(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = factories.scene(
        client_factory, rule_set="rolltest", world_template="unwritten-world"
    )
    tools.describe_world(
        "A drowned kingdom of salt towers, where every road is a canal and every bell rings "
        "underwater."
    )
    state = factories.open_campaign(app, campaign_id)
    compendium = compendium_view(content_library(), "rolltest", "unwritten-world", state)
    entry = next(entry for entry in _sections(compendium)["world"].entries if entry.name)
    assert entry.text == (
        "A drowned kingdom of salt towers, where every road is a canal and every bell rings "
        "underwater."
    )
    assert "Before the opening narration" not in entry.text


def test_an_unwritten_world_never_shows_its_setup_instructions(
    client_factory: Callable[[], TestClient],
):
    compendium = compendium_view(content_library(), "rolltest", "unwritten-world")
    entry = next(entry for entry in _sections(compendium)["world"].entries if entry.name)
    assert entry.text == (
        "The world is still taking shape. Its story will fill in as play goes on."
    )
    assert "Before the opening narration" not in entry.text


def test_world_and_place_text_events_are_hidden():
    assert is_hidden("world.described") is True
    assert is_hidden("location.updated") is True


def test_the_gm_is_reminded_to_write_missing_player_text(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = factories.scene(
        client_factory, rule_set="rolltest", world_template="unwritten-world"
    )
    tools.create_location(name="Bare Nook", description="A secret cave.", discovered=True)
    state = factories.open_campaign(app, campaign_id)
    text = world_state_text(state, NO_CAMPAIGN, unwritten_world=True)
    assert "Discovered places with no player-facing summary: Bare Nook" in text
    assert "Player-facing world overview: not written yet" in text


def test_the_standalone_page_falls_back_to_a_valid_world(
    client_factory: Callable[[], TestClient],
):
    client = factories.signed_in_client(client_factory)

    response = client.get("/compendium")
    assert response.status_code == 200
    assert "Compendium" in response.text

    # Twin Lakes is a Cairn world, so asking for it with Worlds Without Number must fall back to
    # a world that rule set offers rather than build a mismatched compendium.
    response = client.get("/compendium?rule_set=worldswithoutnumber&world_template=twin-lakes")
    assert response.status_code == 200
    selected = re.search(r'name="world_template".*?</select>', response.text, re.DOTALL)
    assert selected is not None
    chosen = re.search(r'<option value="([^"]+)"[^>]*selected', selected.group(0))
    assert chosen is not None
    library = content_library()
    world = library.world_templates[chosen.group(1)]
    assert world.rule_set is None or world.rule_set == "worldswithoutnumber"
