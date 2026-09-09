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
                            "name": "get_web_content",
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


# --- the four combinations of (sessions_file: default or custom) x (--payloads-dir: default or
# custom), for a real (non-dry-run) migration ---------------------------------------------------
#
# Only the two matched combinations (both default, or both overridden) are safe: overriding either
# one alone writes response_ref values that point at a payloads directory the app, reading its own
# configured sessions file, never looks in (or leaves migrated blobs in a directory nothing reads
# from). Both single-override combinations must refuse before writing anything; both matched
# combinations must proceed. `--dry-run` is exempt from all four, since it never writes.
#
#   sessions_file  --payloads-dir   outcome
#   -------------  ---------------  -------------------------------------------------
#   default        default          proceeds (test_main_runs_a_real_migration_with_both_defaulted)
#   default        custom           refused  (test_main_refuses_a_custom_payloads_dir_alone)
#   custom         default          refused  (test_main_refuses_a_real_run_against_an_unconfigured_sessions_file_without_payloads_dir)
#   custom         custom           proceeds (test_main_runs_a_real_migration_once_confirmed)


def test_main_runs_a_real_migration_with_both_defaulted(tmp_path, monkeypatch, capsys):
    """Combination 1: neither sessions_file nor --payloads-dir given. The common case, and the one
    every other test in this pair relies on being unaffected by the new pairing guard."""
    home = tmp_path / "kokua-home"
    monkeypatch.setenv("KOKUA_HOME", str(home))
    from kokua.config import file as settings

    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(settings.example_text(), encoding="utf-8")

    sessions_path = home / "data" / "sessions.json"
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()

    monkeypatch.setattr(sys, "argv", ["migrate_subagent_payloads.py", "--confirm-kokua-stopped"])

    main()

    assert "Tool responses moved to payload files: 1" in capsys.readouterr().out
    store = TinyDBSessionStore(str(sessions_path))
    assert "response_ref" in store.get("a").metadata["subagent"]["0"][0]["append"]
    store.close()
    assert any((home / "data" / "payloads").iterdir())


def test_main_refuses_a_custom_payloads_dir_alone(tmp_path, monkeypatch, capsys):
    """Combination 2: the mirror of the case F2 originally named. sessions_file is left at its
    default (the user's real sessions.json), but --payloads-dir points somewhere else. Left
    unrefused, a real run would rewrite the user's actual sessions.json with response_ref values
    pointing at blobs that exist only in the custom directory, while config.payloads_path (where the
    running app looks) is never even created: every migrated card then reads "could not load full
    response" forever, and the original text survives only in the timestamped backup. That is the
    same failure the sessions_file-alone case guards against, reached through the other argument."""
    home = tmp_path / "kokua-home"
    monkeypatch.setenv("KOKUA_HOME", str(home))
    from kokua.config import file as settings

    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(settings.example_text(), encoding="utf-8")

    sessions_path = home / "data" / "sessions.json"
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()
    before = sessions_path.read_bytes()

    custom_payloads = tmp_path / "elsewhere-payloads"
    monkeypatch.setattr(
        sys,
        "argv",
        ["migrate_subagent_payloads.py", "--payloads-dir", str(custom_payloads), "--confirm-kokua-stopped"],
    )

    with pytest.raises(SystemExit):
        main()

    assert sessions_path.read_bytes() == before, "the real sessions.json must not be touched"
    assert not custom_payloads.exists(), "no blob may be written before the refusal"
    err = capsys.readouterr().err
    assert "--payloads-dir" in err
    assert "sessions_file" in err


def test_main_runs_a_real_migration_once_confirmed(tmp_path, monkeypatch, capsys):
    """Combination 4: both overridden together, which is the other safe pairing."""
    sessions_path = tmp_path / "sessions.json"
    payloads_path = tmp_path / "payloads"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()

    # sessions_path is not the configured sessions.json (isolate_state points that at its own
    # tmp_path/kokua-home/data/sessions.json), so a real run also has to say where the payloads
    # belong; see test_main_refuses_a_real_run_against_an_unconfigured_sessions_file below for what
    # happens without it.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_subagent_payloads.py",
            str(sessions_path),
            "--payloads-dir",
            str(payloads_path),
            "--confirm-kokua-stopped",
        ],
    )

    main()

    assert "Tool responses moved to payload files: 1" in capsys.readouterr().out
    store = TinyDBSessionStore(str(sessions_path))
    assert "response_ref" in store.get("a").metadata["subagent"]["0"][0]["append"]
    store.close()
    assert any(payloads_path.iterdir()), "the blob must land under the directory --payloads-dir named"


def test_main_refuses_a_real_run_against_an_unconfigured_sessions_file_without_payloads_dir(
    tmp_path, monkeypatch, capsys
):
    """Combination 3. A real migration against a sessions_file that is not the one Kokua is
    configured to read, with no --payloads-dir to say where the blobs should actually go. Left
    unrefused, response_ref would point at the *configured* payloads directory
    while the file being migrated is a different one: every migrated card would show "could not
    load full response" forever, with the original text surviving only in the timestamped backup."""
    sessions_path = tmp_path / "sessions.json"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()
    before = sessions_path.read_bytes()

    monkeypatch.setattr(sys, "argv", ["migrate_subagent_payloads.py", str(sessions_path), "--confirm-kokua-stopped"])

    with pytest.raises(SystemExit):
        main()

    assert sessions_path.read_bytes() == before
    assert "--payloads-dir" in capsys.readouterr().err


def test_main_allows_a_dry_run_against_an_unconfigured_sessions_file_without_payloads_dir(
    tmp_path, monkeypatch, capsys
):
    """The refusal above guards a real write; a dry run never writes anything, so a preview copy
    needs no --payloads-dir to be inspected."""
    sessions_path = tmp_path / "sessions.json"
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()

    monkeypatch.setattr(sys, "argv", ["migrate_subagent_payloads.py", str(sessions_path), "--dry-run"])

    main()

    assert "Tool responses moved to payload files: 1" in capsys.readouterr().out


def test_main_honors_a_configured_data_dir(tmp_path, monkeypatch, capsys):
    """`AssistantConfig()` built bare (the previous shape of this script) ignores `[paths] data_dir`
    entirely, so a user who set it would have this script scan and write against a directory the
    running app never uses. Loading config.toml the way `kokua` itself does is what makes the
    default `sessions_file`/payloads directory follow that setting."""
    from kokua.config import file as settings

    home = tmp_path / "kokua-home"
    custom_data = tmp_path / "custom-data"
    home.mkdir(parents=True, exist_ok=True)
    text = settings.example_text().replace('# data_dir = "/path/to/kokua-data"', f'data_dir = "{custom_data}"')
    (home / "config.toml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("KOKUA_HOME", str(home))

    sessions_path = custom_data / "sessions.json"
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    store = TinyDBSessionStore(str(sessions_path))
    store.save(_session_with_response("a", "x" * (RESPONSE_PREVIEW_CHARS * 3)))
    store.close()

    # No sessions_file argument, so main() must resolve the configured one, which only lands under
    # custom_data if it actually parsed [paths] data_dir rather than falling back to the bare default.
    monkeypatch.setattr(sys, "argv", ["migrate_subagent_payloads.py", "--dry-run"])

    main()

    out = capsys.readouterr().out
    assert str(sessions_path) in out
    assert str(custom_data / "payloads") in out
    assert "Tool responses moved to payload files: 1" in out


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
