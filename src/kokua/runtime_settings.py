"""Persist runtime-mutable model settings so the web UI's changes survive restarts.

The settings panel lets the user change model generation kwargs (temperature, max_tokens, ...),
display prefs (show_thinking / show_tools), and the active model mid-session. This stores that
choice as a single JSON object under the app state dir, mirroring ``mcp_registry.py``. It is the
runtime layer of the config chain: ``provider defaults < config.toml [generation] < this store``.
``config.toml`` is never written by the app -- it stays a hand-authored baseline.

Only generation kwargs the user actually set are persisted (blanks are omitted), so an unsupported
key with a default value is never injected into a provider call (e.g. Anthropic rejects
``presence_penalty`` / ``repetition_penalty``; AIMU drops ``top_p`` / ``top_k`` for thinking models).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

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


def load(path: Path) -> dict:
    """Return the persisted settings object (``{}`` if the file is absent or unreadable)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read generation settings %s; ignoring it.", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return sanitize(data)


def save(path: Path, settings: dict) -> None:
    """Validate and write the whole settings object (creates the parent dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize(settings), indent=2), encoding="utf-8")


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
