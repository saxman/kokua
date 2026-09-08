"""The one-off migration that moves already-stored oversized tool responses into payload files."""

from __future__ import annotations

import sys

import pytest
from aimu.sessions import Session, TinyDBSessionStore

from kokua import payloads
from kokua.core.subagents import RESPONSE_PREVIEW_CHARS
from scripts.migrate_subagent_payloads import main, migrate, prune_orphans


def _session_with_response(key: str, response: str) -> Session:
    return Session(
        key=key,
        metadata={
            "subagent": {
                "0": [
                    {
                        "id": "worker-1",
                        "append": {
                            "kind": "tool",
                            "name": "get_webpage",
                            "arguments": {"url": "u"},
                            "response": response,
                        },
                    }
                ]
            }
        },
    )


def test_migrate_moves_an_oversized_response_out(tmp_path):
    sessions_path = tmp_path / "sessions.json"
    payloads_path = tmp_path / "payloads"
    store = TinyDBSessionStore(str(sessions_path))
    long_response = "x" * (RESPONSE_PREVIEW_CHARS * 3)
    store.save(_session_with_response("a", long_response))
    store.close()

    report = migrate(sessions_path, payloads_path, dry_run=False)

    store = TinyDBSessionStore(str(sessions_path))
    append = store.get("a").metadata["subagent"]["0"][0]["append"]
    assert append["response"] == long_response[:RESPONSE_PREVIEW_CHARS]
    assert payloads.read_text(payloads_path, append["response_ref"]) == long_response
    assert append["response_bytes"] == len(long_response)
    assert report.blobs_written == 1
    store.close()


def test_migrate_leaves_a_small_response_alone(tmp_path):
    sessions_path = tmp_path / "sessions.json"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "short"))
    store.close()

    migrate(sessions_path, tmp_path / "payloads", dry_run=False)

    store = TinyDBSessionStore(str(sessions_path))
    append = store.get("a").metadata["subagent"]["0"][0]["append"]
    assert append["response"] == "short"
    assert "response_ref" not in append
    store.close()


def test_migrate_is_idempotent(tmp_path):
    sessions_path = tmp_path / "sessions.json"
    payloads_path = tmp_path / "payloads"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()

    migrate(sessions_path, payloads_path, dry_run=False)
    first = sessions_path.read_bytes()
    second_report = migrate(sessions_path, payloads_path, dry_run=False)

    assert sessions_path.read_bytes() == first
    assert second_report.blobs_written == 0
    assert second_report.sessions_changed == 0


def test_dry_run_writes_nothing(tmp_path):
    sessions_path = tmp_path / "sessions.json"
    payloads_path = tmp_path / "payloads"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()
    before = sessions_path.read_bytes()

    report = migrate(sessions_path, payloads_path, dry_run=True)

    assert sessions_path.read_bytes() == before
    assert not payloads_path.exists()
    assert report.blobs_written == 1
    # A dry run's whole purpose is to say what a real run would do, so this counts the session that
    # would change, not the (necessarily zero) number of sessions actually saved.
    assert report.sessions_changed == 1


def test_migrate_backs_up_before_writing(tmp_path):
    """A backup taken after the first save would be worthless exactly when it is needed.

    So this checks more than "a backup file exists": the backup's bytes must match the file as it
    was *before* migration touched it, and must not match the file as it reads *after*, which is
    what rules out a regression that backs up too late.
    """
    sessions_path = tmp_path / "sessions.json"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()
    before = sessions_path.read_bytes()

    migrate(sessions_path, tmp_path / "payloads", dry_run=False)

    backups = list(tmp_path.glob("sessions.json.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    assert backups[0].read_bytes() != sessions_path.read_bytes()


def test_dry_run_never_creates_a_backup(tmp_path):
    """A dry run is a report, not a write: nothing under tmp_path should appear except the input."""
    sessions_path = tmp_path / "sessions.json"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()

    migrate(sessions_path, tmp_path / "payloads", dry_run=True)

    assert list(tmp_path.glob("sessions.json.backup-*")) == []


def test_missing_sessions_file_is_a_no_op(tmp_path):
    """Nothing to migrate is not an error, and must not create the file TinyDB would otherwise open."""
    sessions_path = tmp_path / "sessions.json"
    payloads_path = tmp_path / "payloads"

    report = migrate(sessions_path, payloads_path, dry_run=True)

    assert report.sessions_scanned == 0
    assert not sessions_path.exists()

    report = migrate(sessions_path, payloads_path, dry_run=False)

    assert report.sessions_scanned == 0
    assert not sessions_path.exists()


def test_a_lone_surrogate_is_left_inline_rather_than_crashing_the_run(tmp_path):
    """Mirrors ``SubagentReporter``'s own fallback: a response ``save_text`` cannot encode stays put.

    An oversized response that reached the recorder already decoded with ``errors="surrogateescape"``
    (a binary file fetched as text is the likely source) carries lone surrogates that strict UTF-8
    cannot encode, so ``payloads.save_text`` would raise. The live recorder catches exactly this and
    keeps the response inline and unbounded rather than losing the turn; migrating the user's only copy
    of their history is the one place this failure absolutely must not become an unhandled exception
    that aborts the whole run, orphaning every session not yet visited.
    """
    sessions_path = tmp_path / "sessions.json"
    payloads_path = tmp_path / "payloads"
    store = TinyDBSessionStore(str(sessions_path))
    undecodable = "\udcff" * (RESPONSE_PREVIEW_CHARS * 3)
    good = "y" * (RESPONSE_PREVIEW_CHARS * 3)
    store.save(_session_with_response("bad", undecodable))
    store.save(_session_with_response("good", good))
    store.close()

    report = migrate(sessions_path, payloads_path, dry_run=False)

    store = TinyDBSessionStore(str(sessions_path))
    bad_append = store.get("bad").metadata["subagent"]["0"][0]["append"]
    good_append = store.get("good").metadata["subagent"]["0"][0]["append"]
    store.close()

    assert bad_append["response"] == undecodable
    assert "response_ref" not in bad_append
    assert good_append["response_ref"] is not None
    assert report.blobs_written == 1
    assert report.undecodable_skipped == 1


def test_prune_orphans_removes_files_no_session_references(tmp_path):
    sessions_path = tmp_path / "sessions.json"
    payloads_path = tmp_path / "payloads"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()
    migrate(sessions_path, payloads_path, dry_run=False)

    orphan = payloads_path / ("0" * 64)
    orphan.write_text("nobody references this", encoding="utf-8")

    pruned = prune_orphans(sessions_path, payloads_path)

    assert pruned == 1
    assert not orphan.exists()
    assert len(list(payloads_path.iterdir())) == 1


def test_main_refuses_a_real_run_without_confirmation(tmp_path, monkeypatch, capsys):
    """`migrate()` is safe to call directly (the tests above do), but `main()` is what a person runs
    by hand, alongside whatever else is touching their real sessions.json, so it is the one place
    that has to insist on the acknowledgement before writing anything."""
    sessions_path = tmp_path / "sessions.json"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()
    before = sessions_path.read_bytes()

    monkeypatch.setattr(sys, "argv", ["migrate_subagent_payloads.py", str(sessions_path)])

    with pytest.raises(SystemExit):
        main()

    assert sessions_path.read_bytes() == before
    assert "--confirm-kokua-stopped" in capsys.readouterr().err


def test_main_allows_a_dry_run_without_confirmation(tmp_path, monkeypatch, capsys):
    """The confirmation guards writes, not reports: a dry run is read-only regardless, so asking for
    it there would just be friction with nothing behind it."""
    sessions_path = tmp_path / "sessions.json"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()

    monkeypatch.setattr(sys, "argv", ["migrate_subagent_payloads.py", str(sessions_path), "--dry-run"])

    main()

    assert "Tool responses moved to payload files: 1" in capsys.readouterr().out


def test_main_runs_a_real_migration_once_confirmed(tmp_path, monkeypatch, capsys):
    sessions_path = tmp_path / "sessions.json"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()

    monkeypatch.setattr(sys, "argv", ["migrate_subagent_payloads.py", str(sessions_path), "--confirm-kokua-stopped"])

    main()

    assert "Tool responses moved to payload files: 1" in capsys.readouterr().out
    store = TinyDBSessionStore(str(sessions_path))
    assert "response_ref" in store.get("a").metadata["subagent"]["0"][0]["append"]
    store.close()


def test_main_refuses_prune_orphans_with_a_custom_sessions_file(tmp_path, monkeypatch, capsys):
    """`--prune-orphans` always deletes from the one configured payloads directory. Pointing the
    positional sessions_file argument somewhere else (a copy made to preview the migration, as the
    README suggests) would make "referenced" mean "referenced by the copy," and delete blobs real,
    untouched sessions still use."""
    sessions_path = tmp_path / "sessions.json"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "short"))
    store.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_subagent_payloads.py",
            str(sessions_path),
            "--prune-orphans",
            "--confirm-kokua-stopped",
        ],
    )

    with pytest.raises(SystemExit):
        main()

    assert "--prune-orphans" in capsys.readouterr().err
