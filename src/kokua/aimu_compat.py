"""Startup preflight: confirm the installed AIMU is new enough to run Kokua.

The ``aimu>=0.29.0`` requirement in ``pyproject.toml`` covers a normal install and nothing else. uv
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
three times before, when it was ``SkillManager(include=...)``, then ``SkillAgent(script_env=...)``, then
``WebChannel(stream_thinking=...)``. Checking one surface is no claim about the others; covering those is
the version floor's job.

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

AIMU 0.23.0 was the surface until 0.25.0, and it is the case this module exists for in its purest form. AIMU
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

AIMU 0.29.0 is the current surface: a membership check on ``StreamingContentType.CONTINUING``, the
phase a streamed driver yields before a round the loop itself injected rather than one the model asked
for. The argument for that shape, and what it does and does not cover, lives in the comment above the
``_PROBE_*`` constants below rather than here, so it is stated once instead of two places that can drift
apart.

AIMU 0.27.0 was the surface until 0.29.0, and it was a plain name lookup: ``ModelRefusalError``, exported
from ``aimu.aio`` alongside ``ModelConnectionError``. The second time this module had that shape (0.21.0's
``resolve_default_text_model`` was the first) and for the same reason: the capability was the exported
name, so a name lookup asked exactly the question that mattered, and nothing else had to be true of a
checkout once the class was importable. Anthropic returns a refusal as HTTP 200 with
``stop_reason: "refusal"`` and no content block, so an AIMU that does not raise for it hands back an
empty string, which inside an agent loop is indistinguishable from a degenerate turn: the continuation
nudge fires and the run spends its iterations being refused again. ``core/turns.py`` branches on this
class at three sites so a declined request reads as declined rather than as a generic failure, and an
AIMU without the name fails at import instead of degrading in silence.

That floor was the first where the capability that *forced* it up and the capability the probe
*gripped* were different, from different releases, and the split is worth understanding because it
is the shape of every future case where a bug fix rather than a feature moves the floor. The floor
moved for **0.26.0**: its tool loop no longer strands an un-dispatched tool call before the forced
wrap-up prompt. Before that fix, exhausting ``max_iterations`` on a turn that had requested tools
left those calls unanswered and then appended the wrap-up's *user* message on top of them, which
Anthropic rejects with ``messages.N: `tool_use` ids were found without `tool_result` blocks
immediately after``. Search-heavy sub-agents hit it routinely, being the shape of run that spends
every round calling tools and so the one still holding a pending call when the cap lands. That fix
offers no handle worth gripping: ``_settle_pending_tools`` is a private method on a private class,
precisely the internal a later honest refactor would rename, which would turn this preflight into a
wall in front of a *newer, working* AIMU, the trap AIMU 0.20.0 documents at length below. So it is
the floor's job, like every capability no name lookup could ever have asked about. 0.27.0's other
half sat in the same position, and is the floor's job to this day: every provider now reports how a
turn ended, so ``TruncatedTurnError`` fires outside Ollama for the first time and
``client.last_stop_reason`` carries the provider's own word for it. That is an attribute on a live
client rather than a module symbol, and Kokua reads it nowhere directly, so the floor covers it too.

AIMU 0.25.0 was the surface until 0.27.0, and the shape was a signature check, the fourth time: a
sub-agent built by ``make_async_subagent_tool`` used to have no way to report its model turns anywhere
but its own return value, so a spawn was invisible to whatever cost accounting the delegator kept. The
release adds an ``events`` parameter that forwards those turns to a sink the caller supplies, and a
critic that builds its own client (see ``workflows.critics.reviewer_agent``) had the identical gap for
the identical reason. Unlike ``SkillManager(include=...)`` and ``SkillAgent(script_env=...)``, where the
parameter carried settings *to* the capability, ``events`` *is* the capability: there is nothing else an
older AIMU is missing once this one argument exists. A name lookup on the module would not catch its
absence, because ``make_async_subagent_tool`` itself predates 0.25.0 and is importable either way; only
its parameters changed. What that probe could not see: whether a spawned worker's *own* spawn tool
forwards ``events`` on to a grandchild it delegates to in turn. The parameter reaching the first hop was
everything that signature check asked, so a recursive delegation could go uncounted one level down
without this module raising anything -- and since the surface has moved on, that gap is the floor's now.

The capability was first published as part of a 0.24.0, but that version number collided: AIMU's own
``main`` branch released a different 0.24.0 first, carrying ``make_command_tool`` (the factory behind
``[compute] command_env_passthrough``) and ``run_command``'s membership in ``builtin.compute``, the
shell tool the ``compute`` toolset now exposes -- none of which is the ``events`` capability. The branch
that added ``events`` rebased past that release and renumbered to 0.25.0, so an installed 0.24.0 (the
real, released one) fails this probe correctly, exactly as an old checkout should, and is not itself a
bug in it.

AIMU 0.24.0 was this probe's surface for the interval it was current on that other branch alone, before
the two merged onto 0.25.0. ``make_command_tool`` was the easy shape for the second time running: the
capability and its handle are the same object, so a name lookup asked precisely the question that
mattered, and the stricter check available there was declined on purpose. A signature check for
``env_passthrough`` would have inspected ``probed.__init__``, right for a class and wrong for a plain
function, so taking it would have taught this probe a fourth shape and dated the checkout no better than
the name did, since the factory and its only parameter shipped in one commit. That release also carried
the reverse of the usual problem: two capabilities Kokua depends on, where the better handle belongs to
the earlier of them. ``make_command_tool`` arrived in the commit that added the tool; ``run_command``'s
membership in ``builtin.compute``, the widening the ``compute`` toolset actually relies on, arrived in
the next one, so a sibling parked between those two commits would have passed that probe and still
handed the ``compute`` toolset no shell tool. Closing that window would have taken a membership check
over a list of *callables*, matching on ``__name__``, a fourth shape a fifteen-minute window did not
earn. AIMU 0.24.0 was the surface until 0.25.0's ``events`` took its place in the merged floor;
``make_command_tool`` and ``run_command`` are now the version floor's job like every other capability
older than the current probe -- as ``events`` itself now is.
"""

from __future__ import annotations

import importlib
import inspect
from importlib.metadata import PackageNotFoundError, version
from typing import Optional

MINIMUM_AIMU = (0, 29, 0)

# The newest AIMU surface Kokua depends on is `StreamingContentType.CONTINUING`, the phase a streamed
# driver yields to mark the boundary before a round the loop itself injected (a continuation nudge, a
# forced wrap-up) rather than one the model asked for. This floor moved *for* that same phase, which
# makes it unlike 0.27.0's: there, the floor moved for 0.26.0's tool-loop fix while the probe gripped
# `ModelRefusalError`, a different capability from a different release. Here the reason for the floor
# and the capability the probe grips are the same one.
#
# The shape is a membership check on an enum class, the third shape this probe has taken and the second
# time membership answered the question (0.18.0's `SUBAGENT_SPEC_KEYS` was the first). `StreamingContentType`
# itself predates this floor by a long way, so its mere presence proves nothing; only whether it carries
# this particular member dates a checkout, the same argument `SUBAGENT_SPEC_KEYS` made one container kind
# over. It still needed the detour below rather than a bare `in`: a frozenset answers `in` by value
# directly, while an enum class answers it by value too on Python 3.12 and raises `TypeError` for a plain
# string on 3.11, which Kokua still supports, and the capability here is a member's *name*, not its
# value.
#
# It is a genuine silent-degradation case, the kind this module exists to catch. Kokua constructs
# nothing differently against an older AIMU: `channels/web.py` and `core/subagents.py` simply never see
# the phase, so no boundary is ever reported anywhere and nothing raises.
#
# What this probe cannot see, and what the floor covers alone: whether *both* streamed drivers emit the
# chunk, and whether *both* injection kinds (a continuation nudge and a forced wrap-up) do. A checkout
# carrying the member but wired to only one driver, or emitting it for only one injection kind, still
# passes this probe, the same kind of gap `events`' recursive passthrough left one level down for its
# own capability.
#
# `make_async_subagent_tool(events=...)` was this probe's surface while 0.25.0 was the floor,
# `make_command_tool` (a name lookup) before that, and `ModelRefusalError` (a name lookup) while 0.27.0
# was the floor; all three are the floor's responsibility now, as everything this probe has ever pointed
# at eventually becomes.
_PROBE_MODULE = "aimu.models"
_PROBE_SYMBOL = "StreamingContentType"
_PROBE_PARAMETER: Optional[str] = None
_PROBE_MEMBER: Optional[str] = "CONTINUING"


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
        f"  Using the sibling checkout (the default here): confirm what it is actually on with "
        f"git -C ../aimu log -1, then check out or pull a branch that reaches {floor} (`main` moves "
        f"independently of this floor and is not guaranteed to have caught up), and run `uv sync "
        f"--all-extras`.\n"
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
    # `in` reads a frozenset directly and an enum class by name through `__members__`. The fallback is
    # the container itself, so the set shape is unchanged; the detour matters because `in` on an enum
    # compares *values* on Python 3.12 and raises TypeError on 3.11, and the capability here is a
    # member's name.
    if _PROBE_MEMBER is not None and _PROBE_MEMBER not in getattr(probed, "__members__", probed):
        raise AimuVersionError(
            _message(
                f"the AIMU at {where} reports version {installed} but its {_PROBE_SYMBOL} has no "
                f"{_PROBE_MEMBER!r} entry, so it predates that release"
            )
        )
    if _PROBE_PARAMETER is None:
        return
    if _PROBE_PARAMETER not in inspect.signature(probed).parameters:
        raise AimuVersionError(
            _message(
                f"the AIMU at {where} reports version {installed} but its {_PROBE_SYMBOL} takes no "
                f"{_PROBE_PARAMETER!r} argument, so it predates that release"
            )
        )
