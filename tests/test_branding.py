"""The Cygnus Loom mark is shared by the web pages, the desktop window, and Authentik."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cygnusloom.web.app import STATIC_DIR

BRAND = STATIC_DIR / "brand"
BRAND_FILES = (
    "cygnus-loom.svg",
    "cygnus-loom-16.png",
    "cygnus-loom-32.png",
    "cygnus-loom-180.png",
    "cygnus-loom-256.png",
    "cygnus-loom-512.png",
    "cygnus-loom.ico",
)


def test_every_page_declares_the_cygnus_loom_icon(client: TestClient):
    page = client.get("/license")
    assert page.status_code == 200
    assert (
        '<link rel="icon" href="/static/brand/cygnus-loom.svg" type="image/svg+xml">' in page.text
    )
    assert (
        '<link rel="icon" href="/static/brand/cygnus-loom-32.png" type="image/png" sizes="32x32">'
        in page.text
    )
    assert '<link rel="apple-touch-icon" href="/static/brand/cygnus-loom-180.png">' in page.text
    assert '<meta name="theme-color" content="#15171c">' in page.text


def test_the_top_bar_shows_the_mark_beside_the_wordmark(client: TestClient):
    page = client.get("/license")
    assert '<a class="brand" href="/">' in page.text
    assert '<img src="/static/brand/cygnus-loom.svg" alt="" width="26" height="26">' in page.text
    assert "<span>Cygnus Loom</span>" in page.text


def test_the_brand_assets_are_checked_in():
    for name in BRAND_FILES:
        path = BRAND / name
        assert path.is_file(), name
        assert path.stat().st_size > 0, name


def test_the_brand_assets_are_served(client: TestClient):
    svg = client.get("/static/brand/cygnus-loom.svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert "<title>Cygnus Loom</title>" in svg.text
    assert "#f5c542" in svg.text

    for name in BRAND_FILES[1:]:
        response = client.get(f"/static/brand/{name}")
        assert response.status_code == 200, name
        assert response.content, name
