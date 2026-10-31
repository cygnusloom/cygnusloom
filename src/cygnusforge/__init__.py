"""Cygnus Forge: a minimal web chat over an LLM."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cygnusforge")
except PackageNotFoundError:  # pragma: no cover - running from an unpacked source tree
    __version__ = "0.0.0"
