"""Where the live settings table comes from: Kokua's core entries plus each toolset's declarations.

This is the seam between the bottom layer and the toolsets. ``config/file.py`` and ``config/table.py``
import nothing above them, which is what keeps a config parse independent of what is installed; the
joining-up has to happen *somewhere*, and doing it here rather than inside either of those keeps that
rule intact while leaving the code next to what it configures.

Only core and entry-point toolsets are consulted. An MCP-derived toolset is excluded because its
existence comes from the config file, so it cannot contribute to the schema that parses that file.
"""

from __future__ import annotations

from typing import Iterator

from kokua.config.file import core_sections
from kokua.config.schema import AssistantConfig
from kokua.config.table import CORE_RUNTIME_SETTINGS, TYPE_LABELS, RuntimeSetting, SettingsTable


def declared_settings(toolsets) -> Iterator[tuple]:
    """Each declared setting paired with the toolset declaring it, rejecting one Kokua cannot honor.

    The single seam every consumer below goes through, so a bad declaration fails once, at startup, naming
    the toolset that has to change. Validated here rather than in ``Toolset`` itself because none of these
    rules is knowable from one ``Setting`` alone: two of them need the config sections the core already
    parses, and the third needs the kinds the schema and the panel sanitizer can actually handle.

    A toolset declaring nothing is skipped before any check, so a toolset merely *named* after a core
    section is fine: what is refused is claiming that section's keys.
    """
    from kokua.toolsets.registry import ToolsetError

    reserved = core_sections()
    for toolset in toolsets:
        if not toolset.settings:
            continue
        if toolset.name in reserved:
            raise ToolsetError(
                f"toolset {toolset.name!r} declares settings, but [{toolset.name}] is a config.toml section "
                "Kokua's own core parses. A toolset's settings section is always its own name, so its keys "
                "would take over that section and the core setting behind them would silently stay at its "
                f"default. Rename the toolset. Sections the core owns: {', '.join(sorted(reserved))}."
            )
        seen: set[str] = set()
        for setting in toolset.settings:
            if setting.kind not in TYPE_LABELS:
                supported = ", ".join(sorted(kind.__name__ for kind in TYPE_LABELS))
                raise ToolsetError(
                    f"toolset {toolset.name!r} declares setting {setting.key!r} of unsupported type "
                    f"{setting.kind.__name__}. A config.toml setting must be one of: {supported}."
                )
            if setting.key in seen:
                raise ToolsetError(
                    f"toolset {toolset.name!r} declares setting {setting.key!r} twice. One key gets one "
                    f"declaration: [{toolset.name}].{setting.key} has a single value, a single type, and a "
                    "single default."
                )
            seen.add(setting.key)
            yield toolset, setting


def declaring_toolsets() -> list:
    """The toolsets that may own a config section: Kokua's core ones and any installed plugin.

    Discovery is not gated on ``load_plugins``: reading an entry point's declaration executes no
    plugin behavior, and a config file mentioning a plugin's section must stay parseable either way.
    """
    # Deferred rather than module-level: toolsets/config.py imports this module at module scope (for
    # startup_schema()), and toolsets/core.py imports toolsets/config.py, so a module-level import of
    # toolsets.core here would close that loop and break `import kokua.toolsets.core` on a
    # partially-initialized module.
    from kokua.plugins import discover_toolsets
    from kokua.toolsets.core import CORE_TOOLSETS

    return [*CORE_TOOLSETS, *discover_toolsets().values()]


def build_settings_table(toolsets=None) -> SettingsTable:
    """The live table: core entries plus one entry per hot setting a toolset declared."""
    contributed = [
        RuntimeSetting(setting.key, toolset.name, setting.kind, toolset=toolset.name)
        for toolset, setting in declared_settings(declaring_toolsets() if toolsets is None else toolsets)
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
        for toolset, setting in declared_settings(declaring_toolsets() if toolsets is None else toolsets)
        if not setting.hot
    }


def seed_toolset_defaults(config: AssistantConfig, toolsets=None) -> None:
    """Fill in every declared default the config file did not set.

    Done after parsing rather than during it, so a toolset always reads a complete view whether or not
    the user has a section for it, and ``config.toolset_settings`` never carries a key no toolset
    declared.
    """
    for toolset, setting in declared_settings(declaring_toolsets() if toolsets is None else toolsets):
        config.toolset_settings.setdefault(toolset.name, {}).setdefault(setting.key, setting.default)
