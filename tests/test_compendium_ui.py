"""The compendium's own page and the campaign links that open it.

These assert markup only; the services tests cover what the compendium contains.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.factories import (
    campaign_ready,
    create_campaign,
    create_character,
    signed_in_client,
)


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
    assert 'data-filter="#compendium-sections"' in page
    assert "Open the standalone page" not in page


def test_a_campaign_has_a_compendium_view(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-view-target="compendium"' in page
    assert 'data-view="compendium"' in page
    assert 'id="compendium-sections"' in page


def test_a_character_ability_links_into_the_compendium(
    client_factory: Callable[[], TestClient],
):
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client)
    created = create_character(client, campaign_id, templates={"role": "test-role"})
    assert created.status_code == 303, created.text

    page = client.get(f"/campaigns/{campaign_id}").text
    sheet = page[page.index('id="panel-character"') : page.index('id="panel-inventory"')]
    assert "Steady Nerves" in sheet
    assert 'data-view-link="compendium"' in sheet
