"""Startup preflight: confirm the installed AIMU is new enough to run Kokua.

The ``aimu>=0.13.1`` requirement in ``pyproject.toml`` covers a normal install and nothing else. uv
installs a ``[tool.uv.sources]`` path source *without* checking it against the version specifier -- a
declared ``aimu>=0.99.0`` will happily install and lock a 0.13.1 sibling -- so in a development checkout
the pin is not a constraint on the AIMU actually running. This module is what enforces the floor there.

Left unchecked, an out-of-date sibling surfaces as an ``ImportError`` on some AIMU symbol from deep
inside ``core/build.py``: accurate, but it names a symbol rather than the fix.

Two checks, because neither alone is honest. The version floor catches an old checkout, including the
capabilities that are not importable symbols (AIMU 0.13.1 added the tool result to its web ``tool``
frame, which no ``getattr`` can detect). The capability probe catches an editable checkout whose
declared version already reads ``0.13.1`` while the code behind it predates the release -- the version
string of an editable install says what the branch claims, not what it contains.
"""

from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError, version

MINIMUM_AIMU = (0, 13, 1)

# One symbol from AIMU's newest surface that Kokua imports unconditionally (see `core/build.py`).
# Probed by name rather than imported, so a miss is a clean False instead of an ImportError here.
_PROBE_MODULE = "aimu.aio.tools.builtin"
_PROBE_SYMBOL = "SubagentObserver"


class AimuVersionError(RuntimeError):
    """The installed AIMU is too old for this Kokua. Carries the fix, for a front end to print."""


def _release(text: str) -> tuple[int, ...]:
    """The leading numeric release segment of a version string, so ``0.14.0.dev1`` reads as (0, 14, 0)."""
    parts: list[int] = []
    for piece in text.split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _message(problem: str) -> str:
    floor = ".".join(str(n) for n in MINIMUM_AIMU)
    return (
        f"Kokua needs AIMU {floor} or newer, but {problem}.\n"
        f"  Using the sibling checkout (the default here): update it -- "
        f"git -C ../aimu checkout main && git -C ../aimu pull, then `uv sync --all-extras`.\n"
        f"  Not developing AIMU? `uv sync --all-extras --no-sources` installs AIMU {floor} from PyPI "
        f"and ignores the sibling entirely."
    )


def require_aimu() -> None:
    """Raise :class:`AimuVersionError` if the installed AIMU predates what Kokua needs."""
    try:
        installed = version("aimu")
    except PackageNotFoundError:
        raise AimuVersionError(_message("AIMU is not installed")) from None

    if _release(installed) < MINIMUM_AIMU:
        raise AimuVersionError(_message(f"version {installed} is installed"))

    try:
        module = importlib.import_module(_PROBE_MODULE)
    except ImportError as e:
        raise AimuVersionError(_message(f"{_PROBE_MODULE} could not be imported ({e})")) from None
    if not hasattr(module, _PROBE_SYMBOL):
        raise AimuVersionError(
            _message(
                f"the AIMU at {getattr(module, '__file__', 'an unknown path')} reports version {installed} "
                f"but has no {_PROBE_SYMBOL}, so it predates that release"
            )
        )
