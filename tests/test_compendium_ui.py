"""The compendium's own page and the campaign links that open it.

These assert markup only; the services tests cover what the compendium contains.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.factories import (
    campaign_ready_fast,
    create_campaign,
    create_character,
    signed_in_client,
)


def test_the_nav_links_to_about_before_and_after_signing_in(
    client_factory: Callable[[], TestClient],
):
    anonymous = client_factory().get("/login").text
    assert '<a href="/license">About</a>' in anonymous
    signed_in = signed_in_client(client_factory).get("/campaigns").text
    assert '<a href="/license">About</a>' in signed_in


def test_the_signed_in_nav_links_to_the_compendium(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    page = client.get("/campaigns").text
    assert '<a href="/compendium">Compendium</a>' in page


def test_the_standalone_page_offers_rule_set_and_world_pickers(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    response = client.get("/compendium")
    assert response.status_code == 200
    page = response.text
    assert "<h1>Compendium</h1>" in page
    assert 'action="/compendium"' in page
    assert 'name="rule_set"' in page
    assert 'name="world_template"' in page
    assert 'name="table_genres"' in page
    assert 'name="table_tones"' in page
    assert '<details class="compendium-creative-sources">' in page
    assert "<summary>Creative sources</summary>" in page
    assert '<details class="compendium-creative-sources" open' not in page
    assert "Creative sources" in page
    assert "Module tones" in page
    assert "data-compendium-search" in page
    assert 'role="tablist"' in page
    assert 'data-compendium-tab="world"' in page
    assert 'data-compendium-panel="world"' in page
    assert "compendium-group" in page
    assert "Open the standalone page" not in page


def test_the_standalone_page_notes_third_party_content(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    page = client.get("/compendium?rule_set=cairn2e&world_template=twin-lakes").text
    assert "Includes third-party material." in page
    assert '<a href="/license">content credits</a>' in page

    original = client.get(
        "/compendium?rule_set=academia-arcana&world_template=unwritten-academy"
    ).text
    assert "Includes third-party material." not in original


def test_story_module_cards_hide_the_gm_location_density(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    page = client.get("/compendium?rule_set=cairn2e&world_template=twin-lakes").text
    assert "The Hermit of Ash Hollow" in page
    assert "grim" in page
    assert "mystery" in page
    assert "low density" not in page


def test_creative_source_filters_narrow_tables_and_story_modules(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    page = client.get(
        "/compendium?rule_set=cairn2e&world_template=twin-lakes"
        "&table_genres=magicschool&table_tones=relaxing"
    ).text
    assert 'name="table_genres" value="magicschool" checked' in page
    assert 'name="table_tones" value="relaxing" checked' in page
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


def test_a_campaign_has_a_compendium_view(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-view-target="compendium"' in page
    assert 'data-view="compendium"' in page
    assert "data-compendium-search" in page
    assert 'data-compendium-tab="world"' in page


def test_the_standalone_page_shows_the_peoples_of_the_setting(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    page = client.get(
        "/compendium?rule_set=worldswithoutnumber&world_template=unwritten-world"
    ).text
    assert 'id="section-peoples"' in page
    assert '<article class="compendium-entry" id="compendium-peoples-dwarf"' in page
    assert "The deep folk of stone halls, craft, and long memory." in page
    assert "Dwarves are the deep folk, short and broad" in page

    # A rule set with no peoples shows no peoples chapter.
    cairn = client.get("/compendium?rule_set=cairn2e&world_template=twin-lakes").text
    assert 'id="section-peoples"' not in cairn


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
