from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.factories import campaign_ready_fast, tools_for


def test_the_campaign_page_shows_a_projects_panel(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    tools = tools_for(client.app, campaign_id)
    tools.start_project(
        "The Barrow Door",
        "research",
        "Learn how the barrow door opens.",
        target=4,
    )

    page = client.get(f"/campaigns/{campaign_id}").text

    assert 'id="tab-projects"' in page
    assert "The Barrow Door" in page
    assert "0 / 4" in page
