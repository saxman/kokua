"""Where the live settings table comes from: Kokua's core entries plus each toolset's declarations.

This is the seam between the bottom layer and the toolsets. ``config/file.py`` and ``config/table.py``
import nothing above them, which is what keeps a config parse independent of what is installed; the
joining-up has to happen *somewhere*, and doing it here rather than inside either of those keeps that
rule intact while leaving the code next to what it configures.

Only core and entry-point toolsets are consulted. An MCP-derived toolset is excluded because its
existence comes from the config file, so it cannot contribute to the schema that parses that file.
"""

from __future__ import annotations

from kokua.config.schema import AssistantConfig
from kokua.config.table import CORE_RUNTIME_SETTINGS, TYPE_LABELS, RuntimeSetting, SettingsTable


def declaring_toolsets() -> list:
    """The toolsets that may own a config section: Kokua's core ones and any installed plugin.

    Discovery is not gated on ``load_plugins``: reading an entry point's declaration executes no
    plugin behavior, and a config file mentioning a plugin's section must stay parseable either way.
    """
    from kokua.plugins import discover_toolsets
    from kokua.toolsets.core import CORE_TOOLSETS

    return [*CORE_TOOLSETS, *discover_toolsets().values()]


def build_settings_table(toolsets=None) -> SettingsTable:
    """The live table: core entries plus one entry per hot setting a toolset declared."""
    contributed = [
        RuntimeSetting(setting.key, toolset.name, setting.kind, toolset=toolset.name)
        for toolset in (declaring_toolsets() if toolsets is None else toolsets)
        for setting in toolset.settings
        if setting.hot
    ]
    return SettingsTable([*CORE_RUNTIME_SETTINGS, *contributed])


def startup_schema(toolsets=None) -> dict:
    """Schema entries for the *cold* settings a toolset declared, which the table does not carry.

    A non-hot setting is still a real config key that must parse and must reject a wrong type; it just
    cannot change without a restart.
    """
    return {
        (toolset.name, setting.key): (
            f"{toolset.name}.{setting.key}",
            (setting.kind,),
            TYPE_LABELS[setting.kind],
            None,
        )
        for toolset in (declaring_toolsets() if toolsets is None else toolsets)
        for setting in toolset.settings
        if not setting.hot
    }


def seed_toolset_defaults(config: AssistantConfig, toolsets=None) -> None:
    """Fill in every declared default the config file did not set.

    Done after parsing rather than during it, so a toolset always reads a complete view whether or not
    the user has a section for it, and ``config.toolset_settings`` never carries a key no toolset
    declared.
    """
    for toolset in declaring_toolsets() if toolsets is None else toolsets:
        if not toolset.settings:
            continue
        bucket = config.toolset_settings.setdefault(toolset.name, {})
        for setting in toolset.settings:
            bucket.setdefault(setting.key, setting.default)
