"""Applying a settings change to a running assistant, and persisting it to config.toml.

Everything here is driven by the :class:`~kokua.config.table.SettingsTable` it is handed: reading the
current values back, applying them live, and writing them to their own ``[section].key``. Adding a
setting -- Kokua's own or a toolset's -- touches none of this code, and a setting a toolset owns is
applied and persisted exactly the way a core one is.

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
        gate,
        *,
        table: SettingsTable,
        state: Callable[[], LiveState],
    ):
        self._config = config
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
        return {s.wire_key: s.read(self._config) for s in self._table.settings}

    # --- write -----------------------------------------------------------------------------------

    async def apply_and_persist(self, incoming: dict) -> None:
        """Apply an incoming settings payload at runtime and write it to config.toml so it survives restarts."""
        applied = await self.apply(self._table.sanitize(incoming))
        self.persist(applied)

    async def apply(self, settings: dict) -> dict:
        """Apply a sanitized settings *payload* live; returns it, for the caller to persist.

        Everything happens under an exclusive gate hold (waits for in-flight turns to drain, blocks
        new ones), so no turn reads a half-applied set. The set is what earns the hold: a settings
        panel sends several keys at once, and a turn reading one toolset's new flag beside its old one
        is the tear this excludes. :meth:`apply_one` writes a single key and takes no hold at all, for
        the reason stated there.
        """
        async with self._gate.exclusive():
            self._write(settings)
        return settings

    def _write(self, settings: dict) -> None:
        """Write each named setting into the live config. Synchronous, and that is load-bearing.

        No ``await`` anywhere in this loop, so it runs to completion before any other coroutine is
        scheduled. That is what lets :meth:`apply_one` skip the gate: a reader can see the values from
        before this call or the values from after it, never a mixture.
        """
        for setting in self._table.settings:
            if setting.wire_key not in settings:
                continue
            setting.write(self._config, settings[setting.wire_key])

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

        **Takes no gate hold, unlike :meth:`apply`, and the asymmetry is the point rather than an
        oversight.** Its one caller is ``update_config``, a tool, and a tool runs inside its turn, which
        holds that conversation's reader for its whole length (invariant 1 in ``core/turns.py``).
        Reaching for the exclusive hold from there deadlocks outright: the writer waits for the reader
        count to reach zero, and the count includes the very turn that is awaiting this call. It also
        blocked every *other* conversation while it hung, the gate being writer-preferring, so one tool
        call stopped the whole assistant rather than only its own conversation.

        What makes going without safe is reach, which is how ``core/turn_gate.py`` says the side of an
        operation is decided: this writes one scalar through :meth:`_write`, with no ``await`` between
        reading the table and writing the value, so there is no half-applied state for a turn to
        observe. The multi-key tear ``apply`` excludes cannot arise from a single key. Reaching for the
        writer when nothing needed excluding is the same mistake ``ConversationBook.delete`` once made,
        and the gate's own module docstring names it: it is how an unbounded wait gets built out of
        bounded parts.

        Still ``async``, though nothing here awaits: ``config_store.apply_setting`` takes this as an
        ``Awaitable[None]``, and that seam is what lets the bottom layer apply a value it cannot itself
        reach.
        """
        applied: dict = {}
        setting = self._table.by_toml(section, key)
        if setting is not None:
            applied[setting.wire_key] = value
        self._write(self._table.sanitize(applied))
