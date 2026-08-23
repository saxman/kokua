"""The startup preflight that turns a too-old AIMU into an instruction instead of an ImportError."""

from __future__ import annotations

import inspect
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from kokua import aimu_compat
from kokua.aimu_compat import AimuVersionError, MINIMUM_AIMU, _release, require_aimu
from tests.helpers import MockAsyncModelClient


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
    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.19.0")
    with pytest.raises(AimuVersionError, match="0.19.0"):
        require_aimu()


def test_a_probe_that_checks_a_set_member_still_works(monkeypatch):
    """The probe follows whatever shape the newest surface has, and a set member is one of the three.

    Not the shape in force today (0.20.0's surface is a keyword argument), but it was for 0.18.0, and
    the branch has to stay exercised: a published set proves nothing by existing once the set itself
    predates the capability, so only its contents can answer.
    """
    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.20.0")
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
    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.20.0")
    monkeypatch.setattr(
        aimu_compat.importlib,
        "import_module",
        lambda name: SimpleNamespace(__file__="/somewhere/aimu/aio/__init__.py"),
    )
    with pytest.raises(AimuVersionError, match=aimu_compat._PROBE_SYMBOL):
        require_aimu()


def test_the_probe_targets_the_release_the_floor_names():
    """The probe has to come from the floor's own release, or a sibling on the previous branch passes it.

    Pinned because the probe has twice been left behind by a moving floor. The surface today is
    ``SkillAgent(script_env=...)``, which is what carries the ``[email]`` settings and the downloads
    folder into a skill script the entry agent runs: a ``SkillAgent`` builds its own skills server, so
    the ``env`` a host passes to ``build_skills_server`` cannot reach those scripts. Asserting that the
    agent keeps the value, and not just that the parameter is accepted, is what keeps this from
    degrading into a bare existence check.
    """
    import importlib

    module = importlib.import_module(aimu_compat._PROBE_MODULE)
    probe = getattr(module, aimu_compat._PROBE_SYMBOL, None)
    assert probe is not None
    assert aimu_compat._PROBE_PARAMETER in inspect.signature(probe.__init__).parameters
    assert probe(MockAsyncModelClient([]), script_env={"KOKUA_EMAIL_HOST": "smtp.example.com"}).script_env == {
        "KOKUA_EMAIL_HOST": "smtp.example.com"
    }


def test_a_probe_that_checks_a_keyword_argument_still_works(monkeypatch):
    """The probe follows whatever shape the newest surface has, and a keyword argument is the shape in
    force today: a name lookup would pass over an older signature, so the signature is what gets
    checked. Exercised here against ``SkillManager(include=...)``, the first surface of this shape, so
    the branch stays covered by a case that does not move when the real probe does."""

    class SkillManagerWithoutInclude:
        def __init__(self, skill_dirs=None):
            pass

    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.20.0")
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

    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.20.0")
    monkeypatch.setattr(aimu_compat.importlib, "import_module", broken)
    with pytest.raises(AimuVersionError, match="no module named aimu.agents"):
        require_aimu()
