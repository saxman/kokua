"""One-off migration: move already-stored oversized sub-agent tool responses into payload files.

``core/subagents.py`` now caps what a sub-agent's tool response keeps inline in a recorded card at
``RESPONSE_PREVIEW_CHARS`` (see that module), spilling anything longer to a content-addressed file
under ``payloads_path`` and leaving a ``/payloads/<sha256>`` reference plus a preview in its place.
That cap applies only to records the app writes from here on; a ``sessions.json`` written before this
change still carries every oversized response inline. On one developer's real store that was 51.9 MB
of a 56.8 MB file, so this script rewrites what is already there to match the new shape.

It is meant to be deleted once every instance of Kokua has run it once: there is no need for a
`kokua` subcommand or a startup migration that would live in the codebase forever for a rewrite that
only ever needs to happen once per installation. Deleting it, though, also removes the only pass that
ever prunes an orphaned payload file (`--prune-orphans`, below); if that matters to an installation,
keep the script around rather than reaching for `rm` the moment every instance has migrated once.

**Run `--dry-run` first.** It reports what would change without touching anything on disk. Once the
numbers look right, run without `--dry-run`; the script copies `sessions.json` to a timestamped
backup before its first write, so the original is always recoverable. The migration is idempotent: an
entry that already carries `response_ref` is left alone, so running the script again after it already
succeeded (or after it was interrupted partway through) changes nothing further.

**Stop Kokua before any real run, including `--prune-orphans`.** This script has no reliable way to
detect a running instance from outside it, so it asks for `--confirm-kokua-stopped` instead of
guessing, and refuses to write without it. Running alongside a live Kokua can lose data in two ways
that would not show up anywhere, including in the backup: a payload the live recorder just wrote can
be deleted by `--prune-orphans` before the session referencing it is next saved, since that window's
blob exists on disk with nothing yet pointing at it; and `sessions.json` is rewritten whole on every
save, not written to a new file and swapped in, so a save this script makes and a save Kokua makes at
close to the same time can each silently overwrite the other's.

`--prune-orphans` is a separate, optional pass that runs after the migration and deletes payload
files no session references. It never runs in a dry run, since deleting files is exactly the kind of
change a dry run promises not to make, and it refuses a custom sessions-file argument, since it always
prunes against the one configured payloads directory and a sessions file that is not the real one
(a copy made to preview the migration, say) would make it delete blobs other, real sessions still use.

**The configured `sessions.json` and payloads directory are resolved the way `kokua` itself resolves
them,** by loading `config.toml` rather than only the built-in defaults, so `[paths] data_dir` is
honored when a user has set it (see `_load_config`). A `sessions_file` argument pointing at a copy
made to preview the migration is still fine for `--dry-run`; for a real run, pass `--payloads-dir` too
in that case, or the blobs would be written to the configured directory while the copy being migrated
is not the file the app ever reads them alongside.
"""

from __future__ import annotations

import argparse
import json
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

# Every sha256 hex digest is this long regardless of what it hashes, which is what lets a dry run
# measure the byte cost of a `response_ref` field without ever computing or writing a real one.
_SHA256_HEX_LENGTH = 64


@dataclass
class Report:
    """What one `migrate()` call did, printed by `main()` and asserted on by the tests."""

    sessions_scanned: int = 0
    # Counts a session with at least one migrated entry regardless of `dry_run`: a dry run's whole
    # purpose is to say what a real run would do, so this is the "would change" count in that mode
    # and the "did change" count in the other, and a reader should not have to know which.
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
            f"Bytes reclaimed from sessions.json, as it is actually serialized to disk: {self.bytes_reclaimed}",
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


def _dry_run_reference_placeholder() -> str:
    """A reference of the exact length `payloads.save_text` would return, without writing anything.

    A dry run must not touch `payloads_path` at all, but the report still needs to account for the
    `response_ref` field's contribution to the file's serialized size, and that contribution depends
    only on the reference's length, never its content.
    """
    return payloads.ROUTE_PREFIX + "0" * _SHA256_HEX_LENGTH


def _migrate_append(append: dict, payloads_path: Path, *, dry_run: bool, report: Report) -> bool:
    """Spill one oversized response to a payload file, mutating `append` in place. True if migrated.

    Mutates `append` even during a dry run. Nothing here is persisted unless the caller goes on to
    call `store.save()`, so an in-memory-only mutation is invisible on disk, and it is what lets a
    dry run measure the exact same JSON-encoded byte delta a real run would produce, rather than a
    rough estimate of it.

    Already migrated (`response_ref` present) and small enough to stay inline both return False
    unchanged, which is what makes a session with nothing left to do produce no write at all.

    The encodability check calls `response.encode("utf-8")` directly rather than only discovering
    the failure inside `payloads.save_text`, so a dry run reports the same skip a real run would,
    without ever creating `payloads_path` or writing a byte.
    """
    if "response_ref" in append:
        return False
    response = append.get("response")
    if not isinstance(response, str) or len(response) <= RESPONSE_PREVIEW_CHARS:
        return False
    try:
        response.encode("utf-8")
    except UnicodeEncodeError:
        # The same undecodable-text case SubagentReporter._tool_append falls back on: a response
        # that reached us already decoded with errors="surrogateescape" (a binary file fetched as
        # text is the likely source) carries lone surrogates that strict UTF-8 cannot encode, so
        # save_text would raise. Deliberately narrower than that fallback, though, not a mirror of
        # it: _tool_append also catches OSError, because ending a live turn over a full disk would be
        # worse than one inline card, but this script is a batch job over the user's only copy of
        # their history, run once with nothing else depending on it staying up. Catching OSError here
        # too would leave a partially migrated, silently inconsistent sessions.json on a write
        # failure the caller never sees; letting it raise keeps every run idempotent, so re-running
        # after fixing whatever disk problem caused it picks up exactly where it left off.
        logger.warning(
            "A stored sub-agent tool response for %r could not be written to a payload file "
            "(undecodable text); leaving it inline instead.",
            append.get("name"),
        )
        report.undecodable_skipped += 1
        return False

    # Measured as the JSON-encoded size of this one append dict, before and after, with the same
    # ensure_ascii=True that TinyDB's JSONStorage always writes with. A plain character count
    # understates the real shrinkage: every non-ASCII or control character removed from `response`
    # was costing up to six bytes on disk as a \uXXXX escape, not one.
    before_size = len(json.dumps(append, ensure_ascii=True))
    reference = _dry_run_reference_placeholder() if dry_run else payloads.save_text(payloads_path, response)
    append["response"] = response[:RESPONSE_PREVIEW_CHARS]
    append["response_ref"] = reference
    append["response_bytes"] = len(response)
    after_size = len(json.dumps(append, ensure_ascii=True))

    report.blobs_written += 1
    report.bytes_reclaimed += before_size - after_size
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

    The backup is taken lazily, right before the first real write (the first session whose scan found
    something to change), not unconditionally at the top: a run that changes nothing (an
    already-migrated file, or one with no oversized responses at all) makes no backup, because a
    backup nothing was ever written over is not one this script needs.

    Calling this function directly, as the tests do, performs no safety check on whether Kokua is
    running; that check belongs to `main()`, the one caller a person runs by hand, not to the function
    a test calls in isolation against a throwaway directory.
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
            if not changed:
                continue
            report.sessions_changed += 1
            if dry_run:
                continue
            if not backed_up:
                _backup(sessions_path)
                backed_up = True
            store.save(session)
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

    # `_tool_appends` is the one place a `response_ref` is written today (see `core/subagents.py`),
    # so it is also the one place this scan needs to look to know what is referenced. `payloads.py`
    # itself is documented as reusable by any future feature with oversized text to keep out of
    # `sessions.json`; if one arrives and stores its reference somewhere other than a `"tool"`
    # append, this scan will not see it and will delete that feature's own live blobs. Whoever later
    # deletes this script because every installation has run it should not mistake this function for
    # a reusable "what does sessions.json reference" utility; it answers that question for exactly
    # one writer.
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


def _load_config() -> AssistantConfig:
    """The same ``AssistantConfig`` a real ``kokua`` invocation would resolve on this machine.

    A bare ``AssistantConfig()`` skips ``config.toml`` entirely, so ``[paths] data_dir`` (a real,
    documented setting) goes unread: every field falls back to its built-in default, honoring only
    ``$KOKUA_HOME`` (which that default already reads) and never a data directory chosen in the file.
    A user who set it would have this script scan and write against a directory the running app
    never touches, reporting zero to migrate while their real, oversized ``sessions.json`` sits
    untouched next door.

    Going through :func:`kokua.cli.resolve_config` with an argument-less ``Namespace`` reruns the same
    defaults-then-file-then-flags resolution ``kokua`` itself performs on every run, just with no CLI
    flags of its own supplied, since this script's own argument list (``sessions_file``, ``--dry-run``,
    ...) is unrelated to it.
    """
    from kokua.cli import build_arg_parser, resolve_config

    return resolve_config(build_arg_parser().parse_args([]))


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
            "and never in a dry run, since deleting files is not something a dry run may do. Refused "
            "together with a custom sessions_file, since pruning always uses the configured payloads "
            "directory and a sessions file that is not the real one would make it delete blobs other, "
            "real sessions still use."
        ),
    )
    parser.add_argument(
        "--payloads-dir",
        type=Path,
        default=None,
        help=(
            "Where payload files are written and read back from (default: the configured payloads "
            "directory). Required whenever sessions_file is given and is not the configured "
            "sessions.json itself, for a real (non-dry-run) migration: writing response_ref values "
            "under the default directory while migrating a different sessions file would point every "
            "migrated card at blobs the app reading that other file would never find, and the "
            "original response text would then survive only in the timestamped backup."
        ),
    )
    parser.add_argument(
        "--confirm-kokua-stopped",
        action="store_true",
        help=(
            "Required before any real (non-dry-run) write, including --prune-orphans. This script "
            "cannot reliably detect a running Kokua from outside it, so this flag is your statement "
            "that you checked, not a check this script performs. Running a real migration or a prune "
            "alongside a live Kokua can silently lose data, with nothing in the backup to show it: a "
            "payload the live recorder just wrote can be pruned before the session referencing it is "
            "next saved, and sessions.json is rewritten whole on every save rather than written to a "
            "new file and swapped in, so a save this script makes and one Kokua makes at close to the "
            "same time can each overwrite the other."
        ),
    )
    args = parser.parse_args()

    if args.prune_orphans and args.sessions_file is not None:
        parser.error(
            "--prune-orphans cannot be combined with a custom sessions_file argument. It always "
            "deletes from the configured payloads directory, so run it with no positional argument, "
            "against the configured sessions file, once that file reflects every session that can "
            "reference a payload there."
        )
    if not args.dry_run and not args.confirm_kokua_stopped:
        parser.error(
            "Refusing to write without --confirm-kokua-stopped. Stop Kokua first, then pass that "
            "flag; see --help for the two specific ways a concurrent write can lose data."
        )

    config = _load_config()
    sessions_path = args.sessions_file if args.sessions_file is not None else config.sessions_path
    payloads_path = args.payloads_dir if args.payloads_dir is not None else config.payloads_path

    # A dry run only ever prints a report, so a sessions_file pointed somewhere other than the
    # configured one costs nothing worse than a report about the wrong file. A real run is where the
    # mismatch is destructive: response_ref values recorded under the *default* payloads directory
    # while migrating a *different* sessions.json point at blobs the app reading that other file
    # would never look for, so this is refused rather than silently doing the harmful thing.
    if (
        not args.dry_run
        and args.sessions_file is not None
        and args.payloads_dir is None
        and sessions_path.expanduser().resolve() != config.sessions_path.resolve()
    ):
        parser.error(
            "sessions_file is not the configured sessions.json, so writing response_ref values "
            "under the default payloads directory would point them at blobs the app (reading its "
            "own configured sessions file) would never find. Pass --payloads-dir to say where those "
            "blobs should actually go, or drop sessions_file to run against the configured file."
        )

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
