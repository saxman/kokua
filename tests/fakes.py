"""Shared fakes: MCP servers and model clients that behave in a specific, scripted way.

Here rather than in one test module because several packages' tests need them, and a test importing
another test module makes the suite's layout load-bearing.
"""

from __future__ import annotations

import asyncio


from tests.helpers import MockAsyncModelClient


def _fake_mcp_tool(name: str):
    async def fn(**kwargs):
        return "ok"

    fn.__name__ = name
    fn.__tool_spec__ = {"function": {"name": name}}
    fn.__tool_is_async__ = True
    fn.__tool_is_streaming__ = False
    return fn


class _FakeMCP:
    def __init__(self, tools):
        self._tools = tools
        self.closed = False

    async def as_tools(self):
        return self._tools

    async def aclose(self):
        self.closed = True


async def _await_value(value):
    return value


def _offline_until_connected(monkeypatch, tool_name: str = "get_quote") -> None:
    """Patch the one connect seam so a configured server fails at boot and succeeds on every later try.

    Declared-but-offline is what keeps a runtime-connect test honest. A server an agent can name has to
    be in ``[[mcp.server]]`` to be in the registry at all, but that also means the boot reconnect
    connects it -- so a later ``add_mcp_server`` would answer "Already connected", rebuild nothing, and
    leave every assertion about the rebuild passing for the wrong reason.
    """
    attempts = {"count": 0}

    async def fake_connect(url, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("unreachable at boot")
        return _FakeMCP([_fake_mcp_tool(tool_name)]), "none"

    monkeypatch.setattr("kokua.mcp.servers.connect_mcp", fake_connect)


class _SeedsSystemMessage:
    """Mixin: seed the system message into an empty transcript, the way the real clients do.

    AIMU's ``_append_user_turn`` prepends the system message when ``messages`` is empty, so a
    conversation's *first* turn produces ``[system, user, assistant, ...]`` and its user message sits
    one past the pre-run length. ``MockAsyncModelClient`` overrides ``_chat`` and so never seeds one,
    which is why the suite could not see a first-turn off-by-one in the index a turn's sub-agent cards
    are filed under. Mix in ahead of the client to get the real transcript:
    ``class C(_SeedsSystemMessage, MockAsyncModelClient)``.
    """

    def __init__(self, *args, system_message: str = "You are a test assistant.", **kwargs):
        super().__init__(*args, **kwargs)
        self._system_message = system_message  # after super(), which sets it to None

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        if len(self.messages) == 0 and self._system_message:
            self._append_message({"role": "system", "content": self._system_message})
        return await super()._chat(user_message, generate_kwargs, use_tools, stream, images, audio)


class _BlockingStreamClient(MockAsyncModelClient):
    """Records the user turn, signals it started, then hangs until the turn task is cancelled."""

    def __init__(self):
        super().__init__([])
        self.started = asyncio.Event()

    async def _chat(self, user_message, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None):
        self.messages.append({"role": "user", "content": user_message})
        self.started.set()
        await asyncio.Event().wait()  # hang until cancelled


class _RequestsToolOnce(MockAsyncModelClient):
    """Requests one gated tool call on the first turn (single-turn: the Agent's engine dispatches it),
    then answers plainly. Lets a real run exercise the approval gate instead of the mock's faked round."""

    def __init__(self, name: str, arguments: dict):
        super().__init__([])
        self._name = name
        self._arguments = arguments
        self._requested = False

    async def _chat(
        self, user_message=None, generate_kwargs=None, use_tools=True, stream=False, images=None, audio=None
    ):
        if user_message is not None:
            self.messages.append({"role": "user", "content": user_message})
        if not self._requested:
            self._requested = True
            self.messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"type": "function", "function": {"name": self._name, "arguments": self._arguments}, "id": "x"}
                    ],
                }
            )
            return ""
        self.messages.append({"role": "assistant", "content": "ok"})
        return "ok"


def shelf_toolset(builds: list | None = None, **kwargs):
    """A toolset granted whole as ``shelf`` or narrower as its parts ``a`` and ``b``, standing in for any
    capability whose parts the config derives. Its two tools, ``read_shelf`` and ``edit_shelf``, answer
    with the parts their build was handed, and ``builds`` records each build's parts (the whole build
    records both), so a test can tell which grant reached the agent."""
    from aimu.tools import tool

    from kokua.registry.registry import Toolset

    builds = builds if builds is not None else []

    def build_parts(ctx, parts):
        builds.append(frozenset(parts))
        scope = ", ".join(sorted(parts))

        @tool
        def read_shelf() -> str:
            """Read the shelves this grant covers."""
            return scope

        @tool
        def edit_shelf() -> str:
            """Edit the shelves this grant covers."""
            return scope

        return [read_shelf, edit_shelf]

    defaults = dict(parts=lambda config: {"a": "a", "b": "b"}, build_parts=build_parts)
    return Toolset(
        name="shelf",
        description="Shelves.",
        build=lambda ctx: build_parts(ctx, {"a", "b"}),
        **{**defaults, **kwargs},
    )


def install_shelf(monkeypatch, builds: list | None = None, **kwargs):
    """Register :func:`shelf_toolset` beside every installed toolset, as a third-party plugin would arrive."""
    from kokua.core import agents

    discovered = agents.discover_toolsets()
    shelf = shelf_toolset(builds, **kwargs)
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {**discovered, "shelf": shelf})
    return shelf


def binder_toolset():
    """A toolset whose read-only part truly lacks its write tool, the shape ``documents`` has: ``notes``
    grants ``read_binder`` alone, and ``notes_write`` (or the whole ``binder``) adds ``write_binder``. A
    gate on ``binder.write_binder`` has to resolve however narrowly the toolset was first built."""
    from aimu.tools import tool

    from kokua.registry.registry import Toolset

    def build_parts(ctx, parts):
        @tool
        def read_binder() -> str:
            """Read the binder's notes."""
            return "notes"

        @tool
        def write_binder() -> str:
            """Write to the binder's notes."""
            return "written"

        writable = any(can_write for _, can_write in parts)
        return [read_binder, write_binder] if writable else [read_binder]

    return Toolset(
        name="binder",
        description="Binders.",
        build=lambda ctx: build_parts(ctx, {("notes", True)}),
        parts=lambda config: {"notes": ("notes", False), "notes_write": ("notes", True)},
        build_parts=build_parts,
    )


def install_binder(monkeypatch):
    """Register :func:`binder_toolset` beside every installed toolset, as a third-party plugin would arrive."""
    from kokua.core import agents

    discovered = agents.discover_toolsets()
    binder = binder_toolset()
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {**discovered, "binder": binder})
    return binder
