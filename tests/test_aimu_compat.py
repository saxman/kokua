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

    Not the shape in force today (0.24.0's surface is a plain name lookup), but it was for 0.18.0, and
    the branch has to stay exercised: a published set proves nothing by existing once the set itself
    predates the capability, so only its contents can answer.
    """
    monkeypatch.setattr(aimu_compat, "version", lambda name: AT_FLOOR)
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


def test_a_new_enough_version_string_over_older_code_is_still_caught(monkeypatch):
    """An editable checkout's version says what its branch claims, not what its code contains, so a
    sibling on an older branch can report the floor while missing the surface behind it."""
    monkeypatch.setattr(aimu_compat, "version", lambda name: AT_FLOOR)
    monkeypatch.setattr(
        aimu_compat.importlib,
        "import_module",
        lambda name: SimpleNamespace(__file__="/somewhere/aimu/aio/__init__.py"),
    )
    with pytest.raises(AimuVersionError, match=aimu_compat._PROBE_SYMBOL):
        require_aimu()


def test_the_probe_targets_the_release_the_floor_names(monkeypatch):
    """The probe has to come from the floor's own release, or a sibling on the previous branch passes it.

    Pinned because the probe has twice been left behind by a moving floor. The surface today is
    ``make_command_tool``, the factory Kokua's ``compute`` toolset calls to hand a command child the
    environment variables ``[compute] command_env_passthrough`` names.

    Exercising what the factory does, and not merely that it resolves, is what keeps this from
    degrading into the bare existence check the probe's own mechanics already perform. The property
    Kokua depends on is that a name passed as ``env_passthrough`` reaches the child's environment and an
    unnamed one does not, which is the entire reason the setting exists.
    """
    import importlib

    module = importlib.import_module(aimu_compat._PROBE_MODULE)
    probe = getattr(module, aimu_compat._PROBE_SYMBOL, None)
    assert probe is not None
    assert aimu_compat._PROBE_PARAMETER is None, "the capability and its handle are the same object"

    monkeypatch.setenv("KOKUA_AIMU_COMPAT_PROBE", "reached-the-child")
    passthrough_command = probe(env_passthrough=("KOKUA_AIMU_COMPAT_PROBE",))
    assert "reached-the-child" in passthrough_command(command="echo $KOKUA_AIMU_COMPAT_PROBE")
    assert "reached-the-child" not in probe()(command="echo $KOKUA_AIMU_COMPAT_PROBE")


def test_a_probe_that_checks_a_keyword_argument_still_works(monkeypatch):
    """A keyword argument is one of the three shapes, and was the one in force through 0.23.0.

    Not the shape in force today (0.24.0's surface is a plain name lookup, and a name lookup answers it
    exactly), but the branch has to stay exercised: where a capability is a constructor parameter, a
    name lookup passes over an older signature that has the class and not the argument.
    ``SkillManager(include=...)`` was this shape for 0.14.0, ``SkillAgent(script_env=...)`` for 0.20.0,
    and ``WebChannel(stream_thinking=...)`` for 0.23.0, so the double keeps its historical name.
    """

    class SkillManagerWithoutInclude:
        def __init__(self, skill_dirs=None):
            pass

    monkeypatch.setattr(aimu_compat, "version", lambda name: AT_FLOOR)
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
