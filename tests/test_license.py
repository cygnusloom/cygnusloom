"""The license text is served from the installed package."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_license_text_is_served_from_the_installed_package(client: TestClient):
    response = client.get("/license.txt")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in response.text
    assert "END OF TERMS AND CONDITIONS" in response.text
    checked_in = Path(__file__).resolve().parents[1] / "LICENSE"
    assert response.text == checked_in.read_text(encoding="utf-8")
