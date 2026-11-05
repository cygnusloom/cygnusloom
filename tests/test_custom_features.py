"""Features invented during play, such as researched spells, potions, and enchantments.

They live on their character rather than in the loaded rule set, so the sheet, feature lookups,
and the compendium all see them without a content file changing.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.services.common import InvalidInput
from cygnusforge.services.compendium import compendium_view
from cygnusforge.services.world import WorldTools
from tests.conftest import content_library
from tests.factories import (
    DEFAULT_PC,
    choose_campaign_options,
    create_campaign,
    create_character,
    creation_fields,
    open_campaign,
    pinned_stats,
    rules_for,
    signed_in_client,
    tools_for,
)

RULE_SET = "academia-arcana"
WORLD = "unwritten-academy"
PC = DEFAULT_PC
FEATURE = "Whispering Ledger"
DESCRIPTION = "The page turns itself to whatever was last said aloud."
# The creation page's stat picker can show a negative assigned value, which the shared
# `creation_fields` parser does not read, so pin a legal roll of its own.
STATS = {
    stat.id: 1 for stat in rules_for(RULE_SET).character.stats.values() if stat.derived is None
}


def _academy(
    client_factory: Callable[[], TestClient],
) -> tuple[FastAPI, int, WorldTools, TestClient]:
    """An Academy Arcana campaign with a character, built through the app's own routes."""
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template=WORLD, rule_set=RULE_SET)
    choose_campaign_options(client, campaign_id, RULE_SET)
    with pinned_stats(STATS):
        created = create_character(client, campaign_id, fields=creation_fields(client, campaign_id))
    assert created.status_code == 303, created.text
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    return client.app, campaign_id, tools, client


def test_a_custom_feature_is_created_listed_looked_up_updated_and_removed(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools, _client = _academy(client_factory)

    created = tools.create_custom_feature(
        PC,
        FEATURE,
        "spell",
        DESCRIPTION,
        tag="Charms",
        rank=1,
        subject="Memory",
        base="A blank ledger",
        mods=["quiet", "patient"],
        stress=1,
    )
    assert created["id"] == "custom-whispering-ledger"
    assert created["custom"] is True
    assert created["name"] == FEATURE
    assert created["feature_type"] == "spell"
    assert created["mods"] == ["quiet", "patient"]
    assert created["stress"] == 1

    listed = tools.list_custom_features(PC)
    assert [feature["name"] for feature in listed] == [FEATURE]

    looked_up = tools.lookup_feature(FEATURE)
    assert looked_up["id"] == created["id"]
    assert looked_up["description"] == DESCRIPTION
    assert looked_up["tag"] == "Charms"
    assert looked_up["mods"] == ["quiet", "patient"]

    statblock = tools.get_statblock(PC)
    assert any(FEATURE in line for line in statblock["statblock"])

    updated = tools.update_custom_feature(
        PC,
        FEATURE,
        description="The ledger now writes back.",
        mods=["loud"],
        stress=2,
    )
    assert updated["name"] == FEATURE
    assert updated["description"] == "The ledger now writes back."
    assert updated["mods"] == ["loud"]
    assert updated["stress"] == 2

    looked_up = tools.lookup_feature(FEATURE)
    assert looked_up["description"] == "The ledger now writes back."
    assert looked_up["mods"] == ["loud"]
    assert looked_up["stress"] == 2

    removed = tools.remove_custom_feature(PC, FEATURE)
    assert removed == {"character": PC, "feature": FEATURE}
    assert tools.list_custom_features(PC) == []
    with pytest.raises(InvalidInput, match="no feature named"):
        tools.lookup_feature(FEATURE)

    character = open_campaign(app, campaign_id).characters[PC.casefold()]
    assert created["id"] not in character.custom_features
    assert created["id"] not in character.features


def test_custom_feature_names_are_unique_per_character(
    client_factory: Callable[[], TestClient],
):
    _app, _campaign_id, tools, _client = _academy(client_factory)
    tools.create_custom_feature(PC, FEATURE, "spell", DESCRIPTION)

    with pytest.raises(InvalidInput, match="already has a custom feature"):
        tools.create_custom_feature(PC, FEATURE.upper(), "potion", "A second try.")

    # Another character may know a feature with the same name; the check is per character.
    tools.create_location("The Quad", "A square of worn flagstones.")
    tools.create_npc("Bram", "The Quad", "A rival student.")
    other = tools.create_custom_feature("Bram", FEATURE, "potion", "Bram's own version.")
    assert other["name"] == FEATURE
    assert [feature["name"] for feature in tools.list_custom_features("Bram")] == [FEATURE]


def test_the_compendium_lists_an_invented_feature_under_abilities(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools, _client = _academy(client_factory)
    created = tools.create_custom_feature(PC, FEATURE, "spell", DESCRIPTION, tag="Charms", rank=1)
    state = open_campaign(app, campaign_id)

    compendium = compendium_view(content_library(), RULE_SET, WORLD, state)
    abilities = next(section for section in compendium.sections if section.id == "abilities")
    entry = next(entry for entry in abilities.entries if entry.name == FEATURE)

    assert entry.source == "campaign"
    assert entry.summary == DESCRIPTION
    assert "spell" in entry.tags
    assert compendium.ability_links[FEATURE] == entry.id
    assert created["id"] in compendium.ability_links


def test_the_character_replays_custom_feature_events(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools, _client = _academy(client_factory)
    created = tools.create_custom_feature(
        PC,
        FEATURE,
        "enchantment",
        "A first draft.",
        mods=["inked"],
        stress=1,
        cause="research",
    )

    character = open_campaign(app, campaign_id).characters[PC.casefold()]
    feature = character.custom_features[created["id"]]
    assert feature.name == FEATURE
    assert feature.feature_type == "enchantment"
    assert feature.description == "A first draft."
    assert feature.mods == ["inked"]
    assert feature.stress == 1
    assert feature.cause == "research"
    assert character.features[created["id"]] == 1

    tools.update_custom_feature(
        PC,
        FEATURE,
        description="A second draft.",
        mods=["gilded"],
        stress=2,
        rank=2,
    )
    character = open_campaign(app, campaign_id).characters[PC.casefold()]
    feature = character.custom_features[created["id"]]
    assert feature.description == "A second draft."
    assert feature.mods == ["gilded"]
    assert feature.stress == 2
    assert feature.rank == 2

    tools.remove_custom_feature(PC, FEATURE)
    character = open_campaign(app, campaign_id).characters[PC.casefold()]
    assert created["id"] not in character.custom_features
    assert created["id"] not in character.features
