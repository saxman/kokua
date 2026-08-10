"""The declarative table of runtime-mutable settings, and the sanitizer for the settings panel.

A *runtime* setting is one the user can change without restarting: the model, the display flags, the
planning flags, and the generation kwargs. Everything else in ``config.toml`` is startup-only and is
declared in ``settings._SCHEMA`` instead.

This table is the single declaration of that set. It drives, in one place, what used to be repeated
across nine sites: the TOML schema entry, the panel payload sanitizer, the hot-appliable key set that
``update_config`` checks, the settings dict the panel reads back, the live-apply loop, the
channel-mirroring of the display flags, and the persist loop. **Adding a runtime setting is one entry
here plus one field in ``AssistantConfig`` and one input in the web panel** -- ``tests/`` asserts the
first two stay in step.

Generation kwargs are a separate table because they are not one-field-per-setting: they all land in
the single ``AssistantConfig.generation`` dict, and they are range-checked rather than type-flagged.
Only kwargs the user actually set survive sanitizing (blanks are dropped), so an unsupported key with
a default value is never injected into a provider call (e.g. Anthropic rejects ``presence_penalty`` /
``repetition_penalty``; AIMU drops ``top_p`` / ``top_k`` for thinking models).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

_TYPE_LABELS = {bool: "a boolean", str: "a string", int: "an integer"}


@dataclass(frozen=True)
class RuntimeSetting:
    """One setting the web panel and ``update_config`` can change without a restart.

    ``field`` is the ``AssistantConfig`` attribute; ``section`` and ``key`` locate it in config.toml
    (``key`` defaults to ``field``, which is true for every setting today -- it exists so a TOML key
    can be renamed without renaming the attribute). ``mirror_on_channel`` also sets the attribute on
    the channel, which is how the display flags reach ``WebChannel`` while it is streaming.

    There is no ``hot`` flag: being in this table *is* what makes a setting hot.
    """

    field: str
    section: str
    kind: type
    key: Optional[str] = None
    mirror_on_channel: bool = False

    @property
    def toml_key(self) -> str:
        return self.key or self.field

    @property
    def label(self) -> str:
        """The human phrase a ConfigError uses ("must be a boolean")."""
        return _TYPE_LABELS[self.kind]


RUNTIME_SETTINGS: tuple[RuntimeSetting, ...] = (
    RuntimeSetting("model", "assistant", str),
    RuntimeSetting("show_thinking", "display", bool, mirror_on_channel=True),
    RuntimeSetting("show_tools", "display", bool, mirror_on_channel=True),
    RuntimeSetting("plan_review", "planning", bool),
    RuntimeSetting("plan_review_agent", "planning", bool),
    RuntimeSetting("result_review", "planning", bool),
    RuntimeSetting("show_reasoning", "planning", bool),
)


@dataclass(frozen=True)
class GenerationSetting:
    """One model generation kwarg the panel exposes, with its inclusive bounds.

    A ``None`` bound is unbounded on that side. Values of the wrong type or outside the range are
    dropped by ``sanitize`` rather than clamped, so a junk value never reaches a provider call.
    """

    field: str
    kind: type
    lo: Optional[float] = None
    hi: Optional[float] = None


# In panel display order.
GENERATION_SETTINGS: tuple[GenerationSetting, ...] = (
    GenerationSetting("temperature", float, 0.0, 2.0),
    GenerationSetting("max_tokens", int, 1, None),
    GenerationSetting("top_p", float, 0.0, 1.0),
    GenerationSetting("top_k", int, 0, None),
    GenerationSetting("presence_penalty", float, -2.0, 2.0),
    GenerationSetting("repetition_penalty", float, 0.0, 2.0),
)

GENERATION_KEYS: tuple[str, ...] = tuple(setting.field for setting in GENERATION_SETTINGS)

_BY_TOML = {(setting.section, setting.toml_key): setting for setting in RUNTIME_SETTINGS}
_BY_FIELD = {setting.field: setting for setting in RUNTIME_SETTINGS}


def by_toml(section: str, key: str) -> Optional[RuntimeSetting]:
    """The runtime setting a ``[section].key`` names, or None if it is not runtime-mutable."""
    return _BY_TOML.get((section, key))


def by_field(field: str) -> Optional[RuntimeSetting]:
    """The runtime setting an ``AssistantConfig`` field name refers to, or None."""
    return _BY_FIELD.get(field)


def is_hot(section: str, key: str) -> bool:
    """Whether a config change takes effect live. Every ``[generation]`` key is hot."""
    return section == "generation" or (section, key) in _BY_TOML


def _coerce_generation(value: Any, setting: GenerationSetting) -> Optional[Any]:
    """Coerce ``value`` to the setting's type within its bounds; return None to drop it."""
    if value is None or isinstance(value, bool):  # bool is an int subclass; never a numeric kwarg
        return None
    try:
        coerced = setting.kind(value)
    except (TypeError, ValueError):
        return None
    if setting.lo is not None and coerced < setting.lo:
        return None
    if setting.hi is not None and coerced > setting.hi:
        return None
    return coerced


def _coerce_runtime(value: Any, setting: RuntimeSetting) -> Optional[Any]:
    """Coerce a panel value for a runtime setting; return None to drop it."""
    if setting.kind is bool:
        return value if isinstance(value, bool) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def sanitize(raw: dict) -> dict:
    """Keep only known keys, coerce types, drop None / out-of-range / junk.

    Accepts the panel's wire shape (one key per ``RUNTIME_SETTINGS`` field, plus ``generate_kwargs``)
    and returns the same shape with only the values that survived validation. ``generate_kwargs`` is
    always present, holding only the parameters the user actually set.
    """
    result: dict = {}
    for setting in RUNTIME_SETTINGS:
        if setting.field not in raw:
            continue
        coerced = _coerce_runtime(raw[setting.field], setting)
        if coerced is not None:
            result[setting.field] = coerced

    incoming = raw.get("generate_kwargs")
    generate_kwargs: dict = {}
    if isinstance(incoming, dict):
        for setting in GENERATION_SETTINGS:
            if setting.field not in incoming:
                continue
            coerced = _coerce_generation(incoming[setting.field], setting)
            if coerced is not None:
                generate_kwargs[setting.field] = coerced
    result["generate_kwargs"] = generate_kwargs
    return result
