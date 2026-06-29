"""Kokua (Hawaiian: help, assistance): a personal AI assistant.

A hackable, modular personal-assistant application built on the AIMU library. Front ends
(CLI, web, ...) and tool-packs are discovered as plugins via Python entry points, so the
assistant grows by installing modules rather than editing the core.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kokua")
except PackageNotFoundError:  # running from a source checkout that isn't installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
