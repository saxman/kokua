"""One-off migration: move already-stored oversized sub-agent tool responses into payload files.

``core/subagents.py`` now caps what a sub-agent's tool response keeps inline in a recorded card at
``RESPONSE_PREVIEW_CHARS`` (see that module), spilling anything longer to a content-addressed file
under ``payloads_path`` and leaving a ``/payloads/<sha256>`` reference plus a preview in its place.
That cap applies only to records the app writes from here on; a ``sessions.json`` written before this
change still carries every oversized response inline. On one developer's real store that was 51.9 MB
of a 56.8 MB file, so this script rewrites what is already there to match the new shape.

It is meant to be deleted once every instance of Kokua has run it once: there is no need for a
`kokua` subcommand or a startup migration that would live in the codebase forever for a rewrite that
only ever needs to happen once per installation.

**Run `--dry-run` first.** It reports what would change without touching anything on disk. Once the
numbers look right, run without `--dry-run`; the script copies `sessions.json` to a timestamped
backup before its first write, so the original is always recoverable. The migration is idempotent: an
entry that already carries `response_ref` is left alone, so running the script again after it already
succeeded (or after it was interrupted partway through) changes nothing further.

`--prune-orphans` is a separate, optional pass that runs after the migration and deletes payload
files no session references. It never runs in a dry run, since deleting files is exactly the kind of
change a dry run promises not to make.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aimu.sessions import TinyDBSessionStore

from kokua import payloads
from kokua.config import AssistantConfig
from kokua.core.subagents import RESPONSE_PREVIEW_CHARS

logger = logging.getLogger(__name__)


@dataclass
class Report:
    """What one `migrate()` call did, printed by `main()` and asserted on by the tests."""

    sessions_scanned: int = 0
    sessions_changed: int = 0
    blobs_written: int = 0
    bytes_reclaimed: int = 0
    # Counted, not raised: see `_migrate_append`'s docstring for why a response `save_text` cannot
    # encode is skipped rather than treated as a fatal error. Surfaced here so a user who ran the
    # real migration can tell "everything moved" from "some responses are still inline" instead of
    # discovering the difference only by noticing `sessions.json` is still larger than expected.
    undecodable_skipped: int = 0

    def __str__(self) -> str:
        lines = [
            f"Sessions scanned: {self.sessions_scanned}",
            f"Sessions changed: {self.sessions_changed}",
            f"Tool responses moved to payload files: {self.blobs_written}",
            f"Bytes reclaimed from sessions.json (approximate, before JSON escaping): {self.bytes_reclaimed}",
        ]
        if self.undecodable_skipped:
            lines.append(f"Left inline and unbounded (undecodable text, not an error): {self.undecodable_skipped}")
        return "\n".join(lines)


def _tool_appends(metadata: dict) -> Iterator[dict]:
    """Every `{"kind": "tool", ...}` append dict nested in a session's `metadata["subagent"]`.

    The shape is `{"subagent": {<turn index>: [<event>, ...]}}`, and only some events carry an
    `"append"` at all (a `spawned` event carries only status fields). Walked defensively rather than
    assuming every level is well-formed, because this reads a file nobody has rewritten before and a
    malformed entry from an older Kokua should be skipped, not turned into a crash that leaves the
    rest of the user's history unmigrated.
    """
    subagent = metadata.get("subagent") if isinstance(metadata, dict) else None
    if not isinstance(subagent, dict):
        return
    for events in subagent.values():
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            append = event.get("append")
            if isinstance(append, dict) and append.get("kind") == "tool":
                yield append


def _migrate_append(append: dict, payloads_path: Path, *, dry_run: bool, report: Report) -> bool:
    """Spill one oversized response to a payload file, mutating `append` in place. True if changed.

    Already migrated (`response_ref` present) and small enough to stay inline both return False
    unchanged, which is what makes a session with nothing left to do produce no write at all.

    The encodability check calls `response.encode("utf-8")` directly rather than only discovering
    the failure inside `payloads.save_text`, so a dry run reports the same skip a real run would,
    without ever creating `payloads_path` or writing a byte (the same reason the actual write below
    is skipped whenever `dry_run` is set).
    """
    if "response_ref" in append:
        return False
    response = append.get("response")
    if not isinstance(response, str) or len(response) <= RESPONSE_PREVIEW_CHARS:
        return False
    try:
        response.encode("utf-8")
    except UnicodeEncodeError:
        # Mirrors SubagentReporter._tool_append's own fallback: a response that reached us already
        # decoded with errors="surrogateescape" (a binary file fetched as text is the likely source)
        # carries lone surrogates that strict UTF-8 cannot encode, so save_text would raise. This is
        # a migration of the user's only copy of their history; leaving the one oversized entry
        # inline, exactly as it always was, is the right failure mode, not aborting everything after
        # it.
        logger.warning(
            "A stored sub-agent tool response for %r could not be written to a payload file "
            "(undecodable text); leaving it inline instead.",
            append.get("name"),
        )
        report.undecodable_skipped += 1
        return False

    report.blobs_written += 1
    report.bytes_reclaimed += len(response) - RESPONSE_PREVIEW_CHARS
    if dry_run:
        return False
    reference = payloads.save_text(payloads_path, response)
    append["response"] = response[:RESPONSE_PREVIEW_CHARS]
    append["response_ref"] = reference
    append["response_bytes"] = len(response)
    return True


def _backup(sessions_path: Path) -> Path:
    """Copy `sessions_path` to a timestamped sibling before the first write touches it."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = sessions_path.with_name(f"{sessions_path.name}.backup-{timestamp}")
    shutil.copy2(sessions_path, backup_path)
    return backup_path


def migrate(sessions_path: Path, payloads_path: Path, *, dry_run: bool) -> Report:
    """Move every already-stored oversized tool response out of `sessions_path` into payload files.

    A missing `sessions_path` is treated as nothing to do rather than an error, and is returned
    before `TinyDBSessionStore` ever opens: TinyDB's JSON storage creates the file it is pointed at,
    so opening it even to read would itself be a write a dry run must not make.

    The backup is taken lazily, right before the first real write (the first session whose scan
    found something to change), not unconditionally at the top: a run that changes nothing (an
    already-migrated file, or one with no oversized responses at all) makes no backup, because a
    backup nothing was ever written over is not one this script needs.
    """
    report = Report()
    if not sessions_path.exists():
        return report

    store = TinyDBSessionStore(str(sessions_path))
    backed_up = False
    try:
        for key in store.list_keys():
            report.sessions_scanned += 1
            session = store.get(key)
            changed = False
            for append in _tool_appends(session.metadata):
                if _migrate_append(append, payloads_path, dry_run=dry_run, report=report):
                    changed = True
            if changed and not dry_run:
                if not backed_up:
                    _backup(sessions_path)
                    backed_up = True
                store.save(session)
                report.sessions_changed += 1
    finally:
        store.close()
    return report


def prune_orphans(sessions_path: Path, payloads_path: Path) -> int:
    """Delete every file under `payloads_path` no session's `response_ref` points at. Returns the count.

    Always a pass over the *current* state of `sessions_path`, run after `migrate()` rather than
    folded into it, so a caller who wants only the migration (the common case, and the only one a
    dry run can safely preview) never triggers a deletion as a side effect of asking for a report.
    """
    if not payloads_path.is_dir():
        return 0

    referenced: set[str] = set()
    if sessions_path.exists():
        store = TinyDBSessionStore(str(sessions_path))
        try:
            for key in store.list_keys():
                session = store.get(key)
                for append in _tool_appends(session.metadata):
                    reference = append.get("response_ref")
                    if isinstance(reference, str):
                        path = payloads.reference_to_path(payloads_path, reference)
                        if path is not None:
                            referenced.add(path.name)
        finally:
            store.close()

    pruned = 0
    for path in payloads_path.iterdir():
        if path.is_file() and path.name not in referenced:
            path.unlink()
            pruned += 1
    return pruned


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Move oversized sub-agent tool responses already stored in sessions.json out to "
            "content-addressed payload files, matching what the live recorder now writes for new "
            "ones. One-off and safe to delete once every Kokua installation has run it once."
        )
    )
    parser.add_argument(
        "sessions_file",
        nargs="?",
        type=Path,
        default=None,
        help="Path to sessions.json (default: the configured $KOKUA_HOME/data/sessions.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing, backing up, or creating anything on disk.",
    )
    parser.add_argument(
        "--prune-orphans",
        action="store_true",
        help=(
            "After migrating, delete payload files no session references. Runs as a separate pass "
            "and never in a dry run, since deleting files is not something a dry run may do."
        ),
    )
    args = parser.parse_args()

    config = AssistantConfig()
    sessions_path = args.sessions_file if args.sessions_file is not None else config.sessions_path
    payloads_path = config.payloads_path

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    print(f"Sessions file: {sessions_path}")
    print(f"Payloads directory: {payloads_path}")
    print(f"Mode: {'dry run (nothing will be written)' if args.dry_run else 'live'}")
    print()

    report = migrate(sessions_path, payloads_path, dry_run=args.dry_run)
    print(report)

    if args.prune_orphans:
        if args.dry_run:
            print("\nSkipping --prune-orphans: it never runs in a dry run.")
        else:
            pruned = prune_orphans(sessions_path, payloads_path)
            print(f"\nOrphaned payload files removed: {pruned}")


if __name__ == "__main__":
    main()
