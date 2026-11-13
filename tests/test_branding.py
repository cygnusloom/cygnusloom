"""The Cygnus Loom mark is shared by the web pages, the desktop window, and Authentik."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cygnusloom.web.app import STATIC_DIR

ROOT = Path(__file__).resolve().parent.parent
BRAND = STATIC_DIR / "brand"
BRAND_FILES = (
    "cygnus-loom.svg",
    "cygnus-loom-16.png",
    "cygnus-loom-32.png",
    "cygnus-loom-180.png",
    "cygnus-loom-256.png",
    "cygnus-loom-512.png",
    "cygnus-loom.ico",
    "cygnus-loom-lockup-light.svg",
    "cygnus-loom-lockup-dark.svg",
    "starry-night.svg",
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


def test_the_lockups_reuse_the_brand_mark():
    mark = (BRAND / "cygnus-loom.svg").read_text()
    constellation = "M47 9 29 28 24 41 18 55"
    assert constellation in mark
    for name in ("cygnus-loom-lockup-light.svg", "cygnus-loom-lockup-dark.svg"):
        lockup = (BRAND / name).read_text()
        assert constellation in lockup, name
        assert ">Cygnus Loom</text>" in lockup, name


def test_the_authentik_sign_in_page_is_branded():
    blueprint = (ROOT / "deploy" / "authentik" / "blueprints" / "cygnusloom.yaml").read_text()
    assert "slug: default-authentication-flow" in blueprint
    assert "title: Welcome to Cygnus Loom!" in blueprint
    assert "- model: authentik_brands.brand" in blueprint
    assert "domain: authentik-default" in blueprint
    assert "branding_title: Cygnus Loom" in blueprint
    assert (
        'branding_logo: !Format ["%s/static/brand/cygnus-loom-lockup-%%(theme)s.svg", '
        "!Env CYGNUSLOOM_BASE_URL]"
    ) in blueprint
    assert (
        'branding_default_flow_background: !Format ["%s/static/brand/starry-night.svg", '
        "!Env CYGNUSLOOM_BASE_URL]"
    ) in blueprint
    assert (
        'branding_favicon: !Format ["%s/static/brand/cygnus-loom-32.png", !Env CYGNUSLOOM_BASE_URL]'
    ) in blueprint
