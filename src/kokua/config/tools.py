"""The assistant's own ``read_config`` / ``update_config`` tools.

These let the assistant inspect and repair its configuration during a conversation (e.g. "you keep
failing to send email, check your config"). Reads are unrestricted; writes go through the same
validation as the config file and are refused for a small security-critical blocklist that only a
hand-edit may change. A change to a hot-appliable setting (model, generation kwargs, display and
planning flags) is applied to the running session; every other setting is written to ``config.toml``
and takes effect on the next restart, which the tool says so the assistant reports it honestly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from aimu.tools import tool

from kokua.config import store as config_store
from kokua.config import table as runtime_settings
from kokua.config import file as settings
from kokua.toolsets.registry import Toolset

logger = logging.getLogger(__name__)

# Keys the assistant may never change, even behind the approval prompt: the tool-approval gate itself,
# the locked email recipient, and where all state lives. Only a hand-edit of config.toml changes these.
BLOCKLIST: frozenset[tuple[str, str]] = frozenset(
    {("security", "confirm_tools"), ("email", "to"), ("paths", "data_dir")}
)


def make_config_tools(config_path: Path, apply_hot: Callable[[str, str, object], Awaitable[None]]) -> list[Callable]:
    """Build ``read_config`` / ``update_config`` bound to the config file and the live-apply callback.

    ``apply_hot(section, key, value)`` applies a hot-appliable change (model, generation kwargs, display
    and planning flags) to the running session in memory; it is awaited BEFORE the value is written to
    disk, so a change that fails to apply (e.g. an invalid model) is not persisted.
    """

    @tool
    async def read_config() -> str:
        """Return the current contents of config.toml so you can diagnose a configuration problem.

        Use this before update_config to see what is set, and to check keys and section names. If no
        config file exists yet, all settings are at their built-in defaults.
        """
        if not config_path.exists():
            return "No config file exists yet; all settings are at their built-in defaults."
        return config_path.read_text(encoding="utf-8")

    @tool
    async def update_config(section: str, key: str, value: str) -> str:
        """Change one setting in config.toml, e.g. section="email", key="host", value="smtp.gmail.com".

        Pass the value as a string; it is coerced to the setting's real type (numbers, true/false, and
        comma-separated lists are understood). Setting the model, a generation parameter (temperature,
        max_tokens, ...), or a display/planning flag takes effect immediately in this session; any other
        setting is saved and takes effect the next time Kokua restarts (the result says which). A few
        security-critical keys cannot be changed here and must be hand-edited in the file.
        """
        if (section, key) in BLOCKLIST:
            return (
                f"[{section}].{key} is security-critical and can only be changed by hand-editing "
                "config.toml, not with this tool."
            )
        try:
            coerced = settings.coerce_config_string(section, key, value)
        except settings.ConfigError as error:
            return f"Rejected: {error}"

        # Apply a hot change to the live session first; only persist if it took (so a bad model, say,
        # is not written to config.toml and left to break the next startup).
        if runtime_settings.is_hot(section, key):
            try:
                await apply_hot(section, key, coerced)
            except Exception as error:
                logger.warning("Could not apply [%s].%s live", section, key, exc_info=True)
                return f"[{section}].{key} could not be applied: {error}. No change was saved."
            config_store.set_value(config_path, section, key, coerced)
            return f"Set [{section}].{key} = {coerced!r} and applied it to the current session."

        config_store.set_value(config_path, section, key, coerced)
        return f"Set [{section}].{key} = {coerced!r} in config.toml. It takes effect the next time Kokua restarts."

    return [read_config, update_config]


TOOLSET = Toolset(
    name="config",
    description="Read config.toml and change a runtime setting, persisted back to the file.",
    build=lambda ctx: make_config_tools(ctx.config.config_path, ctx.state.reapply_config),
    cross_cutting=True,
)
