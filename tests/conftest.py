"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch, tmp_path):
    """Point state at a throwaway dir so tests never read or write the real ~/.mopai.

    Tests resolve the default config (``$MOPAI_HOME/config.toml``) and open stores under
    ``$MOPAI_HOME/data``; isolating the root keeps them off a developer's real state.
    """
    monkeypatch.setenv("MOPAI_HOME", str(tmp_path / "mopai-home"))
    monkeypatch.delenv("MOPAI_CONFIG", raising=False)
