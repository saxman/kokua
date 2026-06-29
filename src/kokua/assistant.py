"""The assistant core: wires AIMU primitives into a single-user, always-on assistant.

    Channel.receive()  ->  SkillAgent.run()  ->  Channel.send()
              Scheduler  ->  proactive SkillAgent.run()  ->  Channel.send()
              ConversationManager persists history across restarts
              author_skill / add_skill_script let the assistant grow its own skills
              memory tools give it persistent facts + documents
              tool-pack plugins contribute extra tools

Kept transport-agnostic (it takes a `Channel`), so the CLI and web front ends share it
unchanged. The CLI/web entry points live in `kokua.cli` / `kokua.frontends`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from aimu import aio
from aimu.aio import Channel, Scheduler
from aimu.aio.channels.base import ChannelMessage
from aimu.history import ConversationManager
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.skills import SkillManager, make_skill_authoring_tool, make_skill_script_tool
from aimu.tools import builtin, tool
from aimu.tools.builtin import make_document_tools, make_memory_tools

from .config import MEMORY_GUIDANCE, AssistantConfig
from .plugins import discover_tool_packs

logger = logging.getLogger(__name__)

# AIMU's built-in tool subgroups, selectable by name via the --tools flag / AssistantConfig.tools.
# The generative groups (image/audio/speech/transcription) need their AIMU_*_MODEL env var set and
# raise at call time otherwise, so they are not in the default set. The default tools are sync; the
# async agent dispatches them via asyncio.to_thread, so no wrapping is needed.
_TOOL_GROUPS = {
    "web": builtin.web,
    "fs": builtin.fs,
    "compute": builtin.compute,
    "misc": builtin.misc,
    "image": builtin.image,
    "audio": builtin.audio,
    "speech": builtin.speech,
    "transcription": builtin.transcription,
}


def _resolve_builtin_tools(names: list[str]) -> list:
    """Map tool-group names to built-in tool callables (deduped by name).

    ``"all"`` expands to ``builtin.ALL_TOOLS``; ``"none"`` contributes nothing. An unknown name
    raises ``ValueError`` listing the valid groups.
    """
    resolved: list = []
    seen: set[str] = set()
    for name in names:
        if name == "none":
            continue
        if name == "all":
            group = builtin.ALL_TOOLS
        elif name in _TOOL_GROUPS:
            group = _TOOL_GROUPS[name]
        else:
            valid = ", ".join(sorted(_TOOL_GROUPS)) + ", all, none"
            raise ValueError(f"unknown tool group {name!r}; choose from: {valid}")
        for fn in group:
            if fn.__name__ not in seen:
                seen.add(fn.__name__)
                resolved.append(fn)
    return resolved


def make_add_mcp_server_tool(agent: aio.SkillAgent, mcp_clients: list) -> Callable:
    """Build an ``add_mcp_server`` tool bound to ``agent`` and a live-client registry.

    Lets the assistant connect to a remote MCP service by URL mid-session and use its tools
    immediately. The new tools are appended to ``agent.tools`` (the configured list the
    SkillAgent copies to its model client every run, so they persist across turns), deduped by
    name. The connected client is kept in ``mcp_clients`` for the connection's lifetime.
    """

    @tool
    async def add_mcp_server(url: str, bearer_token: Optional[str] = None) -> str:
        """Connect to a remote MCP server by URL and add its tools to this assistant.

        The server's tools become callable immediately, even in this same turn. Pass
        bearer_token for an authenticated server. Returns the names of the newly available tools.
        """
        try:
            mcp = await aio.MCPClient.connect(url=url, auth=bearer_token)
            new_tools = await mcp.as_tools()
        except Exception as exc:
            return f"Failed to connect to MCP server {url!r}: {exc}"
        mcp_clients.append(mcp)
        existing = {getattr(fn, "__name__", None) for fn in agent.tools}
        added = [fn for fn in new_tools if fn.__name__ not in existing]
        agent.tools.extend(added)
        names = ", ".join(fn.__name__ for fn in added) if added else "(no new tools)"
        return f"Connected to {url}. Tools now available: {names}."

    return add_mcp_server


def _load_plugin_tools(config: AssistantConfig) -> list:
    """Build the tools contributed by installed tool-pack plugins (deduped by name)."""
    tools: list = []
    seen: set[str] = set()
    for name, pack in discover_tool_packs().items():
        try:
            pack_tools = pack.build(config)
        except Exception:
            logger.warning("Tool-pack %r failed to build; skipping.", name, exc_info=True)
            continue
        for fn in pack_tools:
            fname = getattr(fn, "__name__", None)
            if fname and fname not in seen:
                seen.add(fname)
                tools.append(fn)
        logger.info("Loaded tool-pack %r (%d tools).", name, len(pack_tools))
    return tools


class Assistant:
    """A single-user personal assistant wired from AIMU primitives."""

    def __init__(
        self,
        agent: aio.SkillAgent,
        channel: Channel,
        scheduler: Scheduler,
        conversation: ConversationManager,
        config: AssistantConfig,
    ):
        self._agent = agent
        self._channel = channel
        self._scheduler = scheduler
        self._conversation = conversation
        self._config = config
        # Live remote-MCP connections (startup + runtime-added) kept alive for their lifetime
        # and closed on shutdown. Assigned by create().
        self._mcp_clients: list = []
        # Persistent memory stores (None when --no-memory). Assigned by create(); persistence is
        # automatic (Chroma PersistentClient / DocumentStore disk writes), so no teardown needed.
        self._memory_store: Optional[SemanticMemoryStore] = None
        self._document_store: Optional[DocumentStore] = None
        # The reactive turn and a proactive turn share one agent/client; serialize them so
        # a reminder firing mid-conversation can't interleave on shared message state.
        self._lock = asyncio.Lock()

    @classmethod
    async def create(cls, config: AssistantConfig, channel: Channel, *, client=None) -> "Assistant":
        if client is None:
            system = config.system_message + (MEMORY_GUIDANCE if config.memory else "")
            client = aio.client(config.model, system=system)

        # Persistent memory: a SemanticMemoryStore for facts about the user and a DocumentStore for
        # longer reference documents. Both live under the app state dir, so they survive restarts and
        # span conversations (unlike per-conversation history). Tools have distinct names, so both
        # sets coexist on the one agent.
        memory_store: Optional[SemanticMemoryStore] = None
        document_store: Optional[DocumentStore] = None
        memory_tools: list = []
        if config.memory:
            memory_store = SemanticMemoryStore(persist_path=str(config.memory_path))
            document_store = DocumentStore(persist_path=str(config.documents_path))
            memory_tools = make_memory_tools(memory_store) + make_document_tools(document_store)

        plugin_tools = _load_plugin_tools(config) if config.load_plugins else []

        manager = SkillManager(skill_dirs=[str(config.skills_dir)])
        author_skill = make_skill_authoring_tool(manager, config.skills_dir)
        agent = aio.SkillAgent(client, tools=[author_skill], skill_manager=manager, name="assistant")
        # add_skill_script and add_mcp_server need the agent (to surface new tools this turn), so
        # they are built after it. Built-in tools, memory tools, and plugin tools are appended too;
        # the SkillAgent re-appends its skills-server tools each run.
        mcp_clients: list = []
        agent.tools = [
            author_skill,
            make_skill_script_tool(agent, manager, config.skills_dir),
            make_add_mcp_server_tool(agent, mcp_clients),
            *memory_tools,
            *plugin_tools,
            *_resolve_builtin_tools(config.tools),
        ]

        # Connect any startup MCP servers; their tools persist on agent.tools. A connect failure
        # logs and continues so one unreachable server can't stop the assistant from starting.
        for url in config.mcp_servers:
            try:
                mcp = await aio.MCPClient.connect(url=url, auth=config.mcp_bearer)
                agent.tools.extend(await mcp.as_tools())
                mcp_clients.append(mcp)
            except Exception:
                logger.warning("Could not connect MCP server %s; continuing without it.", url, exc_info=True)

        conversation = ConversationManager(config.history_path, use_last_conversation=True)
        prior = conversation.messages
        if prior:
            agent.restore(prior)

        scheduler = Scheduler()
        assistant = cls(agent, channel, scheduler, conversation, config)
        assistant._mcp_clients = mcp_clients  # same list the add_mcp_server tool appends to
        assistant._memory_store = memory_store
        assistant._document_store = document_store
        if config.reminder_seconds is not None:
            scheduler.at(config.reminder_seconds, assistant._proactive, name="reminder")
        return assistant

    async def run(self) -> None:
        """Serve the channel and run the scheduler concurrently until the channel closes."""
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._serve_channel())
                tg.create_task(self._scheduler.run())
        finally:
            for mcp in self._mcp_clients:
                try:
                    await mcp.aclose()
                except Exception:
                    logger.debug("Error closing MCP client", exc_info=True)

    async def _serve_channel(self) -> None:
        try:
            async for msg in self._channel.receive():
                await self._handle(msg)
        finally:
            self._scheduler.stop()  # channel closed -> stop the scheduler so run() returns

    async def _handle(self, msg: ChannelMessage) -> None:
        async with self._lock:
            try:
                stream = await self._agent.run(msg.text, stream=True, images=msg.images)
                await self._channel.send(stream, reply_to=msg)
            except Exception:
                logger.exception("Error handling message")
                await self._channel.send("Sorry, something went wrong handling that.", reply_to=msg)
            self._persist()

    async def _proactive(self) -> None:
        """Scheduled callback: produce a message unprompted and push it to the channel."""
        async with self._lock:
            reply = await self._agent.run(self._config.reminder_text)
            await self._channel.send(reply)
            self._persist()

    def _persist(self) -> None:
        # Copy each message so the manager's timestamp annotation doesn't leak into the live
        # model-client message dicts.
        self._conversation.update_conversation([dict(m) for m in self._agent.model_client.messages])
