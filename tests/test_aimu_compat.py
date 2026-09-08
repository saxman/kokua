"""The startup preflight that turns a too-old AIMU into an instruction instead of an ImportError."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from kokua import aimu_compat
from kokua.aimu_compat import AimuVersionError, MINIMUM_AIMU, _release, require_aimu

#: A version string that clears the floor, whatever the floor currently is. Derived rather than
#: written out, because these tests exercise the probe rather than the floor: a literal here goes
#: stale on the next bump and fails every one of them for a reason that has nothing to do with what
#: they check. The floor itself is pinned with explicit old versions in the two tests above.
AT_FLOOR = ".".join(str(n) for n in MINIMUM_AIMU)


def test_release_reads_the_numeric_prefix():
    assert _release("0.13.1") == (0, 13, 1)
    assert _release("0.14.0.dev1") == (0, 14, 0)  # a pre-release of a new enough version still passes
    assert _release("1.0") == (1, 0)


def test_the_installed_aimu_satisfies_the_floor():
    """The suite runs against the AIMU Kokua declares, so the preflight must pass here."""
    require_aimu()


def test_an_old_version_names_both_fixes(monkeypatch):
    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.12.0")
    with pytest.raises(AimuVersionError) as excinfo:
        require_aimu()
    message = str(excinfo.value)
    assert "0.12.0" in message and ".".join(str(n) for n in MINIMUM_AIMU) in message
    assert "../aimu" in message  # update the sibling checkout
    assert "--no-sources" in message  # or stop using it


def test_a_missing_aimu_is_reported_as_such(monkeypatch):
    def absent(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(aimu_compat, "version", absent)
    with pytest.raises(AimuVersionError, match="not installed"):
        require_aimu()


def test_a_version_one_release_below_the_floor_is_caught(monkeypatch):
    """The floor moves with the capabilities Kokua uses, so the previous release must fail."""
    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.23.0")
    with pytest.raises(AimuVersionError, match="0.23.0"):
        require_aimu()


def test_a_probe_that_checks_a_set_member_still_works(monkeypatch):
    """The probe follows whatever shape the newest surface has, and a set member is one of the three.

    Exercised here against 0.18.0's `generate_kwargs` member rather than monkeypatching over the live
    probe (a name lookup today), so the branch stays covered independent of which shape currently applies.
    """
    monkeypatch.setattr(aimu_compat, "version", lambda name: AT_FLOOR)
    monkeypatch.setattr(aimu_compat, "_PROBE_CLASS", None)
    monkeypatch.setattr(aimu_compat, "_PROBE_SYMBOL", "SUBAGENT_SPEC_KEYS")
    monkeypatch.setattr(aimu_compat, "_PROBE_MEMBER", "generate_kwargs")
    monkeypatch.setattr(
        aimu_compat.importlib,
        "import_module",
        lambda name: SimpleNamespace(
            __file__="/somewhere/aimu/tools/builtin.py",
            SUBAGENT_SPEC_KEYS=frozenset({"system_message", "tools", "model", "thinking"}),
        ),
    )
    with pytest.raises(AimuVersionError, match="generate_kwargs"):
        require_aimu()


def test_a_probe_that_checks_an_enum_member_still_works(monkeypatch):
    """Two container shapes reach the same check. A frozenset answers ``in`` by value; an enum class
    answers it by value too on 3.12 and raises TypeError on 3.11, so the check reads ``__members__``
    when it is there and the container itself when it is not.
    """
    from enum import Enum

    class _OldPhases(str, Enum):
        THINKING = "thinking"

    monkeypatch.setattr(aimu_compat, "version", lambda name: AT_FLOOR)
    monkeypatch.setattr(aimu_compat, "_PROBE_CLASS", None)
    monkeypatch.setattr(aimu_compat, "_PROBE_SYMBOL", "StreamingContentType")
    monkeypatch.setattr(aimu_compat, "_PROBE_MEMBER", "CONTINUING")
    monkeypatch.setattr(
        aimu_compat.importlib,
        "import_module",
        lambda name: SimpleNamespace(
            __file__="/somewhere/aimu/models/_base/shared.py",
            StreamingContentType=_OldPhases,
        ),
    )
    with pytest.raises(AimuVersionError, match="CONTINUING"):
        require_aimu()


def test_a_new_enough_version_string_over_older_code_is_still_caught(monkeypatch):
    """An editable checkout's version says what its branch claims, not what its code contains, so a
    sibling on an older branch can report the floor while missing the surface behind it."""
    monkeypatch.setattr(aimu_compat, "version", lambda name: AT_FLOOR)
    monkeypatch.setattr(aimu_compat, "_PROBE_CLASS", None)
    monkeypatch.setattr(
        aimu_compat.importlib,
        "import_module",
        lambda name: SimpleNamespace(__file__="/somewhere/aimu/aio/__init__.py"),
    )
    with pytest.raises(AimuVersionError, match=aimu_compat._PROBE_SYMBOL):
        require_aimu()


def test_the_probe_targets_the_release_the_floor_names():
    """The probe has to come from the floor's own release, or a sibling on the previous branch passes it.

    The surface today is ``SessionStore.list_summaries``, the method a session store answers "every
    stored conversation's title and timestamp" with, in one call rather than one read per conversation.
    An older AIMU has no such method (or, on the ABC alone, only the default that still costs a full read
    per conversation), so a sibling missing it is exactly what this preflight exists to catch before the
    sidebar reintroduces the multi-second cost it was built to remove.
    """
    import importlib

    module = importlib.import_module(aimu_compat._PROBE_MODULE)
    holder = getattr(module, aimu_compat._PROBE_CLASS)
    probe = getattr(holder, aimu_compat._PROBE_SYMBOL, None)
    assert probe is not None
    assert aimu_compat._PROBE_MODULE == "aimu.sessions"
    assert aimu_compat._PROBE_CLASS == "SessionStore"
    assert aimu_compat._PROBE_SYMBOL == "list_summaries"
    # A name lookup, because the capability is the method itself: nothing else has to be true of a
    # checkout once it exists on `SessionStore`. See the module docstring for what that leaves to the
    # floor (whether `TinyDBSessionStore` actually overrides the default rather than just inheriting it).
    assert aimu_compat._PROBE_MEMBER is None
    assert aimu_compat._PROBE_PARAMETER is None


def test_the_floor_covers_the_streaming_content_type_the_probe_no_longer_grips():
    """0.28.0's probe surface is 0.29.0's floor now that ``list_summaries`` holds the one probe slot.

    ``StreamingContentType.CONTINUING`` is the phase a streamed driver yields before a round the loop
    itself injected; both producers still branch on it by name, so an AIMU predating it would still
    degrade in silence with no probe to catch it. Pinned directly against the source, the same way
    ``test_the_floor_covers_the_spec_key_the_probe_no_longer_grips`` pins ``max_iterations``: a single
    probe slot cannot hold every capability the floor has come to cover.
    """
    from pathlib import Path

    from aimu.models import StreamingContentType

    from kokua.channels import web
    from kokua.core import subagents

    assert hasattr(StreamingContentType, "CONTINUING")
    for module in (web, subagents):
        assert "StreamingContentType.CONTINUING" in Path(module.__file__).read_text()


def test_the_floor_covers_the_spec_key_the_probe_no_longer_grips():
    """0.28.0 brought two capabilities Kokua depends on, and only one fits the single probe slot.

    ``core/agents.py`` writes ``"max_iterations"`` into an ``agent_types`` spec for an agent declaring its
    own cap, and AIMU validates that closed key set when the spawn tool is built, so an AIMU without the
    entry raises ``ValueError`` at agent construction with or without a probe. That loudness is why the
    probe grips the phase instead, which degrades in silence. This pins the half left to the floor, so a
    sibling that satisfies the version check but lacks the key fails here rather than at the first
    delegation.
    """
    from aimu.tools.builtin import SUBAGENT_SPEC_KEYS

    assert "max_iterations" in SUBAGENT_SPEC_KEYS


def test_the_default_cap_matches_aimus_own():
    """``AssistantConfig.max_iterations`` restates AIMU's ``Agent`` default so four construction sites can
    pass it unconditionally. Two halves of one decision, and nothing else in the suite would notice them
    drifting: if AIMU raises its default, Kokua would quietly pin the old number for every agent.
    """
    from aimu import aio

    from kokua.config.schema import AssistantConfig

    assert AssistantConfig().max_iterations == aio.Agent.max_iterations


def test_the_declared_floor_matches_the_packaged_requirement():
    """``MINIMUM_AIMU`` and ``pyproject.toml``'s specifier have to agree, or one of them is a lie.

    They are two halves of one decision (CLAUDE.md: raise both in the same commit) and neither can
    detect the other drifting. The preflight governs a developer's sibling checkout; the specifier
    governs an installed wheel, where the preflight would pass while pip had been free to resolve
    something older. Nothing else in the suite would notice.
    """
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    specifiers = [d for d in pyproject["project"]["dependencies"] if d.replace("-", "_").startswith("aimu")]
    assert len(specifiers) == 1, f"expected exactly one aimu dependency, found {specifiers}"
    # Compared on the version bound alone: the extras list beside it is a separate decision that
    # moves for its own reasons, and pinning the whole string would make this fail on an extra.
    assert specifiers[0].endswith(f">={AT_FLOOR}"), (
        f"pyproject declares {specifiers[0]!r} but the preflight floor is {AT_FLOOR}"
    )


def test_a_probe_that_checks_a_keyword_argument_still_works(monkeypatch):
    """A keyword argument is one of the three shapes, and was the one in force for 0.25.0's `events`.

    Exercised here against a stand-in rather than the live surface, because the point is the *negative*:
    where a capability is a constructor parameter, a name lookup passes over an older signature that has
    the class and not the argument. ``SkillManager(include=...)`` was this shape for 0.14.0,
    ``SkillAgent(script_env=...)`` for 0.20.0, and ``WebChannel(stream_thinking=...)`` for 0.23.0, so the
    quadruple keeps its historical name.
    """

    class SkillManagerWithoutInclude:
        def __init__(self, skill_dirs=None):
            pass

    monkeypatch.setattr(aimu_compat, "version", lambda name: AT_FLOOR)
    monkeypatch.setattr(aimu_compat, "_PROBE_CLASS", None)
    monkeypatch.setattr(aimu_compat, "_PROBE_SYMBOL", "SkillManager")
    monkeypatch.setattr(aimu_compat, "_PROBE_MEMBER", None)
    monkeypatch.setattr(aimu_compat, "_PROBE_PARAMETER", "include")
    monkeypatch.setattr(
        aimu_compat.importlib,
        "import_module",
        lambda name: SimpleNamespace(
            __file__="/somewhere/aimu/skills/__init__.py", SkillManager=SkillManagerWithoutInclude
        ),
    )
    with pytest.raises(AimuVersionError, match="include"):
        require_aimu()


def test_an_unimportable_aimu_carries_the_import_error(monkeypatch):
    def broken(name):
        raise ImportError("no module named aimu.agents")

    monkeypatch.setattr(aimu_compat, "version", lambda name: AT_FLOOR)
    monkeypatch.setattr(aimu_compat.importlib, "import_module", broken)
    with pytest.raises(AimuVersionError, match="no module named aimu.agents"):
        require_aimu()
