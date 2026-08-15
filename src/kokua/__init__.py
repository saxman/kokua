"""Kokua (Hawaiian: help, assistance): a personal AI assistant.

A hackable, modular personal-assistant application built on the AIMU library. Front ends
(CLI, web, ...) and toolsets are discovered as plugins via Python entry points, so the
assistant grows by installing modules rather than editing the core.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

try:
    __version__ = version("kokua")
except PackageNotFoundError:  # running from a source checkout that isn't installed
    __version__ = "0.0.0+unknown"

if TYPE_CHECKING:
    from .config import AssistantConfig
    from .core import Assistant

__all__ = ["__version__", "Assistant", "AssistantConfig"]


def __getattr__(name: str):
    """Expose the two headline names lazily (PEP 562).

    Lazily, because importing ``Assistant`` pulls in ``aimu.aio`` and the whole runtime. ``import
    kokua`` must stay cheap: ``plugins`` already goes out of its way to avoid that cost so a
    front-end listing does not pay for a model client it will never build.
    """
    if name == "Assistant":
        from .core import Assistant

        return Assistant
    if name == "AssistantConfig":
        from .config import AssistantConfig

        return AssistantConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
