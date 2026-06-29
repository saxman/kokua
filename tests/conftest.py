"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch, tmp_path):
    """Point state at a throwaway dir so tests never read or migrate the real ~/.mopai.

    ``resolve_config`` runs the legacy-layout migration against ``$MOPAI_HOME``; without this an
    unconfigured run during tests could move a developer's real history/memory into ``data/``.
    """
    monkeypatch.setenv("MOPAI_HOME", str(tmp_path / "mopai-home"))
    monkeypatch.delenv("MOPAI_CONFIG", raising=False)
