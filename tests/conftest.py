"""Shared test fixtures."""

from __future__ import annotations

import pytest

#: What an unset ``[assistant].model`` resolves to under test. See ``pin_default_model``.
TEST_DEFAULT_MODEL = "lmstudio:kokua-test-default@http://test-host:1234"


@pytest.fixture(autouse=True)
def pin_default_model(monkeypatch):
    """Pin the model a config with no ``[assistant].model`` resolves to.

    ``AssistantConfig.default_model`` asks AIMU for its default, and AIMU's answer is
    ``AIMU_LANGUAGE_MODEL`` if that is exported and a probe of the local servers if it is not. Both
    reach outside the sandbox: the env-var read loads a ``.env`` found by walking up from the working
    directory (a developer's ``~/devel/.env`` is two levels above this checkout, and did leak into a
    run here), and the probe makes an HTTP call. Neither belongs in a mock-only suite, and either one
    makes an assertion about the default depend on the machine it runs on.

    Pinned rather than stubbed out, so the real resolver still runs and the extended grammar is what
    the suite exercises. The value is deliberately an endpoint-carrying ad-hoc string: that is the
    shape that was silently losing its endpoint, so it is the shape the default should have here.
    A test wanting a different answer sets ``config.model`` or patches the resolver itself.
    """
    monkeypatch.setenv("AIMU_LANGUAGE_MODEL", TEST_DEFAULT_MODEL)


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch, tmp_path):
    """Point state at a throwaway dir holding a real config, so tests never touch the developer's.

    Tests resolve the default config (``$KOKUA_HOME/config.toml``) and open stores under
    ``$KOKUA_HOME/data``; isolating the root keeps them off a developer's real state.

    The config file is seeded with the shipped example rather than left absent, because config.toml is
    required: Kokua will not start without one, so "no config file" is not a state worth simulating by
    default. The few tests that exercise the missing-file error write to a path of their own.
    """
    home = tmp_path / "kokua-home"
    monkeypatch.setenv("KOKUA_HOME", str(home))
    monkeypatch.delenv("KOKUA_CONFIG", raising=False)

    from kokua.config import file as settings

    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(settings.example_text(), encoding="utf-8")


@pytest.fixture(autouse=True)
def no_generated_titles(monkeypatch):
    """Stub out the generated conversation title, since writing one is a model call.

    The suite is mock-only, and this is the one model call no test asks for: it is spawned in the
    background by any first turn (see ``core/titles.py``). Stubbed to "the endpoint had nothing to
    say", which is the documented fallback path, so a test that does not care keeps the truncated
    placeholder ``derive_title`` already gave it. A test about generated titles patches this again
    with the answer it wants.

    Both title calls are stubbed. The whole-conversation one is spawned by a rename rather than by a
    turn, so it is reachable only from a test that asked for it, but stubbing one and not the other
    would leave a real endpoint one ``retitle`` control away.
    """

    async def no_title(model, first_message):
        return None

    monkeypatch.setattr("kokua.core.titles.summarize_title", no_title)
    monkeypatch.setattr("kokua.core.titles.summarize_conversation_title", no_title)
