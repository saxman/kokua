"""The ``config`` toolset: the assistant's own ``read_config`` / ``update_config``.

These let the assistant inspect and repair its configuration during a conversation (e.g. "you keep
failing to send email, check your config"). Reads are unrestricted; writes go through the same
validation as the config file and are refused for the small security-critical set only a hand-edit may
change (``config.store.is_locked``).

The policy, the coercion, and the apply-then-persist ordering are all in ``config/store.py``. What is
here is the two tool schemas and the four sentences that report what happened -- including the one that
says a change waits for a restart, which is what keeps the assistant honest about a cold setting.

One thing this module has to supply rather than merely wrap: *which keys exist*. ``config/`` cannot see
the installed toolsets, so the cold half of their declarations is assembled here and handed down (see
:func:`make_config_tools`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from aimu.tools import tool

from kokua.config import file as settings
from kokua.config import store as config_store
from kokua.config.settings_sources import startup_schema
from kokua.toolsets.registry import Toolset


def make_config_tools(
    config_path: Path, apply_hot: Callable[[str, str, object], Awaitable[None]], table
) -> list[Callable]:
    """Build ``read_config`` / ``update_config`` bound to the config file, the live-apply callback, and
    the live settings table, so ``update_config`` resolves a key against the same declarations the
    settings panel does.

    The table is only half of those declarations: it holds hot settings, so the *cold* keys a toolset
    declared are resolved from ``startup_schema()``, computed once here. This module is above ``config/``
    and so may read the installed toolsets, which is the reason the seam is here rather than inside
    ``config.store`` -- and it is the same route ``kokua.cli`` takes to build the parse schema, so the
    tool and the file agree about which keys exist by construction. Without it the assistant would answer
    "unknown config key" for a cold toolset key sitting in the user's own file.
    """
    cold_schema = startup_schema()

    @tool
    async def read_config() -> str:
        """Return the current contents of config.toml so you can diagnose a configuration problem.

        Use this before update_config to see what is set, and to check keys and section names. If no
        config file exists yet, all settings are at their built-in defaults.
        """
        text = config_store.read_text(config_path)
        if text is None:
            return "No config file exists yet; all settings are at their built-in defaults."
        return text

    @tool
    async def update_config(section: str, key: str, value: str) -> str:
        """Change one setting in config.toml, e.g. section="email", key="host", value="smtp.gmail.com".

        Pass the value as a string; it is coerced to the setting's real type (numbers, true/false, and
        comma-separated lists are understood). A setting marked hot -- the model, a generation parameter
        (temperature, max_tokens, ...), or any other setting flagged to apply live -- takes effect
        immediately in this session; any other setting is saved and takes effect the next time Kokua
        restarts (the result says which). A few security-critical keys cannot be changed here and must
        be hand-edited in the file.
        """
        try:
            applied = await config_store.apply_setting(
                config_path, section, key, value, apply_hot, table=table, extra_schema=cold_schema
            )
        except config_store.SettingLocked:
            return (
                f"[{section}].{key} is security-critical and can only be changed by hand-editing "
                "config.toml, not with this tool."
            )
        except settings.ConfigError as error:
            return f"Rejected: {error}"
        except config_store.HotApplyFailed as error:
            return f"[{section}].{key} could not be applied: {error}. No change was saved."
        if applied.hot:
            return f"Set [{section}].{key} = {applied.value!r} and applied it to the current session."
        return (
            f"Set [{section}].{key} = {applied.value!r} in config.toml. It takes effect the next time Kokua restarts."
        )

    return [read_config, update_config]


TOOLSET = Toolset(
    name="config",
    description="Read config.toml and change a runtime setting, persisted back to the file.",
    build=lambda ctx: make_config_tools(ctx.config.config_path, ctx.state.reapply_config, ctx.state.settings_table),
    cross_cutting=True,
)
