"""The startup preflight that turns a too-old AIMU into an instruction instead of an ImportError."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from kokua import aimu_compat
from kokua.aimu_compat import AimuVersionError, MINIMUM_AIMU, _release, require_aimu


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
    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.17.0")
    with pytest.raises(AimuVersionError, match="0.17.0"):
        require_aimu()


def test_a_spec_key_set_without_the_depended_on_key_is_caught(monkeypatch):
    """0.17.0 published the set itself, so its mere existence no longer proves the capability."""
    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.18.0")
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
    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.18.0")
    monkeypatch.setattr(
        aimu_compat.importlib,
        "import_module",
        lambda name: SimpleNamespace(__file__="/somewhere/aimu/tools/builtin.py"),
    )
    with pytest.raises(AimuVersionError, match=aimu_compat._PROBE_SYMBOL):
        require_aimu()


def test_the_probe_targets_the_release_the_floor_names():
    """The probe has to come from the floor's own release, or a sibling on the previous branch passes it.

    Pinned because the previous probe (``aio.ContextOverflowError``) predated the floor: AIMU 0.17.0's
    contribution to Kokua is the ``thinking`` key in an ``agent_types`` spec, and a dict key is invisible
    to both a name lookup and a signature check. The same release closing that spec to a known set is
    what gave it a detectable symbol, so the probe could move forward again. 0.18.0 moves the floor once
    more, to the ``generate_kwargs`` member of that same set, which is what this test now pins.
    """
    import importlib

    module = importlib.import_module(aimu_compat._PROBE_MODULE)
    assert getattr(module, aimu_compat._PROBE_SYMBOL, None) is not None
    # The spec key Kokua actually depends on is inside this set, so the symbol is not a bare proxy.
    assert "generate_kwargs" in getattr(module, aimu_compat._PROBE_SYMBOL)


def test_a_probe_that_checks_a_keyword_argument_still_works(monkeypatch):
    """The probe follows whatever shape the newest surface has. When that is a keyword argument, a
    name lookup would pass over an older signature, so the signature is what gets checked."""

    class SkillManagerWithoutInclude:
        def __init__(self, skill_dirs=None):
            pass

    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.18.0")
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

    monkeypatch.setattr(aimu_compat, "version", lambda name: "0.18.0")
    monkeypatch.setattr(aimu_compat.importlib, "import_module", broken)
    with pytest.raises(AimuVersionError, match="no module named aimu.agents"):
        require_aimu()
