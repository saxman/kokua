"""Applying a settings change to a running assistant, and persisting it to config.toml.

Everything here is driven by the :class:`~kokua.config.table.SettingsTable` it is handed: reading the
current values back, applying them live, mirroring the display flags onto the channel, and writing them
to their own ``[section].key``. Adding a setting -- Kokua's own or a toolset's -- touches none of this
code, and a setting a toolset owns is applied and persisted exactly the way a core one is.

The model is deliberately absent: every agent's model comes from its own ``[agents.*]`` table or the
``[assistant].model`` default, both read at startup, and no live client is ever rebound to another one.
Offering it as a runtime setting could only report a change it had not made, against a table this path
cannot write.

Sampling parameters are not here either, and for the same reason, but they are set elsewhere: AIMU owns
their precedence chain (client fallbacks, then the model card's tuned profile, then
``client.default_generate_kwargs``, then the per-call dict), and ``[assistant.generation]`` plus each
``[agents.<name>.generation]`` write that third tier at startup, with only the keys those tables name. A
runtime setting always holds a value, so it would write the tier even when the user asked for nothing,
shadowing the card's tuned profile -- which is why that tier is startup-only and not a runtime setting.
"""

from __future__ import annotations

from typing import Callable, Optional

from kokua.config import store as config_store
from kokua.config.table import SettingsTable
from kokua.config import AssistantConfig
from kokua.registry.context import LiveState


class SettingsApplier:
    """Reads, applies, and persists the runtime-mutable settings."""

    def __init__(
        self,
        config: AssistantConfig,
        ui,
        gate,
        *,
        table: SettingsTable,
        state: Callable[[], LiveState],
    ):
        self._config = config
        self._ui = ui
        self._gate = gate
        self._table = table
        # Lazy accessor rather than the object itself: constructed before Assistant.create builds the
        # LiveState this needs, so a closure that reads it at call time is the only shape that works.
        self._state = state
        self._client_factory: Optional[Callable[[str], object]] = None

    # --- the client factory ---------------------------------------------------------------------

    @property
    def client_factory(self) -> Callable[[str], object]:
        return self._client_factory

    def set_client_factory(self, factory: Callable[[str], object]) -> Callable[[str], object]:
        """Record the factory later conversations build their clients from, and return it."""
        self._client_factory = factory
        return factory

    # --- read ------------------------------------------------------------------------------------

    def current(self) -> dict:
        """The effective runtime settings, in the wire shape a settings client reads."""
        return {s.wire_key: s.read(self._config, self._ui.display_flag) for s in self._table.settings}

    # --- write -----------------------------------------------------------------------------------

    async def apply_and_persist(self, incoming: dict) -> None:
        """Apply an incoming settings payload at runtime and write it to config.toml so it survives restarts."""
        applied = await self.apply(self._table.sanitize(incoming))
        self.persist(applied)

    async def apply(self, settings: dict) -> dict:
        """Apply a sanitized settings dict live; returns it, for the caller to persist.

        Everything happens under an exclusive gate hold (waits for in-flight turns to drain, blocks
        new ones), so no turn reads a half-applied set.
        """
        async with self._gate.exclusive():
            for setting in self._table.settings:
                if setting.wire_key not in settings:
                    continue
                setting.write(self._config, settings[setting.wire_key], self._ui.set_display_flag)
        return settings

    def persist(self, settings: dict) -> None:
        """Write an applied settings dict back into config.toml, one ``[section].key`` per setting."""
        path = self._config.config_path
        for setting in self._table.settings:
            if setting.wire_key in settings:
                config_store.set_value(path, setting.section, setting.toml_key, settings[setting.wire_key])

    async def apply_one(self, section: str, key: str, value) -> None:
        """Apply one hot ``update_config`` change live (no persist; the tool writes disk itself).

        Builds the wire-shaped settings dict for the single change and applies it. Raises if it cannot
        be applied, so the tool skips persisting a change that did not take.
        """
        applied: dict = {}
        setting = self._table.by_toml(section, key)
        if setting is not None:
            applied[setting.wire_key] = value
        await self.apply(self._table.sanitize(applied))
