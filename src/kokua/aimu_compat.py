"""Startup preflight: confirm the installed AIMU is new enough to run Kokua.

The ``aimu>=0.23.0`` requirement in ``pyproject.toml`` covers a normal install and nothing else. uv
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
check's shape. A name lookup answers for a symbol; a membership check answers for an entry in a published
set whose mere existence proves nothing (``SUBAGENT_SPEC_KEYS`` shipped a release before the
``"generate_kwargs"`` entry Kokua came to depend on, so only its contents dated a checkout); a signature
check answers for a keyword argument no ``getattr`` would notice, which is the shape in force today and
twice before, when it was ``SkillManager(include=...)`` and then ``SkillAgent(script_env=...)``. Checking
one surface is no claim about the others; covering those is the version floor's job.

A capability can also be shaped so that *nothing* can probe it, and AIMU 0.17.0's headline surface is:
the ``"thinking"`` key Kokua writes into an ``agent_types`` spec is a dict key, neither a symbol nor a
parameter, and an AIMU that predates it ignores it in silence rather than raising. What makes that
release probe-able anyway is the other half of it -- closing a spec's keys to a known set, published as
``SUBAGENT_SPEC_KEYS``, which is a symbol *and* is the set the key Kokua depends on belongs to. Where a
release offers no such handle, leave the probe where it is and say so here rather than moving it to
something it could only pretend to check.

AIMU 0.20.0 carries two capabilities Kokua depends on, and only the later one is worth probing. The
first is that a sub-agent spawned with a ``provider:model@base_url`` string reaches that endpoint: a
two-line fix inside a private function, adding no symbol, no parameter, and no set member, whose one
direct tell (which resolver that function reaches for) is exactly the sort of internal a later honest
refactor would change, turning this preflight into a wall in front of a *newer, working* AIMU. The
nearest handle on its path, ``endpoint_kwargs``, is what the probe gripped while that was the newest
surface, and its limit had to be stated out loud: the plumbing landed earlier *within* 0.20.0 than the
spawn fix riding on it, so a sibling parked between those two commits passed and still dropped a
sub-agent's endpoint.

AIMU 0.20.0's second capability closed that gap by arriving later in the same release with a handle of
its own: ``SkillAgent(script_env=...)``, the constructor parameter carrying the ``[email]`` settings and
the downloads folder into the entry agent's skill scripts, without which those scripts run with the
settings missing and no error anywhere. It was the probe until 0.21.0, and it subsumed the endpoint
check rather than trading one narrow window for another, since it landed after both of the commits that
window sat between.

AIMU 0.21.0 was the surface until 0.23.0, and for once the capability and its handle were the same
object. ``resolve_default_text_model`` is what ``AssistantConfig.default_model`` calls to learn the model an
unset ``[assistant].model`` runs on, and it is a plain exported name, so a name lookup asks precisely
the question that matters. The function is old; only its export is new, which is exactly why the floor
and the probe are both needed and neither is redundant here. What Kokua needs is not the behavior but
the *reachability*: AIMU's own docs already told a caller wanting an ``@base_url`` to use "the string
resolver", while it lived in ``aimu.models._internal`` and could not be imported. A sibling predating
the export raises ``AttributeError`` at the first config that declares no model, which is most of them.

Worth recording, since it is the case this module keeps meeting: the capability behind that export has
*no* handle at all. Nothing on a live client retains the string it was constructed from, so a host
cannot ask a built client which endpoint it is talking to. The export is the route around that gap
rather than a fix for it, which is why the probe grips the export and not something on the client.

AIMU 0.23.0 is the current surface, and it is the case this module exists for in its purest form. AIMU
renamed its channel flags ``show_thinking`` / ``show_tools`` to ``stream_thinking`` / ``stream_tools``
and flipped both defaults from ``False`` to ``True``; Kokua deleted its own display settings in the same
change and now constructs both channels bare, relying on that default. Against an older AIMU the bare
construction still *works* -- and silently streams neither reasoning nor tool calls, in a front end
whose whole claim is that the loop is watched rather than inferred. Nothing raises, because after the
rename Kokua no longer reads ``self.show_thinking`` anywhere, so there is not even an ``AttributeError``
left to notice.

The handle is a signature check on ``aio.WebChannel.__init__`` for ``stream_thinking``. Note what it
does and does not establish: the parameter's presence is not itself the capability, which is the
*default value*. It stands in for that because AIMU renamed the arguments and flipped their defaults in
one change, so a checkout carrying the new name carries the new default. The default is directly
inspectable, unusually for this probe, and checking the parameter name is still preferred: it dates the
checkout to the same release without teaching this module a fourth probe shape for one case.
"""

from __future__ import annotations

import importlib
import inspect
from importlib.metadata import PackageNotFoundError, version
from typing import Optional

MINIMUM_AIMU = (0, 23, 0)

# The newest AIMU surface Kokua depends on is a channel that relays reasoning and tool calls by default:
# `stream_thinking` / `stream_tools` replaced `show_thinking` / `show_tools` and default to True, which is
# why Kokua's front ends construct both channels with no flags at all. A signature check, since the
# rename and the default flip are one change: the new argument name is what dates the checkout past both.
# An older AIMU accepts the same bare construction and streams neither phase, with nothing raised
# anywhere. See the module docstring for what this deliberately misses.
_PROBE_MODULE = "aimu.aio"
_PROBE_SYMBOL = "WebChannel"
_PROBE_PARAMETER: Optional[str] = "stream_thinking"
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
