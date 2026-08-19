"""The table of runtime-mutable settings, and the sanitizer for the settings panel.

A *runtime* setting is one the user can change without restarting: the model, the display flags, the
planning flags, and whatever a toolset declared as hot. Everything else in
``config.toml`` is startup-only and is declared in ``config.file._STARTUP_SCHEMA`` instead.

The table is the single declaration of that set. It drives, in one place, what used to be repeated
across nine sites: the TOML schema entry, the panel payload sanitizer, the hot-appliable key set that
``update_config`` checks, the settings dict the panel reads back, the live-apply loop, the
channel-mirroring of the display flags, and the persist loop.

A runtime setting may be Kokua's own or a toolset's, and the two are added differently:

- **Kokua's own** is one entry in :data:`CORE_RUNTIME_SETTINGS` plus one field in ``AssistantConfig``
  (plus one input in the web panel) -- ``tests/`` asserts the first two stay in step.
- **A toolset's** is one ``kokua.toolsets.Setting`` on the toolset and nothing else: it lives in
  ``config.toolset_settings[<toolset>]`` rather than in an ``AssistantConfig`` field, and
  ``config.settings_sources`` turns it into the :class:`RuntimeSetting` this table holds.

:class:`SettingsTable` is therefore an instance rather than module state, since which settings exist is
not known until the installed toolsets are.

Sampling parameters are deliberately absent. AIMU resolves them itself, lowest precedence first: the
client's own fallbacks, then the model card's tuned profile, then ``client.default_generate_kwargs``,
then the per-call dict. Kokua used to write the third tier from a ``[generation]`` section, which
duplicated the chain and shadowed the card; it no longer does, so a model's own tuned profile is what
reaches a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

TYPE_LABELS = {bool: "a boolean", str: "a string", int: "an integer"}


@dataclass(frozen=True)
class RuntimeSetting:
    """One setting the web panel and ``update_config`` can change without a restart.

    ``section`` and ``key`` locate it in config.toml (``key`` defaults to ``field``). Where the value
    *lives* depends on ``toolset``: a core setting is an ``AssistantConfig`` attribute named ``field``,
    while a contributed one is ``config.toolset_settings[toolset][key]``. Reading and writing go through
    :meth:`read` and :meth:`write` so no caller has to know which kind it holds.

    ``mirror_on_channel`` also sets the attribute on the channel, which is how the display flags reach
    ``WebChannel`` while it is streaming.

    There is no ``hot`` flag: being in the table *is* what makes a setting hot.
    """

    field: str
    section: str
    kind: type
    key: Optional[str] = None
    mirror_on_channel: bool = False
    toolset: Optional[str] = None

    @property
    def toml_key(self) -> str:
        return self.key or self.field

    @property
    def wire_key(self) -> str:
        """This setting's key in the settings-panel payload.

        A contributed setting is namespaced by its toolset, because two toolsets may reasonably both
        want a ``review_rounds`` and the panel is one flat object. Core settings stay bare so the
        existing payload shape is unchanged.
        """
        return f"{self.toolset}.{self.toml_key}" if self.toolset else self.field

    @property
    def label(self) -> str:
        """The human phrase a ConfigError uses ("must be a boolean")."""
        return TYPE_LABELS[self.kind]

    def read(self, config, display_flag: Callable[[str, Any], Any]) -> Any:
        """This setting's effective value.

        ``display_flag`` resolves a mirrored flag from the channel, whose copy wins: that is the one
        actually consulted while streaming.
        """
        if self.toolset:
            value = config.toolset_settings.get(self.toolset, {}).get(self.toml_key)
        else:
            value = getattr(config, self.field)
        if self.kind is str:
            return str(value) if value else ""
        if self.mirror_on_channel:
            return display_flag(self.field, value)
        return value

    def write(self, config, value: Any, set_display_flag: Callable[[str, Any], None]) -> None:
        """Apply a value to the live config, mirroring it onto the channel when this flag is mirrored."""
        if self.toolset:
            config.toolset_settings.setdefault(self.toolset, {})[self.toml_key] = value
        else:
            setattr(config, self.field, value)
        if self.mirror_on_channel:
            set_display_flag(self.field, value)


# The settings Kokua's own core owns, each backed by an ``AssistantConfig`` field. Whatever the installed
# toolsets declared is appended to these to form the live table (see ``config.settings_sources``).
#
# Short on purpose: a setting belongs here only if the core itself reads it. Everything a capability
# reads is that capability's own declaration, which is where the [planning] flags live (see
# ``toolsets.planning.PLANNING_SETTINGS``).
CORE_RUNTIME_SETTINGS: tuple[RuntimeSetting, ...] = (
    RuntimeSetting("show_thinking", "display", bool, mirror_on_channel=True),
    RuntimeSetting("show_tools", "display", bool, mirror_on_channel=True),
)


def _coerce_runtime(value: Any, setting: RuntimeSetting) -> Optional[Any]:
    """Coerce a panel value for a runtime setting; return None to drop it."""
    if setting.kind is bool:
        return value if isinstance(value, bool) else None
    if setting.kind is int:
        # bool first: it is an int subclass, so a checkbox value would otherwise pass as a number.
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _declared_by(setting: RuntimeSetting) -> str:
    """Which side declared a setting, for the message about two of them claiming one TOML key."""
    return f"toolset {setting.toolset!r}" if setting.toolset else "Kokua's core"


class SettingsTable:
    """Every runtime-mutable setting in this process: Kokua's own plus each toolset's.

    An instance rather than module state because the set is not known until the toolsets are, and the
    alternative (module functions over a core-only default) would be a second source of truth for
    exactly the question this table exists to answer.

    One TOML key may be declared once. Two entries for a ``[section].key`` is a ``ValueError`` here rather
    than a last-wins lookup, because the two would disagree about where the value lives: a core entry
    writes an ``AssistantConfig`` attribute and a contributed one writes ``toolset_settings``, and the
    panel would carry both keys, apply both, and persist the key twice.
    """

    def __init__(self, settings: Sequence[RuntimeSetting]):
        self.settings: tuple[RuntimeSetting, ...] = tuple(settings)
        self._by_toml: dict[tuple[str, str], RuntimeSetting] = {}
        for setting in self.settings:
            location = (setting.section, setting.toml_key)
            existing = self._by_toml.get(location)
            if existing is not None:
                raise ValueError(
                    f"two runtime settings claim [{setting.section}].{setting.toml_key}: "
                    f"{_declared_by(existing)} and {_declared_by(setting)}. One TOML key gets one "
                    "declaration: the panel payload, the live-apply path and the persist path all resolve "
                    "it through this table, so two entries would apply and write the key twice, to "
                    "different destinations, and the loser would then be invisible."
                )
            self._by_toml[location] = setting
        self._by_field = {s.field: s for s in self.settings if not s.toolset}

    def by_toml(self, section: str, key: str) -> Optional[RuntimeSetting]:
        """The runtime setting a ``[section].key`` names, or None if it is not runtime-mutable."""
        return self._by_toml.get((section, key))

    def by_field(self, field: str) -> Optional[RuntimeSetting]:
        """The core runtime setting an ``AssistantConfig`` field name refers to, or None.

        Core only: a contributed setting has no ``AssistantConfig`` attribute to be named by.
        """
        return self._by_field.get(field)

    def is_hot(self, section: str, key: str) -> bool:
        """Whether a config change takes effect live: being in this table is what makes a setting hot."""
        return (section, key) in self._by_toml

    def toml_schema(self) -> dict:
        """These settings as ``config.file`` schema entries: ``(section, key) -> (field, types, label, convert)``.

        The "field" is the wire key rather than an attribute name, since a contributed setting has no
        attribute; ``config.file.load`` routes an override by asking this table what it was.
        """
        return {(s.section, s.toml_key): (s.wire_key, (s.kind,), s.label, None) for s in self.settings}

    def sanitize(self, raw: dict) -> dict:
        """Keep only known keys, coerce types, drop None / junk.

        Accepts the panel's wire shape (one key per setting's ``wire_key``) and returns the same shape
        with only the values that survived validation.
        """
        result: dict = {}
        for setting in self.settings:
            if setting.wire_key not in raw:
                continue
            coerced = _coerce_runtime(raw[setting.wire_key], setting)
            if coerced is not None:
                result[setting.wire_key] = coerced
        return result
