"""The AGPL notice must reach every user, signed in or not, and offer the running source."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cygnusforge import SOURCE_URL, content_credits
from tests.conftest import register


def test_license_page_states_the_terms_and_offers_the_source(client: TestClient):
    page = client.get("/license")
    assert page.status_code == 200
    text = " ".join(page.text.split())
    assert "GNU Affero General Public License" in text
    assert "either version 3 of the License, or (at your option) any later version" in text
    assert "WITHOUT ANY WARRANTY" in text
    assert SOURCE_URL.startswith("https://")
    assert f'href="{SOURCE_URL}"' in text
    assert 'href="/license.txt"' in text


def test_license_text_is_served_from_the_installed_package(client: TestClient):
    response = client.get("/license.txt")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in response.text
    assert "END OF TERMS AND CONDITIONS" in response.text
    checked_in = Path(__file__).resolve().parents[1] / "LICENSE"
    assert response.text == checked_in.read_text(encoding="utf-8")


def test_license_page_serves_the_content_credits(client: TestClient):
    checked_in = Path(__file__).resolve().parents[1] / "CREDITS.md"
    assert content_credits() == checked_in.read_text(encoding="utf-8")

    page = client.get("/license")
    assert page.status_code == 200
    text = " ".join(page.text.split())
    assert "Content credits" in text
    assert "Cairn 2e by Yochai Gal" in text
    assert "The Twin Lakes world uses content from the CAS-1 module" in text
    assert "https://creativecommons.org/licenses/by-sa/4.0/" in page.text
    assert "#content-credits" not in page.text


def test_footer_shows_the_notice_before_and_after_signing_in(client: TestClient):
    anonymous = client.get("/login")
    assert 'href="/license"' in anonymous.text
    assert "Copyright (C) 2026 Cygnus Forge contributors" in anonymous.text
    assert f'href="{SOURCE_URL}"' in anonymous.text

    register(client)
    signed_in = client.get("/campaigns")
    assert 'href="/license"' in signed_in.text
    assert f'href="{SOURCE_URL}"' in signed_in.text
