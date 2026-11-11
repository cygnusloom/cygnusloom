"""The QtWebEngine window used by the single-player edition.

The renderer is bundled with the app, so Windows and Linux behave the same and no system
browser or webview runtime is required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 820
MIN_WIDTH = 960
MIN_HEIGHT = 640
# The window's size, position, and mode are remembered here, beside the game's other data.
WINDOW_STATE_FILE = "window.ini"
# The taskbar and window icon shared with the web favicon.
BRAND_ICON_FILE = Path(__file__).parent / "web" / "static" / "brand" / "cygnus-loom-256.png"


class DesktopUnavailable(RuntimeError):
    """PySide6 is not installed in a source checkout."""


def brand_icon_path() -> Path:
    """The app icon used by the single-player window."""
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


def run_window(url: str, title: str, data_dir: Path) -> int:
    """Open the game window and block until it closes. Returns the Qt exit code."""
    try:
        from PySide6.QtCore import QSettings, QUrl
        from PySide6.QtGui import QDesktopServices, QIcon
        from PySide6.QtWebEngineCore import (
            QWebEnginePage,
            QWebEngineProfile,
            QWebEngineSettings,
        )
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication, QMainWindow
    except ImportError as exc:  # pragma: no cover - exercised by the packaged build
        raise DesktopUnavailable(
            "The desktop edition needs PySide6. Install it with "
            "`pip install 'cygnusloom[desktop]'` or run the packaged build."
        ) from exc

    class LocalPage(QWebEnginePage):
        """Keep the game inside the window; send documentation links to the system browser."""

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            external = (
                nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked
                and url.host() not in ("127.0.0.1", "localhost", "::1")
            )
            if external:
                QDesktopServices.openUrl(url)
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)

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

    window = QMainWindow()
    window.setWindowTitle(title)
    window.setWindowIcon(icon)
    view = QWebEngineView(window)
    view.setPage(LocalPage(profile, view))
    # The desktop window may start the player's background music without an extra click. The
    # browser edition still follows the browser's autoplay policy, so its music starts on the
    # player's first interaction.
    view.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
    view.setUrl(QUrl(url))
    window.setCentralWidget(view)
    settings = QSettings(str(window_state_path(data_dir)), QSettings.Format.IniFormat)
    restore_window_state(settings, window)
    app.aboutToQuit.connect(lambda: save_window_state(settings, window))
    return app.exec()
