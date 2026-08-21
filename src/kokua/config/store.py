"""Programmatic reads and comment-preserving writes to config.toml.

The config file is both hand-authored and app-written (the ``add_mcp_server`` tool and the assistant's
own ``update_config`` tool both write it). stdlib ``tomllib`` only reads TOML,
so writes go through ``tomlkit``, which patches the parsed document in place and so keeps the user's
comments and formatting. ``settings.py`` still owns config reads and validation; this module owns the
write path and the policy that guards it.

When the file does not exist yet, the first write seeds it from the shipped example (``config init``'s
content) so a fresh file keeps its documentation rather than becoming a bare one-key stub.

Single-user app: writes are last-writer-wins. A hand-edit made while the app is also writing can be
clobbered; that is accepted rather than guarded with file locking.

Nothing here formats a sentence. ``apply_setting`` returns what it did or raises; the ``config``
toolset in ``toolsets/config.py`` words it for the model.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.container import Container
from tomlkit.items import Comment, InlineTable, Key, Null, Table, Whitespace

from kokua.config import file as settings

logger = logging.getLogger(__name__)


def name_from_url(url: str) -> str:
    """A server name derived from its host, for a server added without one.

    Names are the namespace an agent declares against, so every server needs one. A host is stable,
    readable, and already unique per server in practice.
    """
    host = urlparse(url).hostname or "mcp"
    return host.replace(".", "-")


def disambiguate_name(base: str, used: set[str]) -> str:
    """``base``, or ``base-2``, ``base-3``, ... -- the first not already in ``used``.

    Two servers on one host (a service exposing several MCP endpoints under one domain) derive the same
    ``name_from_url`` base; shared by every caller that turns a URL into a registry name (the
    ``add_mcp_server`` config write and the ``--mcp`` CLI flag), so a config that names two collides the
    same way regardless of which of them derived the name.

    These two live with the write that mints the name rather than in ``mcp/servers.py``, where they used
    to: ``config`` is the bottom layer and imports nothing above it, which is what lets
    ``mcp/servers.py`` record a runtime-added server here.
    """
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def _load(path: Path) -> TOMLDocument:
    """Parse the file for editing, seeding from the shipped example if it does not exist yet.

    That seed includes the shipped example's ``[agents.*]`` tables: if the file is deleted mid-session,
    the next write here (from ``update_config`` or ``add_mcp_server``) recreates it, default agents and
    all. The content is exactly what ``kokua config init`` would write, so this is not a way to grant a
    capability beyond what a hand-edit already could -- but it is a path by which ``[agents.*]`` reappears
    on disk without a human typing it.
    """
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.parse(settings.example_text())


def _write(path: Path, doc: TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _section_table(doc: TOMLDocument, section: str, *, create: bool):
    """The table at a (possibly dotted) ``section`` path, walked segment by segment.

    ``doc["assistant.generation"] = table`` writes a *quoted* header -- a top-level key whose name
    contains a dot -- which is not the sub-table of ``[assistant]`` the reader looks in. Walking the
    segments is what makes the write land where ``load`` reads. Returns ``None`` for a missing path
    when ``create`` is False.
    """
    table = doc
    for segment in section.split("."):
        nested = table.get(segment)
        if nested is None:
            if not create:
                return None
            nested = tomlkit.table()
            table[segment] = nested
        table = nested
    return table


def set_value(path: Path, section: str, key: str, value) -> None:
    """Set ``[section].key = value``, creating the section if absent, preserving everything else."""
    doc = _load(path)
    _section_table(doc, section, create=True)[key] = value
    _write(path, doc)


def unset_value(path: Path, section: str, key: str) -> None:
    """Remove ``[section].key`` if present; a missing section or key is a no-op."""
    doc = _load(path)
    table = _section_table(doc, section, create=False)
    if table is not None and key in table:
        del table[key]
        _write(path, doc)


def _server_array(doc: TOMLDocument):
    """The ``[[mcp.server]]`` array-of-tables, created (with its ``[mcp]`` parent) if absent."""
    mcp = doc.get("mcp")
    if mcp is None:
        mcp = tomlkit.table()
        doc["mcp"] = mcp
    servers = mcp.get("server")
    if servers is None:
        servers = tomlkit.aot()
        mcp["server"] = servers
    return servers


def _unique_name(servers, url: str) -> str:
    """A name derived from ``url``, disambiguated against every name already in ``servers``.

    Appending a numeric suffix until the name is free (see ``disambiguate_name``) keeps a successful
    ``add_mcp_server`` call from producing a config the registry's collision check would later reject at
    boot, deferred past the point where the tool could still report the problem usefully.
    """
    used = {entry.get("name") for entry in servers}
    return disambiguate_name(name_from_url(url), used)


def add_mcp_server(path: Path, url: str, token_env: str | None = None) -> None:
    """Append (or, by URL, replace) an ``[[mcp.server]]`` entry.

    A runtime-added server (OAuth or unauthenticated) is recorded with just its URL and a name derived
    from it, so it reconnects on the next restart; ``token_env`` is written only for a bearer-token
    server declared explicitly. The derived name reaches no agent until a human names it in
    ``[agents.*]``, since that section is hand-edit only and this write cannot grant capability.

    Replacing an existing entry for the same URL keeps that entry's ``name`` rather than re-deriving
    one, since a human may have hand-edited it to match an ``[agents.*]`` reference that this write
    cannot see or repair. A brand-new entry gets a freshly derived name, disambiguated against every
    name already on file so this call can never write a name collision the registry would reject later.
    """
    doc = _load(path)
    servers = _server_array(doc)
    existing_name = None
    for i, entry in enumerate(servers):
        if entry.get("url") == url:
            existing_name = entry.get("name")
            del servers[i]
            break
    table = tomlkit.table()
    table["url"] = url
    table["name"] = existing_name or _unique_name(servers, url)
    if token_env is not None:
        table["token_env"] = token_env
    servers.append(table)
    _write(path, doc)


def remove_mcp_server(path: Path, url: str) -> bool:
    """Remove the ``[[mcp.server]]`` entry with this URL. Returns whether one was removed."""
    doc = _load(path)
    servers = _server_array(doc)
    for i, entry in enumerate(servers):
        if entry.get("url") == url:
            del servers[i]
            _write(path, doc)
            return True
    return False


def _task_tables(doc: TOMLDocument, *, create: bool):
    """The ``[scheduling.task]`` table holding one sub-table per task, or ``None`` when absent.

    Walked segment by segment for the same reason ``_section_table`` is: assigning a dotted key would
    write a quoted top-level header rather than the sub-table the reader looks in.
    """
    scheduling = doc.get("scheduling")
    if scheduling is None:
        if not create:
            return None
        scheduling = tomlkit.table(is_super_table=True)
        doc["scheduling"] = scheduling
    tables = scheduling.get("task")
    if tables is None:
        if not create:
            return None
        tables = tomlkit.table(is_super_table=True)
        scheduling["task"] = tables
    return tables


def load_tasks(path: Path) -> list[dict]:
    """Every ``[scheduling.task.*]`` table as a record, each validated by ``file.parse_task``.

    Raises ``ConfigError`` rather than returning ``[]`` for an unreadable file, unlike the tolerant
    read the JSON registry used to do. ``TaskService`` treats "no such task" as "it was cancelled" and
    drops it, so swallowing a parse error would let a syntax slip anywhere in config.toml silently
    delete a recurring task. The caller decides what to do with the failure instead.
    """
    text = read_text(path)
    if text is None:
        return []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise settings.ConfigError(f"{path} is not valid TOML: {error}") from error
    scheduling = data.get("scheduling")
    tables = scheduling.get("task") if isinstance(scheduling, dict) else None
    if tables is None:
        return []
    if not isinstance(tables, dict):
        raise settings.ConfigError("[scheduling.task] must hold one table per task name")
    return [settings.parse_task(name, spec) for name, spec in tables.items()]


def write_task(path: Path, name: str, record: dict) -> None:
    """Upsert ``[scheduling.task.<name>]``, leaving every other section and comment alone.

    An existing table is patched key by key rather than replaced, so a comment the user wrote against
    one of its keys survives a write the app makes for an unrelated one. ``name`` is not written into
    the table: it is the key. ``enabled`` is written only when false, since an absent key already means
    enabled and a line saying so on every task is noise.

    A key whose parsed value already equals the incoming one is left untouched rather than
    reassigned. tomlkit's own containers compare equal to the plain dict/scalar ``parse_task``
    produces, so this is a real value comparison, not a formatting one -- but reassigning even an
    unchanged value replaces tomlkit's *rendering* of it (an inline ``schedule = { ... }`` becomes a
    nested ``[...schedule]`` table) and detaches any comment written directly above that line. Only a
    key whose value actually changed needs a new rendering.
    """
    doc = _load(path)
    tables = _task_tables(doc, create=True)
    table = tables.get(name)
    if table is None:
        table = tomlkit.table()
        tables[name] = table
    written = {
        key: value
        for key, value in record.items()
        if key != "name" and value is not None and not (key == "enabled" and value is True)
    }
    for key, value in written.items():
        if key in table and table[key] == value:
            continue
        table[key] = value
    for stale in [key for key in table if key not in written]:
        del table[stale]
    _write(path, doc)


# --- Which task a comment belongs to -----------------------------------------------------------------
#
# tomlkit does not store the comment lines written above a ``[header]`` next to that header. It appends
# them to whatever precedes the header in the document: the previous sibling table's own body when
# there is one, and the enclosing section's body when the header opens its section. So deleting or
# re-appending a task's table leaves those lines behind, where they become the comment of whichever
# task now occupies the slot. A comment reading "do not disable" silently retitling a different task is
# the one failure that would contradict the point of keeping tasks in a file the user can annotate, so
# the helpers below move a task's comment deliberately.
#
# The rule they implement: a task's own comment is the unbroken run of comment lines directly above its
# header. A blank line ends the run, so a comment separated from the header by one reads as a note on
# whatever is above it and stays where it is. That separated case is genuinely ambiguous; this is the
# reading a person scanning the file would give it.
#
# They edit ``Container.body`` in place, and blank a removed slot with ``Null`` (which renders as
# nothing) instead of shortening the list, because a container keys its lookup map by body index. What
# they do not repair is that map's *keys*, which a rename leaves naming the old table. That is safe only
# because rendering reads the body and nothing else, and every caller here writes and discards the
# document immediately: look a task up again after one of these and the lookup answers from before.


def _indices_in(body: list, name: str) -> list[int]:
    """Every position in ``body`` holding a table named ``name``, in document order.

    An inline table counts: ``task.nightly = { prompt = ... }`` is a task ``load_tasks`` reads, so a
    remove or rename that skipped it would report success and leave the task on disk. One name can
    occupy several positions in one body when a hand edit writes a task as dotted keys
    (``nightly.prompt`` on one line, ``nightly.schedule`` on another), which tomlkit keeps as one entry
    per line.
    """
    return [
        index
        for index, (key, item) in enumerate(body)
        if key is not None and key.key == name and isinstance(item, (Table, InlineTable))
    ]


def _task_sections(doc: TOMLDocument) -> list[tuple[Container, list[tuple[list, int]]]]:
    """Every ``[scheduling.task]`` container, each with the path of ``(body, index)`` steps reaching it.

    The path is what lets ``_comment_slot`` find the body a task's comment lines actually live in.
    A hand-edited file may split the section (``[scheduling.task.a]``, some other section, then
    ``[scheduling.task.b]``), which tomlkit keeps as separate fragments, so this collects all of them
    rather than the first.

    Descending into an inline table (``task = { nightly = { ... } }``) starts the path over, because
    TOML forbids a comment line inside one: no comment found outside it can belong to an entry within
    it, so ``_comment_slot`` must not reach out past the inline table to claim one.
    """
    frontier: list[tuple[Container, list[tuple[list, int]]]] = [(doc, [])]
    for segment in ("scheduling", "task"):
        deeper: list[tuple[Container, list[tuple[list, int]]]] = []
        for container, chain in frontier:
            body = container.body
            for index, (key, item) in enumerate(body):
                if key is None or key.key != segment:
                    continue
                if isinstance(item, Table):
                    deeper.append((item.value, [*chain, (body, index)]))
                elif isinstance(item, InlineTable):
                    deeper.append((item.value, []))
        frontier = deeper
    return frontier


def _find_task(doc: TOMLDocument, name: str) -> list[tuple[Container, list[tuple[list, int]]]]:
    """Every fragment of task ``name``: its container, and the ``(body, index)`` steps down to it.

    A task normally has exactly one fragment, but a hand edit can spread one task's keys over several
    (``[scheduling.task.a]`` here, ``[scheduling.task.a.schedule]`` after an intervening section, or one
    dotted line per key). ``load_tasks`` reads those as a single task, so a remove that took only the
    first would leave the rest behind and turn the next read into a ``ConfigError`` about a task missing
    a required key.
    """
    return [
        (container, [*chain, (container.body, index)])
        for container, chain in _task_sections(doc)
        for index in _indices_in(container.body, name)
    ]


def _comment_run_start(body: list, position: int) -> int:
    """Where the run of comment lines ending at ``position`` begins.

    Walks back over ``Comment`` items only, so a blank line (a ``Whitespace``) or any real content ends
    the run. Returns ``position`` itself when nothing there is a comment.
    """
    start = position
    while start > 0 and body[start - 1][0] is None and isinstance(body[start - 1][1], Comment):
        start -= 1
    return start


def _comment_slot(chain: list[tuple[list, int]]) -> Optional[tuple[list, int]]:
    """The body list and position rendered immediately above the item ``chain`` ends at.

    Walks outward for as long as the item is the first thing in its container, since that is exactly
    when its comment lines were appended to the enclosing section instead of to a previous sibling.
    Returns ``None`` when nothing at all precedes the item, which is to say it opens the file.
    """
    for body, index in reversed(chain):
        if index > 0:
            previous = body[index - 1][1]
            if isinstance(previous, Table):
                return previous.value.body, len(previous.value.body)
            return body, index
    return None


def _take_comment_run(body: list, position: int) -> list:
    """Blank the comment run ending at ``position`` out of ``body`` and return its items."""
    start = _comment_run_start(body, position)
    taken = [item for _, item in body[start:position]]
    for index in range(start, position):
        body[index] = (None, Null())
    return taken


def _as_one_item(items: list) -> Whitespace:
    """The run as a single item, since one body slot holds exactly one.

    ``Whitespace`` is the carrier because its two behaviors are the two wanted here: tomlkit emits its
    string verbatim, so joining the run's own rendering reproduces the original lines exactly, and it
    does not count as content, so a super table holding one still declines to print a header of its own.
    """
    return Whitespace("".join(item.as_string() for item in items))


def _delete_task(container: Container, chain: list[tuple[list, int]]) -> None:
    """Remove the task ``chain`` ends at, taking its own comment with it and leaving the next task's.

    The trailing comment run inside the deleted table is the *next* table's comment, so it is re-homed
    into the slot the deleted table vacated, which renders in the same place. The run leading the
    deleted table is this task's own, and is taken out of wherever tomlkit put it. An inline table has
    neither: it is one line with no body of its own, and the comment above that line is found by the
    same walk, so no special case is needed for it here.

    The slot can already be blank when one task has several fragments in a single container (dotted
    keys, one per line): tomlkit maps that name to every one of its positions and blanks them all on the
    first removal, so a later fragment of the same task is already gone by the time it is reached.
    """
    body, index = chain[-1]
    key, table = body[index]
    if key is None:
        return
    successor_comment = _take_comment_run(table.value.body, len(table.value.body))
    slot = _comment_slot(chain)
    if slot is not None:
        _take_comment_run(*slot)
    container.remove(key)
    if successor_comment:
        body[index] = (None, _as_one_item(successor_comment))


def remove_task(path: Path, name: str) -> bool:
    """Delete ``[scheduling.task.<name>]`` and the comment lines above it. Returns whether one was found.

    A comment directly above the header is read as this task's and leaves with it; one separated from
    the header by a blank line stays, along with every neighbouring task's.

    Every fragment of the task goes, not just the first. Deletion blanks a body slot in place rather
    than shortening the list, so the positions of the fragments still to be reached stay valid.
    """
    doc = _load(path)
    fragments = _find_task(doc, name)
    if not fragments:
        return False
    for fragment in fragments:
        _delete_task(*fragment)
    _write(path, doc)
    return True


def _renamed_key(old: Key, new: str) -> Key:
    """``new`` as a key rendered the way ``old`` was, dotted or not.

    tomlkit records dottedness on the key, not on the table it points at, and decides from it whether to
    render the table as a ``task.<name>.prompt = ...`` line or as a ``[task.<name>]`` header. A plain
    ``tomlkit.key(new)`` is never dotted, so swapping one into a dotted line rewrites it as a header,
    and a task split across two dotted lines becomes two headers of the same name: a file that no longer
    parses. tomlkit exposes no setter for the flag, hence the direct assignment.
    """
    key = tomlkit.key(new)
    key._dotted = old.is_dotted()
    return key


def rename_task(path: Path, old: str, new: str) -> None:
    """Rename a task's table where it stands, contents, position, and comment intact.

    The key is swapped in place rather than the table being deleted and re-appended at the end of the
    section, so nothing else in the file moves and no comment changes hands. ``invalidate_display_name``
    is what makes the header print the new key: a parsed table remembers the header text it came from
    and would otherwise keep rendering the old name. It applies to a table with a header of its own;
    an inline table has none, and renaming its key is the whole edit.

    Every fragment of a task split across several is renamed, so the file still reads back as one task.

    A missing ``old`` is a no-op. An existing ``new`` is deleted first, since two tables of one name is
    not a file that can be read back.
    """
    doc = _load(path)
    fragments = _find_task(doc, old)
    if not fragments:
        return
    if new != old:
        for collision in _find_task(doc, new):
            _delete_task(*collision)
    for _, chain in fragments:
        body, index = chain[-1]
        key, table = body[index]
        body[index] = (_renamed_key(key, new), table)
        if isinstance(table, Table):
            table.invalidate_display_name()
    _write(path, doc)


# --- The programmatic-write policy and the apply path ------------------------------------------------

# Keys no programmatic write may change, even behind the approval prompt: the tool-approval gate
# itself, the locked email recipient, and where all state lives. Only a hand-edit of config.toml
# changes these.
LOCKED_KEYS: frozenset[tuple[str, str]] = frozenset(
    {("security", "confirm_tools"), ("email", "to"), ("paths", "data_dir")}
)


class SettingLocked(Exception):
    """This key may only be changed by hand-editing config.toml. Carries the section and key."""

    def __init__(self, section: str, key: str):
        super().__init__(f"[{section}].{key}")
        self.section = section
        self.key = key


class HotApplyFailed(Exception):
    """A hot-appliable setting could not be applied to the running session, so nothing was written.

    Its message is the underlying failure's, so a caller can report the cause without unwrapping.
    """

    def __init__(self, section: str, key: str, cause: BaseException):
        super().__init__(str(cause))
        self.section = section
        self.key = key
        self.cause = cause


@dataclass(frozen=True)
class AppliedSetting:
    """What :func:`apply_setting` did. ``hot`` says it also took effect in the running session."""

    section: str
    key: str
    value: object
    hot: bool


def is_locked(section: str, key: str) -> bool:
    """Whether only a hand-edit or a dedicated tool may change this key.

    ``[agents.*]`` is locked wholesale, and by prefix rather than by an entry in :data:`LOCKED_KEYS`,
    because a section name is per-agent (``agents.<name>``) and so cannot be enumerated ahead of time.
    It declares what every agent can do, and ``update_config`` is a tool the assistant holds, so a
    writable agent table would let the assistant widen its own reach. Granting a capability stays a
    human decision.

    ``[scheduling.task.*]`` is locked by prefix for a different reason: routing, not capability. The
    assistant may change any task, but only through the scheduling tools, because every task write has
    to be paired with the scheduler (un)arming that accompanies it. A bare ``update_config`` write
    would edit the file and leave the running scheduler firing the old schedule. The parent
    ``[scheduling]`` section stays writable: its ``max_task_conversations`` is an ordinary hot setting.
    """
    if (section, key) in LOCKED_KEYS:
        return True
    return section in ("agents", "scheduling.task") or section.startswith(("agents.", "scheduling.task."))


def read_text(path: Path) -> str | None:
    """The config file's contents, or ``None`` when it does not exist yet."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


async def apply_setting(
    path: Path,
    section: str,
    key: str,
    raw: str,
    apply_hot: Callable[[str, str, object], Awaitable[None]],
    *,
    table,
    extra_schema: Optional[dict] = None,
) -> AppliedSetting:
    """Coerce, apply, and persist one setting. Raises rather than reporting.

    A hot-appliable change (the display flags, and whatever the installed toolsets declared as hot) is
    applied to the live session BEFORE it is written, so a value that coerces but cannot be applied is
    not persisted and left to break the next startup. That ordering is the whole reason this is one
    function rather than a coerce call and a write call at the call site.

    The model is *not* among the hot settings, despite being the setting a reader expects to find there:
    no live client is ever rebound, so ``[assistant].model`` is startup-only and takes the cold path
    below. Nothing here can tell whether the string it writes names a real model -- that answer needs
    AIMU, which this layer cannot import -- so the caller supplies the check as a schema converter
    (``toolsets.config._resolvable_model``). The caller is told which path a change took, via
    :attr:`AppliedSetting.hot`.

    ``table`` is the live :class:`~kokua.config.table.SettingsTable`: it decides both what a value coerces
    to and whether the change is hot, so both questions are answered by the same declaration. ``table``
    alone is not the whole schema, though: it holds only hot settings, so ``extra_schema`` carries the
    *cold* keys the installed toolsets declared, without which this refuses one as an unknown key.

    Raises :class:`SettingLocked`, ``ConfigError`` (from coercion), or :class:`HotApplyFailed`.
    """
    if is_locked(section, key):
        raise SettingLocked(section, key)
    coerced = settings.coerce_config_string(section, key, raw, table=table, extra_schema=extra_schema)

    if table.is_hot(section, key):
        try:
            await apply_hot(section, key, coerced)
        except Exception as error:
            logger.warning("Could not apply [%s].%s live", section, key, exc_info=True)
            raise HotApplyFailed(section, key, error) from error
        set_value(path, section, key, coerced)
        return AppliedSetting(section, key, coerced, hot=True)

    set_value(path, section, key, coerced)
    return AppliedSetting(section, key, coerced, hot=False)
