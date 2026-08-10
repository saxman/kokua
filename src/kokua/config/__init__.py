"""Settings: the schema, the file that holds them, and the tools that change them.

``config.toml`` is the single source of settings. This package owns reading it (``file``), writing
it back with its comments intact (``store``), the dataclass it parses into (``schema``), the
declaration of which settings are changeable at runtime (``table``), and the assistant's own
``read_config``/``update_config`` tools (``tools``).

``AssistantConfig`` is re-exported here because it is Kokua's most-imported name and part of the
plugin contract: a ``ToolPack.build(config)`` receives one.
"""

from .file import ConfigError, example_text, load, resolve_path
from .schema import AssistantConfig, MCPServerConfig

__all__ = [
    "AssistantConfig",
    "MCPServerConfig",
    "ConfigError",
    "example_text",
    "load",
    "resolve_path",
]
