"""Cygnus Loom: a minimal web chat over an LLM."""

from functools import cache
from importlib.metadata import PackageNotFoundError, distribution, metadata, version
from pathlib import Path

DISTRIBUTION = "cygnusloom"
PACKAGE_DIR = Path(__file__).resolve().parent
# The repository root when the package runs from a source tree instead of an installed wheel.
SOURCE_TREE = PACKAGE_DIR.parents[1]


def _project_url(label: str) -> str:
    """A `[project.urls]` entry, recorded in the installed distribution's metadata."""
    for entry in metadata(DISTRIBUTION).get_all("Project-URL") or ():
        name, _, url = entry.partition(",")
        if name.strip() == label:
            return url.strip()
    return ""


def _first_text(paths: list[Path]) -> str:
    """The contents of the first file that exists, or an empty string."""
    for path in paths:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return ""


try:
    __version__ = version(DISTRIBUTION)
    # The AGPL asks a network service to offer its users the source of the version it runs.
    SOURCE_URL = _project_url("Source")
    WEBSITE_URL = _project_url("Homepage")
except PackageNotFoundError:  # pragma: no cover - running from an unpacked source tree
    __version__ = "0.0.0"
    SOURCE_URL = ""
    WEBSITE_URL = ""


@cache
def license_text() -> str:
    """The license file that ships with the engine, which the app serves at `/license.txt`."""
    installed: list[Path] = []
    try:
        installed_distribution = distribution(DISTRIBUTION)
        installed = [
            Path(installed_distribution.locate_file(path))
            for path in installed_distribution.files or ()
            if path.name == "LICENSE"
        ]
    except PackageNotFoundError:  # pragma: no cover - running from an unpacked source tree
        pass
    return _first_text([*installed, SOURCE_TREE / "LICENSE"])


@cache
def content_credits() -> str:
    """The credits for bundled rules and worlds, kept beside the code and at a source tree root."""
    return _first_text([PACKAGE_DIR / "CREDITS.md", SOURCE_TREE / "CREDITS.md"])
