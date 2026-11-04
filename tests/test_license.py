"""The AGPL notice must reach every user, signed in or not, and offer the running source."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cygnusforge import SOURCE_URL
from tests.conftest import register


def test_license_page_states_the_terms_and_offers_the_source(client: TestClient):
    page = client.get("/license")
    assert page.status_code == 200
    text = " ".join(page.text.split())
    assert "GNU Affero General Public License" in text
    assert "either version 3 of the License, or (at your option) any later version" in text
    assert "WITHOUT ANY WARRANTY" in text
    assert SOURCE_URL in text


def test_footer_shows_the_notice_before_and_after_signing_in(client: TestClient):
    anonymous = client.get("/login")
    assert 'href="/license"' in anonymous.text
    assert "Copyright (C) 2026 Cygnus Forge contributors" in anonymous.text
    assert SOURCE_URL in anonymous.text

    register(client)
    signed_in = client.get("/campaigns")
    assert 'href="/license"' in signed_in.text
    assert SOURCE_URL in signed_in.text
