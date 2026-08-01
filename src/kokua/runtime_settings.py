"""Validate the runtime-mutable model settings the web settings panel sends.

The settings panel lets the user change model generation kwargs (temperature, max_tokens, ...),
display prefs (show_thinking / show_tools), and the active model mid-session. ``sanitize`` turns the
panel's wire payload into a clean, type-checked, range-checked settings dict; the assistant applies it
live and persists it into ``config.toml`` (there is no longer a separate JSON store).

Only generation kwargs the user actually set survive (blanks are dropped), so an unsupported key with
a default value is never injected into a provider call (e.g. Anthropic rejects ``presence_penalty`` /
``repetition_penalty``; AIMU drops ``top_p`` / ``top_k`` for thinking models).
"""

from __future__ import annotations

from typing import Any, Optional

# Generation kwargs the panel exposes, in display order. Each maps to a coercer and an inclusive
# (min, max) range; values outside the range or of the wrong type are dropped by ``sanitize``.
# ``None`` bound means unbounded on that side.
_GENERATION_SPEC: dict[str, tuple[type, Optional[float], Optional[float]]] = {
    "temperature": (float, 0.0, 2.0),
    "max_tokens": (int, 1, None),
    "top_p": (float, 0.0, 1.0),
    "top_k": (int, 0, None),
    "presence_penalty": (float, -2.0, 2.0),
    "repetition_penalty": (float, 0.0, 2.0),
}

GENERATION_KEYS = tuple(_GENERATION_SPEC)


def _coerce(value: Any, kind: type, lo: Optional[float], hi: Optional[float]) -> Optional[Any]:
    """Coerce ``value`` to ``kind`` within [lo, hi]; return None to drop it. Rejects bools."""
    if value is None or isinstance(value, bool):  # bool is an int subclass; never a numeric kwarg
        return None
    try:
        coerced = kind(value)
    except (TypeError, ValueError):
        return None
    if lo is not None and coerced < lo:
        return None
    if hi is not None and coerced > hi:
        return None
    return coerced


def sanitize(raw: dict) -> dict:
    """Keep only known keys, coerce types, drop None / out-of-range / junk.

    Accepts the persisted / wire shape ``{"model", "show_thinking", "show_tools", "plan_review",
    "generate_kwargs"}`` and returns the same shape with only the keys that survived
    validation. ``generate_kwargs`` holds only the parameters the user actually set.
    """
    result: dict = {}

    model = raw.get("model")
    if isinstance(model, str) and model.strip():
        result["model"] = model.strip()

    for flag in (
        "show_thinking",
        "show_tools",
        "plan_review",
        "plan_review_agent",
        "result_review",
        "show_reasoning",
    ):
        if isinstance(raw.get(flag), bool):
            result[flag] = raw[flag]

    incoming = raw.get("generate_kwargs")
    generate_kwargs: dict = {}
    if isinstance(incoming, dict):
        for key, (kind, lo, hi) in _GENERATION_SPEC.items():
            if key not in incoming:
                continue
            coerced = _coerce(incoming[key], kind, lo, hi)
            if coerced is not None:
                generate_kwargs[key] = coerced
    result["generate_kwargs"] = generate_kwargs
    return result
