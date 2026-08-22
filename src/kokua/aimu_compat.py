"""Startup preflight: confirm the installed AIMU is new enough to run Kokua.

The ``aimu>=0.20.0`` requirement in ``pyproject.toml`` covers a normal install and nothing else. uv
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

The probe covers exactly one surface at a time: the newest one Kokua depends on, whose shape decides the
check's shape. A name lookup answers for a symbol; a signature check answers for a keyword argument no
``getattr`` would notice (as ``SkillManager(include=...)`` was); a membership check answers for an entry
in a published set, which is the shape in force today. What Kokua depends on is the ``"generate_kwargs"``
it writes into an ``agent_types`` spec, and although the set holding it (``SUBAGENT_SPEC_KEYS``) is a
symbol, that symbol shipped a release earlier, so its existence proves nothing here and only its contents
do. Checking one surface is no claim about the others; covering those is the version floor's job.

A capability can also be shaped so that *nothing* can probe it, and AIMU 0.17.0's headline surface is:
the ``"thinking"`` key Kokua writes into an ``agent_types`` spec is a dict key, neither a symbol nor a
parameter, and an AIMU that predates it ignores it in silence rather than raising. What makes that
release probe-able anyway is the other half of it -- closing a spec's keys to a known set, published as
``SUBAGENT_SPEC_KEYS``, which is a symbol *and* is the set the key Kokua depends on belongs to. Where a
release offers no such handle, leave the probe where it is and say so here rather than moving it to
something it could only pretend to check.

AIMU 0.20.0 is the sharpest case of a capability with no handle of its own, and the probe's stated limit
is part of what it moved to. What Kokua depends on there is that a sub-agent spawned with a
``provider:model@base_url`` string reaches that endpoint: before it, the async spawn path resolved the
string through a resolver reading only ``provider:model_id``, so an endpoint Kokua's own configuration
reference documents killed every delegation while the entry agent ran on it happily. That fix is two
lines inside a private function. It adds no symbol, no parameter, and no set member, and the one thing
that would detect it directly (which resolver the function reaches for) is exactly the sort of internal
that a later honest refactor would change, which would make this preflight refuse a *newer, working*
AIMU. So the probe checks ``endpoint_kwargs`` instead: new in that release, on the very path the fix
routes through, and stable enough that its absence means the checkout predates the release rather than
merely differs from it. Its limit is worth naming, because it is narrower than usual: the endpoint
plumbing landed earlier *within* 0.20.0 than the spawn fix riding on it, so a sibling parked between the
two commits passes this probe and still drops a sub-agent's endpoint. Only the floor covers that, which
is the ordinary division of labour here, just with less margin than a probe usually leaves.
"""

from __future__ import annotations

import importlib
import inspect
from importlib.metadata import PackageNotFoundError, version
from typing import Optional

MINIMUM_AIMU = (0, 20, 0)

# The newest AIMU surface Kokua depends on is a sub-agent honouring a `provider:model@base_url` model
# string, and `endpoint_kwargs` is the function that turns that endpoint into the provider's own
# constructor kwarg. A plain name lookup: the function is new in the release, so its absence dates the
# checkout, and there is nothing finer to check -- both a member and a parameter check would be probing
# a shape this capability does not have. See the module docstring for what this deliberately misses.
_PROBE_MODULE = "aimu.models.model_client"
_PROBE_SYMBOL = "endpoint_kwargs"
_PROBE_PARAMETER: Optional[str] = None
_PROBE_MEMBER: Optional[str] = None


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
