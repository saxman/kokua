"""Applying a settings change to a running assistant, and persisting it to config.toml.

Everything here is driven by the :class:`~kokua.config.table.SettingsTable` it is handed: reading the
panel's values back, applying them live, mirroring the display flags onto the channel, and writing them
to their own ``[section].key``. Adding a setting -- Kokua's own or a toolset's -- touches none of this
code, and a setting a toolset owns is applied and persisted exactly the way a core one is.

One thing is not table-driven because it is not per-setting: **a model switch** replaces the client
under every cached agent, so unlike every other setting it cannot be applied to a conversation with a
turn in flight -- see ``switch_model``.

Sampling parameters are not here at all. AIMU owns their precedence chain (client fallbacks, then the
model card's tuned profile, then ``client.default_generate_kwargs``, then the per-call dict), and Kokua
writing the third tier from a ``[generation]`` section duplicated that chain while shadowing the card.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from aimu import aio

from kokua.config import store as config_store
from kokua.config.table import SettingsTable
from kokua.core.build import build_model_client, entry_agent_system_message
from kokua.config import AssistantConfig
from kokua.toolsets.context import LiveState


class SettingsApplier:
    """Reads, applies, and persists the runtime-mutable settings."""

    def __init__(
        self,
        config: AssistantConfig,
        ui,
        gate,
        *,
        table: SettingsTable,
        live_agents: Callable[[], list],
        cached_ids: Callable[[], list[str]],
        agent_for: Callable[[str], object],
        active_agent: Callable[[], object],
        cancel_active_turn: Callable[[], Awaitable[None]],
        state: Callable[[], LiveState],
    ):
        self._config = config
        self._ui = ui
        self._gate = gate
        self._table = table
        self._live_agents = live_agents
        self._cached_ids = cached_ids
        self._agent_for = agent_for
        self._active_agent = active_agent
        self._cancel_active_turn = cancel_active_turn
        # Lazy accessor rather than the object itself: constructed before Assistant.create builds the
        # LiveState this needs (see the same pattern on live_agents/cached_ids/agent_for above), so a
        # closure that reads it at call time is the only shape that works.
        self._state = state
        self._client_factory: Optional[Callable[[str], object]] = None

    # --- the client factory ---------------------------------------------------------------------

    @property
    def client_factory(self) -> Callable[[str], object]:
        return self._client_factory

    def set_client_factory(self, factory: Callable[[str], object]) -> Callable[[str], object]:
        """Record the factory later conversations build their clients from, and return it.

        ``switch_model`` replaces it, which is how a conversation created after a model change gets the
        new model rather than the one the composition root captured.
        """
        self._client_factory = factory
        return factory

    # --- read ------------------------------------------------------------------------------------

    def current(self) -> dict:
        """The effective runtime settings for the web panel: model and prefs."""
        return {s.wire_key: s.read(self._config, self._ui.display_flag) for s in self._table.settings}

    # --- write -----------------------------------------------------------------------------------

    async def apply_and_persist(self, incoming: dict) -> None:
        """Apply a settings-panel change at runtime and write it to config.toml so it survives restarts."""
        applied = await self.apply(self._table.sanitize(incoming))
        self.persist(applied)

    async def apply(self, settings: dict) -> dict:
        """Apply a sanitized settings dict live; returns it, for the caller to persist.

        Everything happens under an exclusive gate hold (waits for in-flight turns to drain, blocks
        new ones). A model that fails to build raises and leaves the running client untouched.
        """
        new_model = settings.get("model")
        current_model = str(self._config.model) if self._config.model else ""
        switching = bool(new_model) and new_model != current_model

        if switching:
            await self._cancel_active_turn()
        async with self._gate.exclusive():
            if switching:
                await self.switch_model(new_model)
            for setting in self._table.settings:
                # Matched on the wire key, not the field: a toolset may own a key called "model", and only
                # the core model setting is the one switch_model handled above.
                if setting.wire_key not in settings or setting.wire_key == "model":
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

        Builds the panel-shaped settings dict for the single change and applies it. Raises if it cannot
        be applied (e.g. an invalid model), so the tool skips persisting a change that did not take.
        """
        applied: dict = {}
        setting = self._table.by_toml(section, key)
        if setting is not None:
            applied[setting.wire_key] = value
        await self.apply(self._table.sanitize(applied))

    async def switch_model(self, model: str) -> None:
        """Rebuild every live agent's client for the new model, preserving each conversation's messages.

        Tools bind the agent (not the client), so they survive; each agent's own messages are restored
        onto its new client. ``aio.client`` is called once per cached agent with the same fixed model
        string, so the first call to fail means every call fails: a bad model raises before any agent
        is swapped, and no partial swap happens in practice. Also updates the client factory so
        conversations built later use the new model.
        """
        system = entry_agent_system_message(self._config, self._state())
        for conversation_id in self._cached_ids():
            agent = self._agent_for(conversation_id)
            new_client = aio.client(model, system=system)
            messages = list(agent.model_client.messages)
            agent.model_client = new_client
            agent.restore(messages)
        self._config.model = model
        # Later-built conversations go through build_model_client (so a since-broken model raises
        # ModelClientError, not a raw ValueError/TypeError). Recomputed per call (not the `system`
        # snapshotted above) so a conversation built after another runtime change (e.g. a config edit)
        # still gets the current message, not this switch's.
        self.set_client_factory(
            lambda conversation_id: build_model_client(
                self._config, entry_agent_system_message(self._config, self._state())
            )
        )
