"""The compendium's creative-source filters and the campaign links that open it."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.factories import create_campaign, create_character, signed_in_client


def test_creative_source_filters_narrow_tables_and_story_modules(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    page = client.get(
        "/compendium?rule_set=cairn2e&world_template=twin-lakes"
        "&table_genres=magicschool&table_tones=relaxing"
    ).text
    assert 'id="compendium-dice-action"' in page
    assert 'id="compendium-dice-setting-premise"' not in page
    assert "The Hermit of Ash Hollow" not in page
    assert "No story modules fit these creative sources." in page

    page = client.get(
        "/compendium?rule_set=cairn2e&world_template=twin-lakes"
        "&table_genres=fantasy&table_tones=grim"
    ).text
    assert 'id="compendium-dice-setting-premise"' in page
    assert "The Hermit of Ash Hollow" in page


def test_the_character_panel_links_its_sheet_to_the_compendium(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client)
    created = create_character(client, campaign_id, templates={"role": "test-role"})
    assert created.status_code == 303, created.text

    page = client.get(f"/campaigns/{campaign_id}").text
    sheet = page[page.index('id="panel-character"') : page.index('id="panel-inventory"')]
    assert "Steady Nerves" in sheet
    assert (
        '<a href="#compendium-options-test-role" data-view-link="compendium">Test Role</a>' in sheet
    )
    assert (
        '<a href="#compendium-abilities-steady-nerves" data-view-link="compendium">'
        "Steady Nerves</a>" in sheet
    )
    assert '<a href="#compendium-advancement-focus" data-view-link="compendium">Focus</a>' in sheet
