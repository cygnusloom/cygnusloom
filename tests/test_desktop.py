from __future__ import annotations

import http.server
import json
import logging
import os
import re
import ssl
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom import desktop
from cygnusloom.config import Settings
from cygnusloom.db.models import DesktopLogin
from cygnusloom.db.session import make_engine
from cygnusloom.desktop_servers import (
    ServerError,
    ServerStore,
    normalize_invite_url,
    normalize_server_url,
)
from cygnusloom.web import desktop as desktop_web
from tests.conftest import SECRET, csrf_token, make_app
from tests.fake_oidc import FakeOIDC, claims

BASE_SETTINGS = {
    "secret_key": SECRET,
    "llm_models": ("fake/scripted",),
    "llm_default_model": "fake/scripted",
    "base_url": "http://127.0.0.1:8000",
}


def desktop_settings(tmp_path, **overrides) -> Settings:
    values = {
        **BASE_SETTINGS,
        "database_url": f"sqlite:///{tmp_path / 'desktop.db'}",
        "single_player": True,
        "desktop": True,
        "data_dir": str(tmp_path),
        **overrides,
    }
    return Settings(**values)


def test_normalize_server_url_adds_https_and_drops_the_path():
    assert normalize_server_url("table.example.com") == "https://table.example.com"
    assert (
        normalize_server_url("https://table.example.com/join/abc?x=1#top")
        == "https://table.example.com"
    )
    assert normalize_server_url("https://table.example.com:8443/") == (
        "https://table.example.com:8443"
    )


def test_normalize_server_url_allows_plain_http_only_on_this_machine():
    assert normalize_server_url("http://127.0.0.1:8899") == "http://127.0.0.1:8899"
    assert normalize_server_url("http://localhost:8899") == "http://localhost:8899"
    assert normalize_server_url("http://[::1]:8899") == "http://[::1]:8899"
    with pytest.raises(ServerError, match="https"):
        normalize_server_url("http://table.example.com")


def test_normalize_server_url_drops_a_default_port():
    assert normalize_server_url("https://table.example.com:443") == "https://table.example.com"
    assert normalize_server_url("http://127.0.0.1:80") == "http://127.0.0.1"


def test_normalize_server_url_rejects_credentials_and_other_schemes():
    with pytest.raises(ServerError, match="username or password"):
        normalize_server_url("https://ada:secret@table.example.com")
    with pytest.raises(ServerError, match="https"):
        normalize_server_url("ftp://table.example.com")
    with pytest.raises(ServerError, match="server address"):
        normalize_server_url("   ")


def test_invite_urls_must_be_authentik_flows():
    link = "https://idp.example.com/if/flow/cygnusloom-enrollment-invite/?itoken=abc"
    assert normalize_invite_url(link) == link
    with pytest.raises(ServerError, match="Authentik"):
        normalize_invite_url("https://table.example.com/join/abc")
    with pytest.raises(ServerError, match="https"):
        normalize_invite_url("http://idp.example.com/if/flow/x")


def test_the_server_store_adds_renames_removes_and_deduplicates(tmp_path):
    store = ServerStore(tmp_path / "servers.json")

    first = store.add("https://table.example.com", "Tuesday table", "https://idp.example.com")
    again = store.add("https://table.example.com/", "Ignored")
    second = store.add("http://127.0.0.1:9000", "", "")

    assert again.id == first.id
    assert [server.id for server in store.list()] == [second.id, first.id]
    assert store.get(first.id).oidc_issuer == "https://idp.example.com"

    store.set_issuer(first.id, "https://idp.example.com/")
    assert store.get(first.id).oidc_issuer == "https://idp.example.com"

    renamed = store.rename(first.id, "Wednesday table")
    assert renamed.name == "Wednesday table"

    store.touch(second.id)
    assert store.list()[0].id == second.id

    store.remove(first.id)
    assert [server.id for server in store.list()] == [second.id]
    with pytest.raises(ServerError, match="not in the list"):
        store.get(first.id)


def test_a_corrupt_server_list_is_set_aside(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text("{not json", encoding="utf-8")
    store = ServerStore(path)

    assert store.list() == []
    assert (tmp_path / "servers.json.corrupt").is_file()


def test_the_server_list_keeps_its_permissions_private(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX file modes")
    store = ServerStore(tmp_path / "servers.json")
    store.add("https://table.example.com")

    assert store.path.stat().st_mode & 0o777 == 0o600


def test_the_desktop_helpers_build_the_browser_sign_in_url():
    url = desktop.browser_login_url(
        "https://table.example.com/", "http://127.0.0.1:5000/callback", "state-1"
    )

    parsed = urlsplit(url)
    assert parsed.path == "/desktop/login"
    assert parse_qs(parsed.query) == {
        "redirect_uri": ["http://127.0.0.1:5000/callback"],
        "state": ["state-1"],
        "next": ["/campaigns"],
    }


def test_the_return_script_fills_both_prepared_slots():
    script = desktop.return_button_script("http://127.0.0.1:8123")

    assert "desktop-slot" in script
    assert "desktop-slot-sidebar" in script
    assert 'const local = "http://127.0.0.1:8123"' in script
    assert "local + '/campaigns?desktop_home=1'" in script
    assert "Return to my device" in script
    assert "action: 'home'" in script
    assert "action: 'external'" in script
    assert "a[href]" in script
    assert "stopPropagation" in script
    assert "__cygnusReturnInstalled" in script
    assert "__cygnusExternalGuard" in script
    assert "DOMContentLoaded" in script
    assert "attempt < 20" in script


def test_the_device_bar_is_needed_outside_the_local_controls():
    assert desktop.needs_device_bar("https://idp.example.com", "http://127.0.0.1:8123", False)
    assert not desktop.needs_device_bar("https://table.example.com", "http://127.0.0.1:8123", True)
    assert not desktop.needs_device_bar("http://127.0.0.1:8123", "http://127.0.0.1:8123", False)
    assert desktop.needs_device_bar("", "http://127.0.0.1:8123", False)


def test_the_loading_message_names_the_destination():
    local = "http://127.0.0.1:8123"

    assert desktop.loading_message(f"{local}/campaigns", local) == "Loading Cygnus Loom…"
    assert (
        desktop.loading_message("https://table.example.com/login", local)
        == "Loading table.example.com…"
    )
    assert (
        desktop.loading_message("https://table.example.com/login", local, "Tuesday table")
        == "Loading Tuesday table…"
    )
    assert desktop.loading_message("about:blank", local) == "Loading…"


def test_page_console_levels_map_to_logging_levels():
    assert desktop.js_message_level(0) == logging.INFO
    assert desktop.js_message_level(1) == logging.WARNING
    assert desktop.js_message_level(2) == logging.ERROR
    assert desktop.js_message_level(9) == logging.INFO
    assert desktop.js_message_level("unexpected") == logging.INFO

    class Level:
        value = 2

    assert desktop.js_message_level(Level()) == logging.ERROR


def test_a_stopped_renderer_is_named_and_recovered():
    assert desktop.renderer_termination_name(0) == "normal"
    assert desktop.renderer_termination_name(1) == "abnormal"
    assert desktop.renderer_termination_name(2) == "crashed"
    assert desktop.renderer_termination_name(3) == "killed"
    assert desktop.renderer_termination_name("unexpected") == "unexpected"

    class Status:
        value = 2

    assert desktop.renderer_termination_name(Status()) == "crashed"
    assert not desktop.renderer_needs_recovery(0)
    assert desktop.renderer_needs_recovery(1)
    assert desktop.renderer_needs_recovery(2)
    assert desktop.renderer_needs_recovery(3)


def test_renderer_recovery_retries_briefly_then_leaves_the_choice_to_the_player():
    reload, crashes = desktop.plan_renderer_recovery((), 100.0)
    assert reload
    assert crashes == (100.0,)

    reload, crashes = desktop.plan_renderer_recovery(crashes, 110.0)
    assert reload
    assert crashes == (100.0, 110.0)

    # A crash loop must not restart the page forever.
    reload, crashes = desktop.plan_renderer_recovery(crashes, 120.0)
    assert not reload
    assert crashes == (100.0, 110.0, 120.0)

    # A crash long after the last one starts a fresh retry budget.
    reload, crashes = desktop.plan_renderer_recovery(crashes, 300.0)
    assert reload
    assert crashes == (300.0,)


def test_the_desktop_sentinel_is_limited_to_the_local_app():
    script = desktop.desktop_sentinel_script("http://127.0.0.1:8123")

    assert 'location.origin === "http://127.0.0.1:8123"' in script
    assert "__cygnusDesktop" in script


def test_the_desktop_bridge_can_answer_a_late_handshake():
    script = desktop.desktop_bridge_ready_script()

    assert "window.__cygnusDesktop = true" in script
    assert "cygnus-desktop-ready" in script


def test_clicked_links_stay_in_the_app_during_an_invite():
    assert not desktop.should_hand_to_browser(
        "https", "https://table.example.com", set(), invite_pending=True
    )
    assert desktop.should_hand_to_browser(
        "https", "https://table.example.com", set(), invite_pending=False
    )
    assert not desktop.should_hand_to_browser(
        "https",
        "https://table.example.com",
        {"https://table.example.com"},
        invite_pending=False,
    )
    assert desktop.should_hand_to_browser(
        "mailto", "mailto:ada@example.com", set(), invite_pending=True
    )


def test_add_actions_are_parsed_and_validated():
    assert desktop.parse_add_action(
        {"action": "add", "url": "https://table.example.com", "name": "Table"}
    ) == ("https://table.example.com", "Table")
    assert desktop.parse_add_action({"action": "add", "url": "https://table.example.com"}) == (
        "https://table.example.com",
        "",
    )
    assert desktop.parse_add_action({"action": "add"}) is None
    assert desktop.parse_add_action({"action": "add", "url": 7}) is None
    assert (
        desktop.parse_add_action({"action": "connect", "url": "https://table.example.com"}) is None
    )


def test_desktop_actions_are_parsed_and_validated():
    assert desktop.parse_desktop_action({"action": "connect", "server": "abc"}) == (
        "connect",
        "abc",
    )
    assert desktop.parse_desktop_action({"action": "login", "server": "abc"}) == (
        "login",
        "abc",
    )
    assert desktop.parse_desktop_action({"action": "home"}) == ("home", "")
    assert desktop.parse_desktop_action({"action": "browser", "server": "abc"}) == (
        "browser",
        "abc",
    )
    assert desktop.parse_desktop_action(
        {"action": "invite", "url": "https://idp.example.com/if/flow/x"}
    ) == ("invite", "https://idp.example.com/if/flow/x")
    assert desktop.parse_desktop_action(
        {"action": "external", "url": "https://example.com/page"}
    ) == ("external", "https://example.com/page")
    assert desktop.parse_desktop_action(
        {"action": "external", "url": "mailto:ada@example.com"}
    ) == ("external", "mailto:ada@example.com")
    assert (
        desktop.parse_desktop_action({"action": "external", "url": "javascript:alert(1)"}) is None
    )
    assert desktop.parse_desktop_action({"action": "connect"}) is None
    assert desktop.parse_desktop_action({"action": "unknown", "server": "abc"}) is None
    assert desktop.parse_desktop_action("not a dict") is None


def test_the_loopback_receiver_checks_the_state_and_returns_the_code():
    received: list[tuple[str, str]] = []
    done = threading.Event()

    def result(code: str, error: str) -> None:
        received.append((code, error))
        done.set()

    receiver = desktop.LoopbackReceiver("state-0123456789abcdef", result)
    callback = receiver.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{callback}?state=state-0123456789abcdef&code=abc") as reply:
            assert reply.status == 200
            assert b"Signed in" in reply.read()
        assert done.wait(2)
    finally:
        receiver.stop()

    assert received == [("abc", "")]


def test_the_loopback_receiver_rejects_a_wrong_state():
    received: list[tuple[str, str]] = []
    receiver = desktop.LoopbackReceiver(
        "state-0123456789abcdef", lambda code, error: received.append((code, error))
    )
    callback = receiver.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(f"{callback}?state=wrong&code=abc")
        assert error.value.code == 400
    finally:
        receiver.stop()

    assert received == []


def test_desktop_mode_adds_the_server_list_to_the_main_menu(tmp_path, monkeypatch):
    monkeypatch.setattr(
        desktop_web,
        "probe_server",
        lambda url: desktop_web.ServerInfo(version="4.2.0", oidc_issuer="https://idp.example.com"),
    )
    settings = desktop_settings(tmp_path)
    app = make_app(settings)
    client = TestClient(app)

    page = client.get("/campaigns")
    assert "Servers" in page.text
    assert "Add by invitation link" in page.text
    assert "data-desktop-add" in page.text
    assert "Play on my device" in page.text
    assert "Play on a server" in page.text
    assert page.text.index("Play on my device") < page.text.index("Play on a server")
    assert 'data-play-initial="device"' in page.text
    assert re.search(r'id="tab-device"[^>]*aria-selected="true"', page.text)
    assert re.search(r'id="tab-server"[^>]*aria-selected="false"', page.text)
    assert re.search(r'id="panel-server"[^>]*hidden', page.text)
    assert not re.search(r'id="panel-device"[^>]*hidden', page.text)
    assert "data-play-tab" in page.text
    assert "data-play-panel" in page.text
    assert "/static/play_tabs.js" in page.text
    assert "/static/desktop.js" in page.text

    script = client.get("/static/desktop.js")
    assert script.status_code == 200
    assert 'post("hello")' in script.text
    assert "cygnus-desktop-ready" in script.text
    assert 'post("external"' in script.text
    assert "__cygnusExternalGuard" in script.text
    assert 'addEventListener("click", openExternal, true)' in script.text

    # Local pages outside the campaign menu still need the bridge and link guard.
    license_page = client.get("/license")
    assert "/static/desktop.js" in license_page.text

    token = csrf_token(client, "/campaigns")
    response = client.post(
        "/desktop/servers",
        data={
            "csrf_token": token,
            "url": "https://table.example.com",
            "name": "Tuesday table",
        },
    )

    assert response.status_code == 200
    assert "Tuesday table" in response.text
    assert "table.example.com" in response.text
    assert "Added Tuesday table." in response.text
    assert "Last connected" in response.text
    server = ServerStore(tmp_path / "servers.json").list()[0]
    assert server.name == "Tuesday table"
    assert server.oidc_issuer == "https://idp.example.com"

    # The same address is not added twice.
    response = client.post(
        "/desktop/servers",
        data={"csrf_token": token, "url": "https://table.example.com"},
    )
    assert "already in the server list" in response.text
    assert len(ServerStore(tmp_path / "servers.json").list()) == 1


def test_the_desktop_menu_remembers_the_last_play_tab(tmp_path):
    settings = desktop_settings(tmp_path)
    app = make_app(settings)
    client = TestClient(app)

    client.cookies.set("cygnus_play_tab", "server")
    page = client.get("/campaigns")
    assert 'data-play-initial="server"' in page.text
    assert re.search(r'id="tab-server"[^>]*aria-selected="true"', page.text)
    assert re.search(r'id="tab-device"[^>]*aria-selected="false"', page.text)
    assert re.search(r'id="panel-device"[^>]*hidden', page.text)
    assert not re.search(r'id="panel-server"[^>]*hidden', page.text)

    # A stale or unknown preference is not trusted.
    client.cookies.set("cygnus_play_tab", "nonsense")
    page = client.get("/campaigns")
    assert 'data-play-initial="device"' in page.text
    assert re.search(r'id="panel-server"[^>]*hidden', page.text)


def test_the_desktop_menu_lists_servers_by_last_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(
        desktop_web, "probe_server", lambda url: desktop_web.ServerInfo(version="4.2.0")
    )
    settings = desktop_settings(tmp_path)
    app = make_app(settings)
    client = TestClient(app)
    token = csrf_token(client, "/campaigns")

    for url, name in (
        ("https://first.example.com", "First table"),
        ("https://second.example.com", "Second table"),
    ):
        response = client.post(
            "/desktop/servers", data={"csrf_token": token, "url": url, "name": name}
        )
        assert response.status_code == 200

    store = ServerStore(tmp_path / "servers.json")
    first = store.find_by_url("https://first.example.com")
    assert first is not None
    store.touch(first.id)

    page = client.get("/campaigns")
    assert page.text.index("First table") < page.text.index("Second table")
    assert page.text.count("Last connected") == 2


def test_the_desktop_menu_without_desktop_mode_has_no_server_list(client: TestClient):
    client.cookies.set("cygnus_play_tab", "server")
    page = client.get("/campaigns")

    assert "Add by invitation link" not in page.text
    assert "Play on my device" not in page.text
    assert "Play on a server" not in page.text
    assert "/static/play_tabs.js" not in page.text
    assert "/static/desktop.js" not in page.text
    assert "/static/desktop.js" not in client.get("/license").text


def test_the_desktop_setup_wizard_lives_in_the_device_tab(tmp_path):
    settings = desktop_settings(tmp_path, llm_models=(), llm_default_model="")
    client = TestClient(make_app(settings))

    response = client.get("/campaigns", follow_redirects=False)

    assert response.status_code == 200
    assert "Give your game master a mind" in response.text
    assert "Play on my device" in response.text
    assert "Play on a server" in response.text
    device_panel = response.text.split('id="panel-device"', 1)[1].split('id="panel-server"', 1)[0]
    assert "Give your game master a mind" in device_panel
    assert 'id="setup-form"' in device_panel
    server_panel = response.text.split('id="panel-server"', 1)[1]
    assert "Give your game master a mind" not in server_panel
    assert "Add by invitation link" in server_panel
    # Creating a campaign has to wait for the model.
    assert 'href="/campaigns/new"' not in response.text


def test_the_desktop_setup_route_opens_the_device_tab(tmp_path):
    settings = desktop_settings(tmp_path, llm_models=(), llm_default_model="")
    client = TestClient(make_app(settings))

    response = client.get("/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns#device"

    forced = client.get("/setup?force=1", follow_redirects=False)
    assert forced.status_code == 303
    assert forced.headers["location"] == "/campaigns?setup=1#device"


def test_a_desktop_new_campaign_returns_to_the_device_tab(tmp_path):
    settings = desktop_settings(tmp_path, llm_models=(), llm_default_model="")
    client = TestClient(make_app(settings))

    response = client.get("/campaigns/new", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns#device"


def test_the_desktop_setup_wizard_saves_and_disappears(tmp_path):
    settings = desktop_settings(tmp_path, llm_models=(), llm_default_model="")
    client = TestClient(make_app(settings))
    token = csrf_token(client, "/campaigns")

    response = client.post(
        "/setup",
        data={"csrf_token": token, "preset": "demo"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns/new"
    page = client.get("/campaigns")
    assert "Give your game master a mind" not in page.text
    assert "Play on a server" in page.text
    assert 'href="/campaigns/new"' in page.text


def test_a_desktop_setup_error_stays_on_the_campaigns_page(tmp_path):
    settings = desktop_settings(tmp_path, llm_models=(), llm_default_model="")
    client = TestClient(make_app(settings))
    token = csrf_token(client, "/campaigns")

    response = client.post(
        "/setup",
        data={"csrf_token": token, "preset": "custom", "model": "my-model", "base_url": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns?setup=1#device"
    page = client.get("/campaigns?setup=1")
    assert "base url is required" in page.text.lower()
    assert "Give your game master a mind" in page.text
    assert "Play on a server" in page.text
    assert "Play on my device" in page.text
    assert 'value="my-model"' in page.text
    assert 'data-prefilled="1"' in page.text
    assert re.search(r'value="custom"[^>]*checked', page.text)


def test_the_desktop_wizard_can_be_forced_after_setup(tmp_path):
    settings = desktop_settings(tmp_path)
    client = TestClient(make_app(settings))

    page = client.get("/campaigns")
    assert "Give your game master a mind" not in page.text

    forced = client.get("/campaigns?setup=1")
    assert "Give your game master a mind" in forced.text
    assert "Already configured" in forced.text
    assert "fake/scripted" in forced.text


def test_adding_a_server_rejects_a_non_cygnus_address(tmp_path, monkeypatch):
    def fail(url: str):
        raise desktop_web.ServerCheckError(f"{url} is not a Cygnus Loom server.")

    monkeypatch.setattr(desktop_web, "probe_server", fail)
    settings = desktop_settings(tmp_path)
    app = make_app(settings)
    client = TestClient(app)

    token = csrf_token(client, "/campaigns")
    response = client.post(
        "/desktop/servers",
        data={"csrf_token": token, "url": "https://example.com"},
    )

    assert "is not a Cygnus Loom server" in response.text
    assert not (tmp_path / "servers.json").exists()


def test_adding_a_remote_http_server_is_rejected_before_it_is_probed(tmp_path):
    settings = desktop_settings(tmp_path)
    app = make_app(settings)
    client = TestClient(app)

    token = csrf_token(client, "/campaigns")
    response = client.post(
        "/desktop/servers",
        data={"csrf_token": token, "url": "http://table.example.com"},
    )

    assert "Use https://" in response.text
    assert not (tmp_path / "servers.json").exists()


class InfoHandler(http.server.BaseHTTPRequestHandler):
    """A minimal Cygnus Loom /desktop/info endpoint for probe tests."""

    def do_GET(self) -> None:
        body = json.dumps(
            {
                "app": "cygnusloom",
                "version": "4.3.1",
                "oidc_issuer": "https://idp.example.com",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def test_probe_server_uses_plain_http_without_an_ssl_context(monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), InfoHandler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setattr(
        desktop_web.httpx,
        "stream",
        lambda *args, **kwargs: pytest.fail("HTTP probing must not use httpx"),
    )
    try:
        info = desktop_web.probe_server(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert info.version == "4.3.1"
    assert info.oidc_issuer == "https://idp.example.com"


def test_probe_server_reports_an_ssl_configuration_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise ssl.SSLError("unknown error")

    monkeypatch.setattr(desktop_web.httpx, "stream", fail)

    with pytest.raises(desktop_web.ServerCheckError) as error:
        desktop_web.probe_server("https://table.example.com")

    assert "secure connection" in str(error.value)


def test_an_invitation_link_opens_the_desktop_invite_page(tmp_path):
    settings = desktop_settings(tmp_path)
    client = TestClient(make_app(settings))
    token = csrf_token(client, "/campaigns")
    link = "https://idp.example.com/if/flow/cygnusloom-enrollment-invite/?itoken=abc"

    response = client.post("/desktop/servers/invite", data={"csrf_token": token, "url": link})

    assert response.status_code == 200
    assert "Open invitation" in response.text
    assert f'data-desktop-invite="{link.replace("&", "&amp;")}"' in response.text
    assert "/static/desktop.js" in response.text


def test_a_non_authentik_link_is_not_opened_as_an_invitation(tmp_path):
    settings = desktop_settings(tmp_path)
    client = TestClient(make_app(settings))
    token = csrf_token(client, "/campaigns")

    response = client.post(
        "/desktop/servers/invite",
        data={"csrf_token": token, "url": "https://table.example.com/join/abc"},
    )

    assert "not an Authentik invitation link" in response.text


def test_desktop_info_identifies_a_server_without_exposing_a_secret(tmp_path):
    settings = desktop_settings(tmp_path)
    client = TestClient(make_app(settings))

    payload = client.get("/desktop/info").json()

    assert payload["app"] == "cygnusloom"
    assert payload["version"]
    assert payload["desktop_login"] is False
    assert "secret" not in payload


def _signed_in_client(settings: Settings, oidc: FakeOIDC) -> TestClient:
    client = TestClient(make_app(settings, oidc_provider=oidc))
    client.get("/auth/callback")
    return client


def test_browser_sign_in_hands_a_one_time_code_to_the_app(tmp_path):
    settings = Settings(
        **BASE_SETTINGS,
        database_url=f"sqlite:///{tmp_path / 'handoff.db'}",
        auto_create_schema=True,
    )
    browser = _signed_in_client(settings, FakeOIDC(identity=claims(display_name="Ada")))
    redirect_uri = "http://127.0.0.1:43210/callback"
    state = "state-0123456789abcdef"

    response = browser.get(
        "/desktop/login",
        params={"redirect_uri": redirect_uri, "state": state, "next": "/account"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/desktop/approve?flow=")
    approve_path = response.headers["location"]

    page = browser.get(approve_path)
    assert "Allow the desktop app to sign in?" in page.text
    assert "Ada" in page.text
    assert "http://127.0.0.1:8000" in page.text
    token = csrf_token(browser, approve_path)
    response = browser.post(
        approve_path,
        data={"csrf_token": token, "allow": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    callback = urlsplit(response.headers["location"])
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == redirect_uri
    query = parse_qs(callback.query)
    assert query["state"] == [state]
    code = query["code"][0]

    app_client = TestClient(make_app(settings, oidc_provider=FakeOIDC(identity=claims())))
    response = app_client.get("/desktop/session", params={"code": code}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    assert app_client.get("/account").status_code == 200

    # The code is single use.
    other = TestClient(make_app(settings, oidc_provider=FakeOIDC(identity=claims())))
    assert other.get("/desktop/session", params={"code": code}).status_code == 404


def test_browser_sign_in_can_be_denied(tmp_path):
    settings = Settings(
        **BASE_SETTINGS,
        database_url=f"sqlite:///{tmp_path / 'deny.db'}",
        auto_create_schema=True,
    )
    browser = _signed_in_client(settings, FakeOIDC(identity=claims()))
    response = browser.get(
        "/desktop/login",
        params={
            "redirect_uri": "http://127.0.0.1:43210/callback",
            "state": "state-0123456789abcdef",
        },
        follow_redirects=False,
    )
    approve_path = response.headers["location"]
    browser.get(approve_path)
    token = csrf_token(browser, approve_path)

    response = browser.post(
        approve_path,
        data={"csrf_token": token, "deny": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["error"] == ["denied"]
    assert "code" not in query


def test_browser_sign_in_requires_a_loopback_callback(tmp_path):
    settings = Settings(
        **BASE_SETTINGS,
        database_url=f"sqlite:///{tmp_path / 'redirect.db'}",
        auto_create_schema=True,
    )
    browser = _signed_in_client(settings, FakeOIDC(identity=claims()))

    for redirect_uri in (
        "https://evil.example/callback",
        "http://127.0.0.1:43210/other",
        "http://127.0.0.1:43210/callback?extra=1",
        "http://localhost:43210/callback",
        "http://127.0.0.1/callback",
    ):
        response = browser.get(
            "/desktop/login",
            params={
                "redirect_uri": redirect_uri,
                "state": "state-0123456789abcdef",
            },
        )
        assert response.status_code == 400


def test_browser_sign_in_sends_a_signed_out_player_through_the_provider(tmp_path):
    settings = Settings(
        **BASE_SETTINGS,
        database_url=f"sqlite:///{tmp_path / 'login.db'}",
        auto_create_schema=True,
    )
    browser = TestClient(make_app(settings, oidc_provider=FakeOIDC(identity=claims())))

    response = browser.get(
        "/desktop/login",
        params={
            "redirect_uri": "http://127.0.0.1:43210/callback",
            "state": "state-0123456789abcdef",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login?next=/desktop/approve")


def test_an_expired_browser_sign_in_is_refused(tmp_path):
    settings = Settings(
        **BASE_SETTINGS,
        database_url=f"sqlite:///{tmp_path / 'expired.db'}",
        auto_create_schema=True,
    )
    browser = _signed_in_client(settings, FakeOIDC(identity=claims()))
    response = browser.get(
        "/desktop/login",
        params={
            "redirect_uri": "http://127.0.0.1:43210/callback",
            "state": "state-0123456789abcdef",
        },
        follow_redirects=False,
    )
    approve_path = response.headers["location"]
    flow = parse_qs(urlsplit(approve_path).query)["flow"][0]
    with Session(make_engine(settings.database_url)) as db:
        row = db.exec(select(DesktopLogin).where(DesktopLogin.flow == flow)).one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.add(row)
        db.commit()

    page = browser.get(approve_path)

    assert page.status_code == 404
    assert "expired" in page.text


def test_browser_sign_in_is_rate_limited(tmp_path):
    settings = Settings(
        **BASE_SETTINGS,
        database_url=f"sqlite:///{tmp_path / 'limit.db'}",
        auto_create_schema=True,
    )
    browser = _signed_in_client(settings, FakeOIDC(identity=claims()))
    params = {
        "redirect_uri": "http://127.0.0.1:43210/callback",
        "state": "state-0123456789abcdef",
    }

    for _ in range(10):
        assert browser.get("/desktop/login", params=params).status_code == 200

    assert browser.get("/desktop/login", params=params).status_code == 429


def test_browser_sign_in_is_unavailable_in_single_player(tmp_path):
    settings = desktop_settings(tmp_path)
    client = TestClient(make_app(settings))

    response = client.get(
        "/desktop/login",
        params={
            "redirect_uri": "http://127.0.0.1:43210/callback",
            "state": "state-0123456789abcdef",
        },
    )

    assert response.status_code == 404


def test_the_desktop_environment_marks_the_window_and_headless_modes(tmp_path):
    from cygnusloom import launcher

    window = launcher._environment(tmp_path, "http://127.0.0.1:8000", "INFO", headless=False)
    headless = launcher._environment(tmp_path, "http://127.0.0.1:8000", "INFO", headless=True)

    assert window["CYGNUSLOOM_DESKTOP"] == "1"
    assert window["CYGNUSLOOM_SINGLE_PLAYER"] == "1"
    assert headless["CYGNUSLOOM_DESKTOP"] == "0"
    assert window["CYGNUSLOOM_BASE_URL"] == "http://127.0.0.1:8000"


def test_desktop_settings_require_single_player():
    from cygnusloom.config import load_settings

    with pytest.raises(RuntimeError, match="CYGNUSLOOM_DESKTOP"):
        load_settings(
            {
                "CYGNUSLOOM_SECRET_KEY": SECRET,
                "CYGNUSLOOM_DESKTOP": "1",
                "CYGNUSLOOM_BASE_URL": "http://127.0.0.1:8000",
            }
        )
