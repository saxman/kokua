"""Shared test fixtures."""

from __future__ import annotations

import pytest


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
