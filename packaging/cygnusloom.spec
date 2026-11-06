# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build for the single-player desktop edition.

Build after `uv sync --group build`:

    uv run pyinstaller packaging/cygnusloom.spec

The result is `dist/CygnusLoom.exe` on Windows and `dist/CygnusLoom` on Linux. QtWebEngine
is bundled, so the window does not depend on a system webview or browser.
"""

from pathlib import Path

import cygnusloom
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
PACKAGE = Path(cygnusloom.__file__).resolve().parent

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
)
