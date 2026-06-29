"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch, tmp_path):
    """Point state at a throwaway dir so tests never read or write the real ~/.kokua.

    Tests resolve the default config (``$KOKUA_HOME/config.toml``) and open stores under
    ``$KOKUA_HOME/data``; isolating the root keeps them off a developer's real state.
    """
    monkeypatch.setenv("KOKUA_HOME", str(tmp_path / "kokua-home"))
    monkeypatch.delenv("KOKUA_CONFIG", raising=False)
