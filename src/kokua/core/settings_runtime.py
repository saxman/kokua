"""Applying a settings change to a running assistant, and persisting it to config.toml.

Everything here is driven by ``runtime_settings.RUNTIME_SETTINGS``: reading the panel's values back,
applying them live, mirroring the display flags onto the channel, and writing them to their own
``[section].key``. Adding a setting touches none of this code.

Two things are not table-driven because they are not per-setting:

**Generation kwargs** layer over each client's provider built-in defaults. The provider base is
snapshotted before any override is applied, so clearing a field can rebuild from a clean base rather
than leaving the last value stuck on the client.

**A model switch** replaces the client under every cached agent, so unlike every other setting it
cannot be applied to a conversation with a turn in flight -- see ``switch_model``.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from aimu import aio

from kokua.config import store as config_store
from kokua.config import table as runtime_settings
from kokua.core.build import build_model_client, resolve_system_message
from kokua.config import AssistantConfig


def layer_generate_kwargs(client, base: dict, config: AssistantConfig) -> None:
    """Rebuild the client's default generate kwargs in place from the config's generation settings.

    Order (later wins): provider built-in defaults (``base``) < config.toml ``[generation]``. The
    settings panel and update_config write straight into ``config.generation``, so it is the single
    effective layer; a key it never set (e.g. presence_penalty on Anthropic) is never injected.
    """
    kwargs = client.default_generate_kwargs
    kwargs.clear()
    kwargs.update(base)
    kwargs.update(config.generation)


class SettingsApplier:
    """Reads, applies, and persists the runtime-mutable settings."""

    def __init__(
        self,
        config: AssistantConfig,
        ui,
        gate,
        *,
        live_agents: Callable[[], list],
        cached_ids: Callable[[], list[str]],
        agent_for: Callable[[str], object],
        active_agent: Callable[[], object],
        cancel_active_turn: Callable[[], Awaitable[None]],
    ):
        self._config = config
        self._ui = ui
        self._gate = gate
        self._live_agents = live_agents
        self._cached_ids = cached_ids
        self._agent_for = agent_for
        self._active_agent = active_agent
        self._cancel_active_turn = cancel_active_turn
        # The active client's provider built-in generate kwargs, snapshotted before any override is
        # layered on, so a settings change (or a cleared field) can rebuild from a clean base.
        self._base_generate_kwargs: dict = {}
        self._client_factory: Optional[Callable[[str], object]] = None

    # --- the client factory ---------------------------------------------------------------------

    @property
    def client_factory(self) -> Callable[[str], object]:
        return self._client_factory

    def layered_factory(self, raw_factory: Callable[[str], object]) -> Callable[[str], object]:
        """Wrap a raw client factory so every built client carries the same effective generation
        kwargs the active agent has: provider defaults < config.generation.

        Also snapshots the provider built-in defaults (used to re-layer already-live agents on a
        settings change). Every client the factory returns is the current model, so that base is
        stable across conversations.
        """

        def build(conversation_id: str):
            client = raw_factory(conversation_id)
            base = dict(client.default_generate_kwargs)
            self._base_generate_kwargs = base
            layer_generate_kwargs(client, base, self._config)
            return client

        self._client_factory = build
        return build

    # --- read ------------------------------------------------------------------------------------

    def current(self) -> dict:
        """The effective runtime settings for the web panel: model, prefs, generate kwargs."""
        settings = {setting.field: self._read(setting) for setting in runtime_settings.RUNTIME_SETTINGS}
        settings["generate_kwargs"] = dict(self._active_agent().model_client.default_generate_kwargs)
        return settings

    def _read(self, setting: runtime_settings.RuntimeSetting):
        """One setting's effective value: the channel's copy of a mirrored flag wins, since that is
        the one actually consulted while streaming."""
        value = getattr(self._config, setting.field)
        if setting.kind is str:
            return str(value) if value else ""
        if setting.mirror_on_channel:
            return self._ui.display_flag(setting.field, value)
        return value

    # --- write -----------------------------------------------------------------------------------

    async def apply_and_persist(self, incoming: dict) -> None:
        """Apply a settings-panel change at runtime and write it to config.toml so it survives restarts."""
        applied = await self.apply(runtime_settings.sanitize(incoming))
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
            for setting in runtime_settings.RUNTIME_SETTINGS:
                if setting.field not in settings or setting.field == "model":  # model: handled above
                    continue
                setattr(self._config, setting.field, settings[setting.field])
                if setting.mirror_on_channel:
                    self._ui.set_display_flag(setting.field, settings[setting.field])
            self._config.generation = settings["generate_kwargs"]
            for agent in self._live_agents():
                layer_generate_kwargs(agent.model_client, self._base_generate_kwargs, self._config)
        return settings

    def persist(self, settings: dict) -> None:
        """Write an applied settings dict back into config.toml, keeping [generation] in sync.

        A generation kwarg absent from ``settings`` is *unset* rather than left alone, so clearing a
        field in the panel removes it from the file and the provider default takes over again.
        """
        path = self._config.config_path
        for setting in runtime_settings.RUNTIME_SETTINGS:
            if setting.field in settings:
                config_store.set_value(path, setting.section, setting.toml_key, settings[setting.field])
        generate_kwargs = settings["generate_kwargs"]
        for key in runtime_settings.GENERATION_KEYS:
            if key in generate_kwargs:
                config_store.set_value(path, "generation", key, generate_kwargs[key])
            else:
                config_store.unset_value(path, "generation", key)

    async def apply_one(self, section: str, key: str, value) -> None:
        """Apply one hot ``update_config`` change live (no persist; the tool writes disk itself).

        Builds the panel-shaped settings dict for the single change -- always carrying the current
        generation set, so ``apply`` does not wipe it -- and applies it. Raises if it cannot be
        applied (e.g. an invalid model), so the tool skips persisting a change that did not take.
        """
        applied = {"generate_kwargs": dict(self._config.generation)}
        if section == "generation":
            applied["generate_kwargs"][key] = value
        else:
            setting = runtime_settings.by_toml(section, key)
            if setting is not None:
                applied[setting.field] = value
        await self.apply(runtime_settings.sanitize(applied))

    async def switch_model(self, model: str) -> None:
        """Rebuild every live agent's client for the new model, preserving each conversation's messages.

        Tools bind the agent (not the client), so they survive; each agent's own messages are restored
        onto its new client. ``aio.client`` is called once per cached agent with the same fixed model
        string, so the first call to fail means every call fails: a bad model raises before any agent
        is swapped, and no partial swap happens in practice. Also updates the client factory so
        conversations built later use the new model.
        """
        system = resolve_system_message(self._config)
        for conversation_id in self._cached_ids():
            agent = self._agent_for(conversation_id)
            new_client = aio.client(model, system=system)
            messages = list(agent.model_client.messages)
            agent.model_client = new_client
            agent.restore(messages)
        self._config.model = model
        self._base_generate_kwargs = dict(self._active_agent().model_client.default_generate_kwargs)
        # Later-built conversations go through build_model_client (so a since-broken model raises
        # ModelClientError, not a raw ValueError/TypeError) and get the same layered generation kwargs.
        self.layered_factory(lambda conversation_id: build_model_client(self._config))
