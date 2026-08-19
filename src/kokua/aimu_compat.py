"""Startup preflight: confirm the installed AIMU is new enough to run Kokua.

The ``aimu>=0.18.0`` requirement in ``pyproject.toml`` covers a normal install and nothing else. uv
installs a ``[tool.uv.sources]`` path source *without* checking it against the version specifier -- a
declared ``aimu>=0.99.0`` will happily install and lock a 0.13.1 sibling -- so in a development checkout
the pin is not a constraint on the AIMU actually running. This module is what enforces the floor there.

Left unchecked, an out-of-date sibling surfaces as an ``ImportError`` or a ``TypeError`` on some AIMU
call from deep inside the composition root: accurate, but it names a symbol rather than the fix.

Two checks, because neither alone is honest. The version floor catches an old checkout, including the
capabilities that are not importable symbols (AIMU 0.13.1 added the tool result to its web ``tool``
frame, which no ``getattr`` can detect). The capability probe catches an editable checkout whose
declared version already reads new enough while the code behind it predates the release -- the version
string of an editable install says what the branch claims, not what it contains.

The probe targets the newest surface Kokua depends on, which is all any older one needs: a checkout
carrying ``SUBAGENT_SPEC_KEYS`` necessarily carries everything the releases before it added. It takes
whichever shape that surface has -- a name lookup for a symbol, or a signature check when the capability
is a keyword argument that no ``getattr`` would notice (as ``SkillManager(include=...)`` was, before
this).

A capability can also be shaped so that *nothing* can probe it, and AIMU 0.17.0's headline surface is:
the ``"thinking"`` key Kokua writes into an ``agent_types`` spec is a dict key, neither a symbol nor a
parameter, and an AIMU that predates it ignores it in silence rather than raising. What makes that
release probe-able anyway is the other half of it -- closing a spec's keys to a known set, published as
``SUBAGENT_SPEC_KEYS``, which is a symbol *and* is the set the key Kokua depends on belongs to. Where a
release offers no such handle, leave the probe where it is and say so here rather than moving it to
something it could only pretend to check.

A probe takes whichever shape its surface has, and 0.18.0 needed a third: the capability is the
``"generate_kwargs"`` entry in ``SUBAGENT_SPEC_KEYS``, and the set carrying it shipped one release
earlier, so the symbol's existence proves nothing and its contents prove everything. Hence a
membership check, alongside the name lookup and the signature check (as ``SkillManager(include=...)``
was, before this).
"""

from __future__ import annotations

import importlib
import inspect
from importlib.metadata import PackageNotFoundError, version
from typing import Optional

MINIMUM_AIMU = (0, 18, 0)

# The newest AIMU surface Kokua depends on: the `generate_kwargs` member of the closed `agent_types`
# spec key set, which `build_agent_specs` writes for a per-agent generation table. The set is the
# symbol and the key is checked *in* it, because 0.17.0 published the set itself -- so its existence
# no longer proves anything, where its contents do. `None` for the parameter: a member check answers
# this capability, and a signature check would be probing the wrong shape.
_PROBE_MODULE = "aimu.tools.builtin"
_PROBE_SYMBOL = "SUBAGENT_SPEC_KEYS"
_PROBE_PARAMETER: Optional[str] = None
_PROBE_MEMBER: Optional[str] = "generate_kwargs"


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
    where = getattr(module, "__file__", "an unknown path")
    probed = getattr(module, _PROBE_SYMBOL, None)
    if probed is None:
        raise AimuVersionError(
            _message(
                f"the AIMU at {where} reports version {installed} but has no {_PROBE_SYMBOL}, "
                f"so it predates that release"
            )
        )
    if _PROBE_MEMBER is not None and _PROBE_MEMBER not in probed:
        raise AimuVersionError(
            _message(
                f"the AIMU at {where} reports version {installed} but its {_PROBE_SYMBOL} has no "
                f"{_PROBE_MEMBER!r} entry, so it predates that release"
            )
        )
    if _PROBE_PARAMETER is None:
        return
    if _PROBE_PARAMETER not in inspect.signature(probed.__init__).parameters:
        raise AimuVersionError(
            _message(
                f"the AIMU at {where} reports version {installed} but its {_PROBE_SYMBOL} takes no "
                f"{_PROBE_PARAMETER!r} argument, so it predates that release"
            )
        )
