"""PyInstaller one-file build for the single-player desktop edition.

Build after `uv sync --group build`:

    uv run pyinstaller packaging/cygnusloom.spec

The result is `dist/CygnusLoom.exe` on Windows and `dist/CygnusLoom` on Linux. QtWebEngine
is bundled, so the window does not depend on a system webview or browser.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

import cygnusloom

ROOT = Path(SPECPATH).resolve().parent
PACKAGE = Path(cygnusloom.__file__).resolve().parent

# The Windows executable icon. The Qt window icon is set from the same brand asset at runtime.
EXE_ICON = (
    PACKAGE / "web" / "static" / "brand" / "cygnus-loom.ico"
    if sys.platform == "win32"
    else None
)

# Templates, static files, rulesets/worlds, CREDITS, and LICENSE.
datas = collect_data_files("cygnusloom")
# Alembic loads these files at runtime, so they are data, not imported modules.
datas += [(str(PACKAGE / "migrations"), "cygnusloom/migrations")]
datas += copy_metadata("cygnusloom")
# PySide6 and QtWebEngine are LGPL-licensed; ship their metadata and license files too.
for distribution in ("PySide6", "PySide6-Essentials", "PySide6-Addons", "PySide6-WebEngine"):
    try:
        datas += copy_metadata(distribution)
    except Exception:  # noqa: BLE001 - a split distribution name varies between releases
        pass

hiddenimports = collect_submodules("cygnusloom")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
]

a = Analysis(
    [str(ROOT / "packaging" / "desktop_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["psycopg", "psycopg_binary", "pytest", "tests"],
    noarchive=False,
)

# The build runner's C++ runtime and fontconfig are older than what many players' rolling
# desktops ship. Bundling them makes the host's Mesa/EGL drivers resolve against the old
# copies (for example, GLIBCXX_3.4.32 is missing), so QtWebEngine cannot create a context.
# Qt/PySide6 need only GLIBCXX_3.4.29, below the glibc 2.35 desktop baseline, so use the
# host's copies instead. The launcher reports a clear error when the host is too old.
HOST_LIBRARIES = ("libstdc++.so", "libgcc_s.so", "libfontconfig.so")
host_libraries = [
    entry[0] for entry in a.binaries if Path(entry[0]).name.startswith(HOST_LIBRARIES)
]
if host_libraries:
    print("Using the host's system libraries: " + ", ".join(sorted(host_libraries)))
a.binaries = [
    entry for entry in a.binaries if not Path(entry[0]).name.startswith(HOST_LIBRARIES)
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CygnusLoom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(EXE_ICON) if EXE_ICON and EXE_ICON.is_file() else None,
)
