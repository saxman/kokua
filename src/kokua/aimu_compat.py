"""Startup preflight: confirm the installed AIMU is new enough to run Kokua.

The ``aimu>=0.31.0`` requirement in ``pyproject.toml`` covers a normal install and nothing else. uv
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

The probe covers one surface at a time, and until 0.31.0 that was always the *newest* surface Kokua
depends on. It is now the newest surface with a handle worth gripping, which is not the same thing and
is why the rule is written this way: see AIMU 0.31.0 below, the first release where two handles existed
and the later one had to be taken to close a window the earlier one left open.

Whichever surface it is, its shape decides the check's shape. A membership check answers for an entry in
a published set whose mere existence proves nothing, the shape in force today
(``SUBAGENT_SPEC_KEYS``'s ``"compaction"``) and twice before: that same set shipped a release before the
``"generate_kwargs"`` entry Kokua came to depend on, so only its contents dated a checkout, and
``StreamingContentType`` answered the same way for ``CONTINUING``. A name lookup answers for a symbol,
the shape four times before that, for ``resolve_default_text_model``, ``ModelRefusalError``,
``SessionStore.list_summaries``, and ``builtin.get_web_content``; a signature check answers
for a keyword argument no ``getattr`` would notice, the shape four releases running before that:
``SkillManager(include=...)``, then ``SkillAgent(script_env=...)``, then ``WebChannel(stream_thinking=...)``,
then ``make_async_subagent_tool(events=...)``.
Checking one surface is no claim about the others; covering those is the version floor's job.

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

AIMU 0.31.0 is the current surface, and it is the first release where picking the newest handle would
have been the wrong call. Three capabilities in it are Kokua's: ``builtin.select`` and
``builtin.unscoped``, which ``toolsets/fs.py`` and ``toolsets/fs_write.py`` use to partition a group
that gained ``write_file`` and ``edit_file`` (without which every agent declaring ``fs`` silently gains
a write it never declared); ``edit_document`` plus a read-before-replace guard on ``save_document``,
without which a windowed ``read_document`` saved back truncates a user's document to the window it
showed; and ``compaction``, which ``core/agents.py`` writes per worker off a declared
``context_length`` so a long delegation trims its own messages rather than dying in its window.

The probe grips the last of those, and the reason is the release's commit order rather than anything
about the capability. ``select`` and ``unscoped`` arrived in one commit (aimu c44dc8d) and would have
been the obvious handle: a plain name lookup for a function Kokua calls directly. But the
``save_document`` guard landed two commits *later* (c66b08b) and offers no handle at all, being a set of
digests private to one ``make_document_tools`` call, while the windowing that makes an unguarded save
destructive landed *earlier* (aae4a10). A checkout parked between them therefore windows
``read_document``, does not refuse the save, and would have passed a ``select`` probe while silently
cutting a 3,000-line document down to 51 lines. ``compaction`` is in the release's last functional
commit (9354536), so a checkout carrying it carries all three, and taking a later handle to subsume an
earlier window is the reverse of the trade 0.20.0's ``endpoint_kwargs`` had to accept.

The shape is a membership check, and it is the same *set* the probe already gripped once, at 0.18.0, for
a different member. That is the 0.18.0 lesson stated twice: a published set's presence proves nothing
about its contents, and ``SUBAGENT_SPEC_KEYS`` has now twice shipped ahead of an entry Kokua came to
depend on. Worth not confusing with a coincidence one level down: AIMU reads ``"compaction"`` from a
spec by membership too, rather than with ``.get()``, so that a written ``None`` can mean "no compaction
for this specialist" distinctly from an absent key. That is AIMU's reason for its own read, not this
probe's reason for its shape.

What this probe cannot see, and what the floor covers alone: the *contents* of ``builtin.unscoped``.
What Kokua's two ``fs`` toolsets depend on is that the group holds ``write_file`` and ``edit_file``, so
that one selects the writers and the other excludes them, and asking that directly would take a
membership check over a list of *callables* matching on ``__name__`` -- the shape declined at 0.24.0 for
``run_command`` and again at 0.30.0 for ``get_web_content``, declined a third time here, and asserted in
``tests/test_aimu_compat.py`` instead. A checkout whose ``unscoped`` was missing ``write_file`` would
hand Kokua's read-only ``fs`` a writer, which is the failure the split exists to prevent; nothing short
of that declined shape would catch it, and the floor is what stands in its place.

AIMU 0.30.0 was the surface until 0.31.0, and it is the first floor a *rename* has moved. ``get_webpage``
became ``get_web_content``, and the name is not the capability: the old tool never asked what it had
downloaded. It handed ``response.text`` to an HTML stripper, and ``requests`` decodes ``.text`` with
``errors="replace"``, so a PDF behind a URL arrived as megabytes of replacement characters that a tag
stripper passes through almost whole. Kokua hands that group out unchanged (``toolsets/web.py`` is
``list(builtin.web)``) and ``workflows/critics.py`` mounts it for the reviewer, so an older sibling
poisons a model's context with nothing raised anywhere.

The shape is a plain name lookup, the fourth time, and what it declines is the interesting half. What
Kokua hands an agent is the *group*, so the strictly honest question is whether ``builtin.web`` contains
the function, which would take a membership check over a list of *callables* matching on ``__name__``:
a new shape for this module, and the same one declined at 0.24.0 for ``run_command``. It is declined
again here with even less to gain. The rename moved ``def get_web_content`` and the ``web = [...]``
entry for it in one upstream commit, so unlike 0.24.0's fifteen-minute window there is no checkout in
which the name resolves and the group still holds the old tool; ``tests/test_aimu_compat.py`` asserts
the membership instead, as a fact about the release rather than as this module's shape. What the probe
cannot see is the behavior behind the name: a checkout could export ``get_web_content`` and classify
nothing, and its caps (20,000 characters returned, 10 MB downloaded, 200,000 extracted from a PDF) are
invisible to a name lookup. Nothing short of fetching a PDF would establish those, which is not a
preflight's job. ``get_web_content`` is the floor's job now, in its turn.

AIMU 0.29.0 was the surface until 0.30.0, and the probe was a plain name lookup, the third time this
shape had answered (``resolve_default_text_model`` at 0.21.0, ``ModelRefusalError`` at 0.27.0). The
capability is ``SessionStore.list_summaries``: a session store's own answer to "every stored
conversation's title, timestamp, and message count, without its messages". Kokua's sidebar, task
ownership, and startup pointer all used to ask that question through ``ConversationBook.sessions()``,
which cost one whole-file JSON parse per stored conversation to answer it: 4,790 ms on a 56.8 MB
developer store, to draw a list of titles. ``ConversationBook.summaries()`` now calls
``list_summaries()`` instead, and every caller whose question was metadata rather than message text
(``list()``, ``sessions_for_task()``, ``most_recent_or_new()``) moved onto it in the same change;
``sessions()`` survives only for the one caller that genuinely needs message text, the agent's
cross-conversation search.

The handle sits one level deeper than a module attribute: ``list_summaries`` is a method on
``SessionStore``, not a name at module scope, so the probe resolves ``aimu.sessions.SessionStore``
first and looks the symbol up there rather than on ``aimu.sessions`` itself. That is the one structural
difference from ``resolve_default_text_model``'s single-hop lookup; the question the probe asks is still
just "does this name exist", so nothing else has to be true of a checkout once the method is there.
What it cannot see, and what the floor covers alone: ``SessionStore.list_summaries`` has a default
implementation on the ABC itself (read every session, keep its metadata, drop its messages), which is
correct on any store and slow on one where a full read is expensive, and ``TinyDBSessionStore`` overrides
it with a query over TinyDB's own table that never builds a ``Session`` at all. A name lookup on the ABC
is satisfied by the default alone; it cannot tell an override from an inheritance, so a
``TinyDBSessionStore`` that stopped overriding the method, or a future store that never bothered, would
still pass this probe while paying the old per-conversation read in silence. That is the same shape of
gap ``events``' recursive passthrough left one level down for its own capability.

AIMU 0.28.0 was the surface until 0.29.0, and it was a membership check on
``StreamingContentType.CONTINUING``, the phase a streamed driver yields before a round the loop itself
injected rather than one the model asked for. What it did and did not cover is the floor's job now:
``channels/web.py`` and ``core/subagents.py`` still branch on that member by name, so an AIMU predating it
still needs catching, which ``MINIMUM_AIMU`` does in its place.

AIMU 0.27.0 was the surface until 0.28.0, and it was a plain name lookup: ``ModelRefusalError``, exported
from ``aimu.aio`` alongside ``ModelConnectionError``. The second time this module had that shape (0.21.0's
``resolve_default_text_model`` was the first) and for the same reason: the capability was the exported
name, so a name lookup asked exactly the question that mattered, and nothing else had to be true of a
checkout once the class was importable. Anthropic returns a refusal as HTTP 200 with
``stop_reason: "refusal"`` and no content block, so an AIMU that does not raise for it hands back an
empty string, which inside an agent loop is indistinguishable from a degenerate turn: the continuation
nudge fires and the run spends its iterations being refused again. ``core/turns.py`` branches on this
class at three sites so a declined request reads as declined rather than as a generic failure, and an
AIMU without the name fails at import instead of degrading in silence.

0.28.0 carries a second capability Kokua depends on, and the contrast is why the probe grips the phase
rather than it: the ``"max_iterations"`` entry in ``SUBAGENT_SPEC_KEYS``, which ``core/agents.py`` writes
for an agent declaring its own tool-loop cap. Because that key set is closed, an AIMU predating 0.28.0
raises ``ValueError`` on the unknown key rather than ignoring it, so unlike ``stream_thinking`` or
``script_env`` there is no silence to convert into noise. Nor is there a later failure to pull forward:
``_validate_subagent_config`` runs at factory-call time, and Kokua calls that factory from ``wire_agent``,
so the raw ``ValueError`` already lands when a conversation's agent is built, which for the entry agent is
startup. A probe there would buy the *wording*, a message carrying the fix instead of a spec-key
``ValueError`` out of agent construction, and nothing else. The phase above has no such escape hatch, so
it is the surface worth the one slot, and the spec key is the floor's job. Only the per-agent tier ever
needed 0.28.0 in the first place: the factory argument behind ``[assistant].max_iterations`` has been on
both spawn factories since 0.12.0, so the global tier works on an older AIMU and needs neither probe nor
floor.

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

MINIMUM_AIMU = (0, 31, 0)

# The surface is `SUBAGENT_SPEC_KEYS`'s `"compaction"` entry, the spec key `core/agents.py` writes so a
# spawned worker trims its own messages before each model turn instead of filling its window and dying
# in it. Kokua builds that trimmer from a declared `[assistant.generation].context_length`, per worker,
# because `generation_for` has already resolved the per-agent tier by the time the spec is assembled.
#
# Why this key and not `builtin.select`, which lands earlier in the same release and which Kokua calls
# directly: commit order. `select` and `unscoped` arrived together (aimu c44dc8d) and are a plain name
# lookup, but `save_document`'s read-before-replace guard landed two commits later (c66b08b) with no
# handle at all (a digest set private to one `make_document_tools` call), while the `read_document`
# windowing that makes an unguarded save destructive landed earlier (aae4a10). A checkout between those
# two windows a read, does not refuse the save, and would pass a `select` probe while cutting a
# 3,000-line document to 51 lines. `compaction` is in the release's last functional commit (9354536),
# so a checkout carrying it carries `select`, `unscoped`, `edit_document`, and the guard as well.
#
# The shape is a membership check, the third time (`SUBAGENT_SPEC_KEYS`'s `generate_kwargs` at 0.18.0,
# `StreamingContentType.CONTINUING` at 0.28.0) and the second time on this same set, which is the 0.18.0
# lesson restated: a published set's presence proves nothing about its contents, and this one has now
# twice shipped a release ahead of an entry Kokua came to depend on. `_PROBE_CLASS` stays None because
# the set is at module scope; the `in` runs through `__members__` when there is one and against the
# container otherwise, which for a frozenset is the frozenset.
#
# What this probe cannot see, and what the floor covers alone: the *contents* of `builtin.unscoped`.
# Kokua's two fs toolsets depend on that group holding `write_file` and `edit_file`, so that `fs_write`
# selects the writers and `fs` excludes them; an `unscoped` missing one would hand the read-only toolset
# a writer, which is the whole failure the split prevents. Asking it directly needs a membership check
# over a list of *callables* matching on `__name__`, the shape declined at 0.24.0 for `run_command` and
# at 0.30.0 for `get_web_content`, declined again here and asserted in `tests/test_aimu_compat.py`
# instead: a fact about the release, not the preflight's shape.
#
# `get_web_content` (a name lookup) was this probe's surface while 0.30.0 was the floor,
# `SessionStore.list_summaries` (a name lookup) while 0.29.0 was, `StreamingContentType.CONTINUING`
# (a membership check) while 0.28.0 was, `make_async_subagent_tool(events=...)` (a signature check)
# while 0.25.0 was, `make_command_tool` (a name lookup) before that, and `ModelRefusalError` (a name
# lookup) while 0.27.0 was; all six are the version floor's responsibility now, as everything this probe
# has ever pointed at eventually becomes.
_PROBE_MODULE = "aimu.tools.builtin"
_PROBE_CLASS: Optional[str] = None
_PROBE_SYMBOL = "SUBAGENT_SPEC_KEYS"
_PROBE_PARAMETER: Optional[str] = None
_PROBE_MEMBER: Optional[str] = "compaction"


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
    # Most probes look `_PROBE_SYMBOL` up directly on the module. `list_summaries` sits one level
    # deeper, on `SessionStore`, so `_PROBE_CLASS` names that intermediate holder when the symbol lives
    # on a class rather than the module itself; every earlier probe leaves it unset and this runs exactly
    # as before, straight off the module.
    holder = module
    if _PROBE_CLASS is not None:
        holder = getattr(module, _PROBE_CLASS, None)
    probed = getattr(holder, _PROBE_SYMBOL, None) if holder is not None else None
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
