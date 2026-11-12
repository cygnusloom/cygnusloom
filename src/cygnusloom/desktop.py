"""The QtWebEngine window used by the desktop app.

The renderer is bundled with the app, so Windows and Linux behave the same and no system
browser or webview runtime is required. The window is also the desktop client for servers on
other machines: it remembers their addresses and injects a way back to the local device.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from cygnusloom.desktop_servers import (
    FILE_NAME,
    ServerError,
    ServerStore,
    normalize_invite_url,
    normalize_server_url,
    origin_of,
)

WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 820
MIN_WIDTH = 960
MIN_HEIGHT = 640
# The window's size, position, and mode are remembered here, beside the game's other data.
WINDOW_STATE_FILE = "window.ini"
# The taskbar and window icon shared with the web favicon.
BRAND_ICON_FILE = Path(__file__).parent / "web" / "static" / "brand" / "cygnus-loom-256.png"
# How long a browser sign-in may take before the app stops listening.
BROWSER_LOGIN_SECONDS = 5 * 60


class DesktopUnavailable(RuntimeError):
    """PySide6 is not installed in a source checkout."""


def brand_icon_path() -> Path:
    """The app icon used by the desktop window."""
    return BRAND_ICON_FILE


def window_state_path(data_dir: Path) -> Path:
    """Where the desktop window remembers its size, position, and mode."""
    return data_dir / WINDOW_STATE_FILE


def restore_window_state(settings: Any, window: Any) -> None:
    """Restore the saved geometry and show the window in its saved mode. A first launch gets
    the default size."""
    geometry = settings.value("geometry")
    if geometry is not None:
        window.restoreGeometry(geometry)
    else:
        window.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    window.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
    if bool(settings.value("fullscreen", False, type=bool)):
        window.showFullScreen()
    elif bool(settings.value("maximized", False, type=bool)):
        window.showMaximized()
    else:
        window.show()


def save_window_state(settings: Any, window: Any) -> None:
    """Remember the window's geometry and whether it was maximized or full screen."""
    settings.setValue("geometry", window.saveGeometry())
    settings.setValue("maximized", window.isMaximized())
    settings.setValue("fullscreen", window.isFullScreen())
    settings.sync()


def browser_login_url(
    server_url: str, redirect_uri: str, state: str, next_path: str = "/campaigns"
) -> str:
    """The server page that signs in and returns to the app's loopback listener."""
    query = urlencode({"redirect_uri": redirect_uri, "state": state, "next": next_path})
    return f"{server_url.rstrip('/')}/desktop/login?{query}"


def return_button_script(local_url: str) -> str:
    """Fill the prepared desktop slots with a native way back to the local device."""
    local = local_url.rstrip("/")
    return f"""
    (() => {{
      const local = {json.dumps(local)};
      if (location.origin === new URL(local).origin) return;
      if (!/^https?:$/.test(location.protocol)) return;
      if (window.__cygnusReturnInstalled) return;
      window.__cygnusReturnInstalled = true;
      const href = local + '/campaigns?desktop_home=1';
      const goHome = (event) => {{
        event.preventDefault();
        console.log('cygnus-desktop:' + JSON.stringify({{ action: 'home' }}));
      }};
      const openExternal = (event) => {{
        const anchor = event.target && event.target.closest
          ? event.target.closest('a[href]')
          : null;
        if (!anchor) return;
        let target;
        try {{
          target = new URL(anchor.href, location.href);
        }} catch (error) {{
          return;
        }}
        if (target.origin === location.origin) return;
        if (target.origin === new URL(local).origin) return;
        if (!/^https?:$/.test(target.protocol) && target.protocol !== 'mailto:') return;
        event.preventDefault();
        event.stopPropagation();
        console.log(
          'cygnus-desktop:' + JSON.stringify({{ action: 'external', url: target.href }})
        );
      }};
      let guardInstalled = false;
      const installGuard = () => {{
        if (guardInstalled || window.__cygnusExternalGuard) return;
        guardInstalled = true;
        window.__cygnusExternalGuard = true;
        document.addEventListener('click', openExternal, true);
      }};
      const fill = (attempt = 0) => {{
        const top = document.getElementById('desktop-slot');
        const side = document.getElementById('desktop-slot-sidebar');
        if (!top && !side && attempt < 20) {{
          setTimeout(() => fill(attempt + 1), 100);
          return;
        }}
        if (!top && !side) return;
        installGuard();
        if (top) {{
          const link = document.createElement('a');
          link.className = 'desktop-home';
          link.href = href;
          link.textContent = '\\u2190 Return to my device';
          link.title = 'Return to Cygnus Loom on this device';
          link.addEventListener('click', goHome);
          top.replaceChildren(link);
          top.hidden = false;
        }}
        if (side) {{
          const link = document.createElement('a');
          link.className = 'sidebar-item';
          link.href = href;
          link.title = 'Return to my device';
          link.setAttribute('aria-label', 'Return to my device');
          link.addEventListener('click', goHome);
          link.innerHTML =
            '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none"' +
            ' stroke="currentColor" stroke-width="1.8" stroke-linecap="round"' +
            ' stroke-linejoin="round"><path d="M4 11 12 4l8 7"/>' +
            '<path d="M6 10v9h12v-9"/></svg>';
          side.replaceChildren(link);
          side.hidden = false;
        }}
      }};
      if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', fill, {{ once: true }});
      }} else {{
        fill();
      }}
    }})();
    """


CONSOLE_PREFIX = "cygnus-desktop:"


def desktop_sentinel_script(local_origin: str) -> str:
    """Tell the local main-menu page that it runs inside the desktop window.

    QtWebEngine refuses to load ``qrc:///`` scripts from HTTP pages, so page-to-window actions use
    a prefixed console message instead of a QWebChannel. The window accepts menu actions only while
    the page is on its own local origin, except the harmless home action.
    """
    return f"if (location.origin === {json.dumps(local_origin)}) window.__cygnusDesktop = true;"


def desktop_bridge_ready_script() -> str:
    """Answer a local page that asks for the desktop bridge.

    The document-creation sentinel normally sets this before ``desktop.js`` runs. If a navigation
    loses that injection, the page sends a handshake and this answer repairs it without a reload.
    """
    return "window.__cygnusDesktop = true;window.dispatchEvent(new Event('cygnus-desktop-ready'));"


def parse_desktop_action(payload: object) -> tuple[str, str] | None:
    """Validate one action from a menu: ``(action, value)``."""
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    server = payload.get("server")
    url = payload.get("url")
    if action == "connect" and isinstance(server, str) and server:
        return ("connect", server)
    if action == "login" and isinstance(server, str) and server:
        return ("login", server)
    if action == "browser" and isinstance(server, str) and server:
        return ("browser", server)
    if action == "invite" and isinstance(url, str) and url:
        return ("invite", url)
    if action == "external" and isinstance(url, str) and url:
        scheme = urlsplit(url).scheme.lower()
        if scheme in ("http", "https", "mailto"):
            return ("external", url)
    if action == "home":
        return ("home", "")
    return None


def needs_device_bar(page_origin: str, local_origin: str, has_desktop_controls: bool) -> bool:
    """Whether the window needs its native way back to the local device."""
    if not page_origin:
        return True
    if page_origin == local_origin:
        return False
    return not has_desktop_controls


def parse_add_action(payload: object) -> tuple[str, str] | None:
    """Validate an add-server action from the local menu: ``(url, name)``."""
    if not isinstance(payload, dict) or payload.get("action") != "add":
        return None
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        return None
    name = payload.get("name")
    return (url, name if isinstance(name, str) else "")


def should_hand_to_browser(
    scheme: str, target: str, allowed: set[str], *, invite_pending: bool
) -> bool:
    """Whether a clicked link leaves the app.

    An invitation finishes on a server that is not in the list yet, so its main-frame links stay
    in the window until the server has been added.
    """
    if scheme not in ("http", "https"):
        return True
    if target in allowed:
        return False
    return not invite_pending


class LoopbackReceiver:
    """One local callback request from the host browser.

    The listener binds to 127.0.0.1 on a free port and checks the state value it generated, so no
    other page can hand a sign-in code to the app.
    """

    def __init__(self, state: str, result: Callable[[str, str], None]):
        self.state = state
        self._result = result
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> str:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path != "/callback":
                    self.send_error(404)
                    return
                params = parse_qs(parsed.query)
                state = params.get("state", [""])[0]
                code = params.get("code", [""])[0]
                error = params.get("error", [""])[0]
                if not hmac.compare_digest(state, receiver.state):
                    self.send_error(400, "Invalid sign-in state")
                    return
                body = (
                    b'<!doctype html><meta charset="utf-8"><title>Signed in</title>'
                    b'<body style="font-family:system-ui,sans-serif;background:#15171c;'
                    b'color:#e8e8ec;padding:3rem"><h1>Signed in</h1>'
                    b"<p>Return to Cygnus Loom. You can close this tab.</p></body>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                receiver._result(code, error)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        with self._lock:
            self._server = server
            # Poll quickly so closing the sign-in window does not wait on the default
            # half-second sleep.
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="cygnus-loopback",
                daemon=True,
            )
            self._thread.start()
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/callback"

    def stop(self) -> None:
        with self._lock:
            server = self._server
            self._server = None
            thread = self._thread
            self._thread = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001, S110 - shutdown is best effort
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001, S110 - shutdown is best effort
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)


def run_window(url: str, title: str, data_dir: Path) -> int:
    """Open the game window and block until it closes. Returns the Qt exit code."""
    try:
        from PySide6.QtCore import QEvent, QSettings, QTimer, QUrl, Signal
        from PySide6.QtGui import QDesktopServices, QIcon
        from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
        from PySide6.QtWebEngineCore import (
            QWebEnginePage,
            QWebEngineProfile,
            QWebEngineScript,
            QWebEngineSettings,
        )
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import (
            QApplication,
            QFrame,
            QHBoxLayout,
            QLabel,
            QMainWindow,
            QMessageBox,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:  # pragma: no cover - exercised by the packaged build
        raise DesktopUnavailable(
            "The desktop edition needs PySide6. Install it with "
            "`pip install 'cygnusloom[desktop]'` or run the packaged build."
        ) from exc

    local_origin = origin_of(url)
    servers = ServerStore(data_dir / FILE_NAME)

    class LocalPage(QWebEnginePage):
        """Keep app pages in the window and send other sites to the system browser."""

        desktop_handler = None
        invite_navigation = None

        def __init__(self, profile, parent, allowed):
            super().__init__(profile, parent)
            self._allowed = allowed

        def acceptNavigationRequest(self, navigation_url, nav_type, is_main_frame):
            if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                invite = bool(
                    is_main_frame
                    and self.invite_navigation is not None
                    and self.invite_navigation()
                )
                if should_hand_to_browser(
                    navigation_url.scheme(),
                    origin_of(navigation_url.toString()),
                    self._allowed(),
                    invite_pending=invite,
                ):
                    QDesktopServices.openUrl(navigation_url)
                    return False
            return super().acceptNavigationRequest(navigation_url, nav_type, is_main_frame)

        def javaScriptConsoleMessage(self, level, message, line, source):
            if message.startswith(CONSOLE_PREFIX) and self.desktop_handler is not None:
                try:
                    payload = json.loads(message[len(CONSOLE_PREFIX) :])
                except ValueError:
                    return
                self.desktop_handler(payload)
                return
            super().javaScriptConsoleMessage(level, message, line, source)

    class DesktopWindow(QMainWindow):
        browser_result = Signal(str, str)
        page: Any = None

        def __init__(self):
            super().__init__()
            self.local_origin = local_origin
            self.current_server_id = ""
            self.browser_server_id = ""
            self.invite_pending = False
            self.invite_origin = ""
            self.invite_probed: set[str] = set()
            self.invite_replies: set[Any] = set()
            self.add_replies: set[Any] = set()
            self.loopback: LoopbackReceiver | None = None
            self.setWindowTitle(title)
            self.setWindowIcon(icon)
            self.view = QWebEngineView(self)
            self.page = LocalPage(profile, self.view, self.allowed_origins)
            self.page.desktop_handler = self._desktop_action
            self.page.invite_navigation = lambda: self.invite_pending
            self.view.setPage(self.page)
            self.escape_bar = QFrame(self)
            self.escape_bar.setObjectName("device-bar")
            bar_layout = QHBoxLayout(self.escape_bar)
            bar_layout.setContentsMargins(12, 8, 12, 8)
            bar_layout.setSpacing(10)
            self.home_button = QPushButton("\u2190 Return to my device", self.escape_bar)
            self.home_button.setObjectName("device-home")
            self.home_button.clicked.connect(lambda: self.go_home())
            self.escape_label = QLabel("", self.escape_bar)
            self.escape_label.setObjectName("device-label")
            bar_layout.addWidget(self.home_button)
            bar_layout.addWidget(self.escape_label, 1)
            self.escape_bar.setStyleSheet(
                "QFrame#device-bar { background: #1a1d25;"
                " border-bottom: 1px solid #2d3242; }"
                "QLabel#device-label { color: #9aa3b5; }"
                "QPushButton#device-home { color: #e8e8ec; background: #272d3c;"
                " border: 1px solid #3a4257; border-radius: 6px; padding: 6px 12px; }"
                "QPushButton#device-home:hover { background: #333b4e; }"
            )
            self.escape_bar.hide()
            self.shell = QWidget(self)
            shell_layout = QVBoxLayout(self.shell)
            shell_layout.setContentsMargins(0, 0, 0, 0)
            shell_layout.setSpacing(0)
            shell_layout.addWidget(self.escape_bar)
            shell_layout.addWidget(self.view, 1)
            self.setCentralWidget(self.shell)
            self.view.settings().setAttribute(
                QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False
            )
            self._install_script(
                "cygnus-desktop-sentinel",
                desktop_sentinel_script(local_origin),
                QWebEngineScript.InjectionPoint.DocumentCreation,
            )
            self._install_script(
                "cygnus-return",
                return_button_script(url),
                QWebEngineScript.InjectionPoint.DocumentCreation,
            )
            self.network = QNetworkAccessManager(self)
            self.view.loadFinished.connect(self._page_loaded)
            self.view.loadFinished.connect(lambda _ok: self.report_activation())
            self.page.titleChanged.connect(self._title_changed)
            self.browser_result.connect(self._browser_ready)
            self.view.setUrl(QUrl(url))

        def report_activation(self) -> None:
            if self.page is None:
                return
            active = self.isActiveWindow() and not self.isMinimized()
            state = "true" if active else "false"
            self.page.runJavaScript(f"window.setWindowActive?.({state})")

        def changeEvent(self, event: QEvent) -> None:
            super().changeEvent(event)
            if event.type() in (
                QEvent.Type.ActivationChange,
                QEvent.Type.WindowStateChange,
            ):
                self.report_activation()

        def go_home(self) -> None:
            self.stop_browser_login()
            self.current_server_id = ""
            self.invite_pending = False
            self.invite_origin = ""
            self.invite_probed = set()
            self.escape_bar.hide()
            self.view.setUrl(
                QUrl(f"{self.local_origin}/campaigns?desktop_home={secrets.token_urlsafe(6)}")
            )

        def _set_device_bar(self, origin: str, has_desktop_controls: object) -> None:
            if origin != origin_of(self.view.url().toString()):
                return
            self.escape_label.setText(urlsplit(origin).netloc if origin else "")
            self.escape_bar.setVisible(
                needs_device_bar(origin, self.local_origin, bool(has_desktop_controls))
            )

        def _install_script(self, name: str, source: str, point) -> None:
            script = QWebEngineScript()
            script.setName(name)
            script.setInjectionPoint(point)
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(False)
            script.setSourceCode(source)
            self.page.scripts().insert(script)

        def _desktop_action(self, payload: object) -> None:
            if isinstance(payload, dict) and payload.get("action") == "hello":
                if origin_of(self.page.url().toString()) == self.local_origin:
                    self.page.runJavaScript(desktop_bridge_ready_script())
                return
            action = parse_desktop_action(payload)
            if action is not None and action[0] == "home":
                self.go_home()
                return
            if action is not None and action[0] == "external":
                if self._is_trusted_page():
                    QDesktopServices.openUrl(QUrl(action[1]))
                return
            if origin_of(self.page.url().toString()) != self.local_origin:
                return
            add = parse_add_action(payload)
            if add is not None:
                self.add_server(*add)
                return
            if action is None:
                return
            name, value = action
            if name == "connect":
                self.connect_server(value)
            elif name == "login":
                self.login_server(value)
            elif name == "browser":
                self.login_with_browser(value)
            elif name == "invite":
                self.open_invite(value)

        def _is_trusted_page(self) -> bool:
            origin = origin_of(self.page.url().toString())
            if origin == self.local_origin:
                return True
            try:
                return any(
                    origin in (origin_of(server.url), origin_of(server.oidc_issuer))
                    for server in servers.list()
                )
            except Exception:  # noqa: BLE001 - a broken list must not break the page
                return False

        def _title_changed(self, text: str) -> None:
            self.setWindowTitle(text or title)

        def hand_download_to_browser(self, download: Any) -> None:
            """Send a download link to the system browser.

            QtWebEngine cancels downloads it is not told to save, and the app has no save flow.
            The system browser already has a download folder and the player's web sessions, so
            campaign exports work for the local game and for a server alike.
            """
            url = download.url()
            download.cancel()
            if not self._is_trusted_page():
                return
            if not QDesktopServices.openUrl(url):
                self.statusBar().showMessage(
                    "Could not open the download in your web browser.", 8000
                )

        def allowed_origins(self) -> set[str]:
            allowed = {self.local_origin, origin_of(self.view.url().toString())}
            try:
                for server in servers.list():
                    allowed.add(origin_of(server.url))
                    if server.oidc_issuer:
                        allowed.add(origin_of(server.oidc_issuer))
            except Exception:  # noqa: BLE001, S110 - a broken list must not block navigation
                pass
            if self.invite_origin:
                allowed.add(self.invite_origin)
            allowed.discard("")
            return allowed

        def _server(self, server_id: str):
            try:
                return servers.get(server_id)
            except ServerError as exc:
                QMessageBox.warning(self, title, str(exc))
                return None

        def connect_server(self, server_id: str) -> None:
            server = self._server(server_id)
            if server is None:
                return
            self.current_server_id = server.id
            self._touch(server.id)
            self.view.setUrl(QUrl(f"{server.url}/campaigns"))

        def login_server(self, server_id: str) -> None:
            server = self._server(server_id)
            if server is None:
                return
            self.current_server_id = server.id
            self._touch(server.id)
            path = "/auth/login" if server.oidc_issuer else "/login"
            self.view.setUrl(QUrl(f"{server.url}{path}"))

        def _touch(self, server_id: str) -> None:
            try:
                servers.touch(server_id)
            except ServerError:
                pass

        def add_server(self, url: str, name: str) -> None:
            try:
                value = normalize_server_url(url)
            except ServerError as exc:
                QMessageBox.warning(self, title, str(exc))
                return
            try:
                existing = servers.find_by_url(value)
            except ServerError:
                existing = None
            if existing is not None:
                self.statusBar().showMessage(f"{value} is already in the server list.", 8000)
                return
            request = QNetworkRequest(QUrl(f"{value}/desktop/info"))
            request.setRawHeader(b"Accept", b"application/json")
            request.setRawHeader(b"User-Agent", b"CygnusLoom-desktop")
            if hasattr(request, "setTransferTimeout"):
                request.setTransferTimeout(15000)
            reply = self.network.get(request)
            self.add_replies.add(reply)
            reply.finished.connect(lambda: self._add_probe_done(reply, value, name))

        def _add_probe_done(self, reply, url: str, name: str) -> None:
            self.add_replies.discard(reply)
            try:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                error = reply.errorString()
                body = bytes(reply.readAll().data())
            finally:
                reply.deleteLater()
            if status != 200:
                detail = error if error and error != "Unknown error" else f"HTTP {status}"
                QMessageBox.warning(self, title, f"Could not reach {url}.\n\n{detail}")
                return
            try:
                payload = json.loads(body)
            except ValueError:
                QMessageBox.warning(
                    self, title, f"{url} did not answer with Cygnus Loom server information."
                )
                return
            if not isinstance(payload, dict) or payload.get("app") != "cygnusloom":
                QMessageBox.warning(self, title, f"{url} is not a Cygnus Loom server.")
                return
            issuer = payload.get("oidc_issuer")
            issuer = issuer if isinstance(issuer, str) else ""
            try:
                server = servers.add(url, name, oidc_issuer=issuer)
            except ServerError as exc:
                QMessageBox.warning(self, title, str(exc))
                return
            self.current_server_id = server.id
            self.statusBar().showMessage(f"Added {server.name}.", 8000)
            self.view.setUrl(QUrl(f"{self.local_origin}/campaigns"))

        def login_with_browser(self, server_id: str) -> None:
            server = self._server(server_id)
            if server is None:
                return
            self.stop_browser_login()
            state = secrets.token_urlsafe(24)
            receiver = LoopbackReceiver(
                state, lambda code, error: self.browser_result.emit(code, error)
            )
            try:
                redirect_uri = receiver.start()
            except OSError as exc:
                QMessageBox.warning(self, title, f"Could not start the sign-in listener: {exc}")
                return
            self.loopback = receiver
            self.browser_server_id = server.id
            self.current_server_id = server.id
            QTimer.singleShot(BROWSER_LOGIN_SECONDS * 1000, lambda: self._browser_timeout(receiver))
            if not QDesktopServices.openUrl(
                QUrl(browser_login_url(server.url, redirect_uri, state))
            ):
                self.stop_browser_login()
                QMessageBox.warning(self, title, "Could not open the web browser for sign-in.")
                return
            self.statusBar().showMessage("Finish signing in in your web browser…")

        def stop_browser_login(self) -> None:
            receiver = self.loopback
            self.loopback = None
            if receiver is not None:
                receiver.stop()

        def _browser_timeout(self, receiver: LoopbackReceiver) -> None:
            if self.loopback is receiver:
                receiver.stop()
                self.loopback = None
                self.statusBar().showMessage("Browser sign-in timed out. Try again.", 8000)

        def _browser_ready(self, code: str, error: str) -> None:
            self.stop_browser_login()
            if error:
                self.statusBar().showMessage("Browser sign-in was cancelled.", 8000)
                return
            if not code:
                self.statusBar().showMessage("Browser sign-in did not return a code.", 8000)
                return
            server = self._server(self.browser_server_id)
            if server is None:
                return
            self.view.setUrl(QUrl(f"{server.url}/desktop/session?{urlencode({'code': code})}"))

        def open_invite(self, invite_url: str) -> None:
            try:
                value = normalize_invite_url(invite_url)
            except ServerError as exc:
                QMessageBox.warning(self, title, str(exc))
                return
            self.invite_pending = True
            self.invite_origin = origin_of(value)
            self.invite_probed = set()
            self.current_server_id = ""
            self.view.setUrl(QUrl(value))

        def _page_loaded(self, ok: bool) -> None:
            page_origin = origin_of(self.view.url().toString())
            if not ok:
                # Rejecting an external link emits loadFinished(False) while the current page
                # stays put. Do not show the escape bar for a page we are still on.
                if page_origin == self.local_origin:
                    self.escape_bar.hide()
                elif self._server_at(page_origin) is None:
                    self.escape_label.setText(urlsplit(page_origin).netloc if page_origin else "")
                    self.escape_bar.show()
                return
            if not page_origin:
                self.escape_bar.show()
                return
            if page_origin == self.local_origin:
                self.escape_bar.hide()
                self.current_server_id = ""
                self.invite_pending = False
                self.invite_origin = ""
                # The document-creation sentinel normally does this. Repeat it after load so
                # a page that lost the injection still gets a working menu, without reloading.
                self.page.runJavaScript(desktop_bridge_ready_script())
                return
            self.page.runJavaScript(
                "(() => {"
                " const top = document.getElementById('desktop-slot');"
                " if (top && !top.hidden && top.querySelector('a')) return true;"
                " const side = document.getElementById('desktop-slot-sidebar');"
                " return !!(side && !side.hidden && side.querySelector('a'));"
                "})()",
                lambda has_controls, origin=page_origin: self._set_device_bar(origin, has_controls),
            )
            if self.invite_pending and page_origin != self.invite_origin:
                self._probe_invite(page_origin)
            server = self._server_at(page_origin)
            if server is not None:
                self.current_server_id = server.id
                self._touch(server.id)
                return

        def _server_at(self, page_origin: str):
            try:
                for server in servers.list():
                    if origin_of(server.url) == page_origin:
                        return server
            except Exception:  # noqa: BLE001 - a broken list must not break the page
                return None
            return None

        def _probe_invite(self, page_origin: str) -> None:
            if page_origin in self.invite_probed:
                return
            self.invite_probed.add(page_origin)
            request = QNetworkRequest(QUrl(f"{page_origin}/desktop/info"))
            request.setRawHeader(b"Accept", b"application/json")
            request.setRawHeader(b"User-Agent", b"CygnusLoom-desktop")
            reply = self.network.get(request)
            self.invite_replies.add(reply)
            reply.finished.connect(lambda: self._invite_probe_done(reply, page_origin))

        def _invite_probe_done(self, reply, page_origin: str) -> None:
            self.invite_replies.discard(reply)
            try:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                body = bytes(reply.readAll().data())
            finally:
                reply.deleteLater()
            if status != 200:
                return
            try:
                payload = json.loads(body)
            except ValueError:
                return
            if not isinstance(payload, dict) or payload.get("app") != "cygnusloom":
                return
            issuer = payload.get("oidc_issuer")
            issuer = issuer if isinstance(issuer, str) else ""
            try:
                existing = servers.find_by_url(page_origin)
                if existing is None:
                    server = servers.add(page_origin, oidc_issuer=issuer)
                else:
                    server = servers.set_issuer(existing.id, issuer) if issuer else existing
            except ServerError as exc:
                self.statusBar().showMessage(str(exc), 8000)
                return
            self.current_server_id = server.id
            self.invite_pending = False
            self.invite_origin = ""
            self.statusBar().showMessage(f"Added {server.name}.", 8000)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(title)
    app.setApplicationDisplayName(title)
    icon = QIcon(str(brand_icon_path()))
    app.setWindowIcon(icon)

    web_dir = data_dir / "webview"
    cache_dir = web_dir / "cache"
    web_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    profile = QWebEngineProfile("cygnusloom", app)
    profile.setPersistentStoragePath(str(web_dir))
    profile.setCachePath(str(cache_dir))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )

    window = DesktopWindow()
    # Downloads cannot be saved in the window; the host browser downloads them instead.
    profile.downloadRequested.connect(window.hand_download_to_browser)
    settings = QSettings(str(window_state_path(data_dir)), QSettings.Format.IniFormat)
    restore_window_state(settings, window)
    app.aboutToQuit.connect(window.stop_browser_login)
    app.aboutToQuit.connect(lambda: save_window_state(settings, window))
    return app.exec()
