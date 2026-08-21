"""The live state a toolset's ``build`` draws on, and the per-agent handle onto it.

Split in two deliberately. ``LiveState`` is one object per process and owns every shared singleton;
``ToolsetContext`` is one per agent being built and adds only ``agent``. A single class carrying both
would have to be copied per agent, and a copy computing a lazy singleton after the copy would give two
agents two memory stores over one directory.

Every singleton here is lazy, which is what lets a declaration be the only switch: a store is built
when some agent declares the toolset that needs it, and never otherwise. There is no configuration flag
that can disagree with the declaration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from aimu.aio import Scheduler
from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.skills import SkillManager

from kokua.config.schema import AssistantConfig
from kokua.config.table import SettingsTable

# The toolset that grants author_skill / add_skill_script. Declaring it opts an agent out of catalogue
# scoping, since an author has to see the skill it just wrote (see `skill_manager`).
_AUTHORING_TOOLSET = "skills"

if TYPE_CHECKING:
    # Not imported for real here, deliberately: aimu.aio.tools.builtin is the AIMU surface
    # aimu_compat.require_aimu probes for, and this module is reached from kokua.plugins (hence loaded at
    # `import kokua.cli` time, on every invocation including --help and `config init`) well before that
    # preflight ever runs. A real import here would turn an AIMU missing the newest surface into a bare
    # ImportError on those, defeating the preflight it would otherwise go through. toolsets/agents.py
    # holds the real unconditional import that keeps this annotation honest -- it is only ever imported
    # lazily, after preflight has already run.
    from aimu.aio.tools.builtin import SubagentObserver


@dataclass
class LiveState:
    """Process-wide state shared by every agent's toolsets.

    ``for_each_agent`` fans a global tool mutation (an MCP add or remove) across every live agent.
    ``proactive`` is the assistant's unprompted-turn entry point, which a due scheduled task fires.
    ``registry`` is the toolset registry, needed here because rebuilding an agent's delegation tool
    after an MCP change has to re-resolve names.
    """

    config: AssistantConfig
    notify: Optional[Callable] = None
    oauth_storage_dir: Optional[Path] = None
    connections: list = field(default_factory=list)
    for_each_agent: Optional[Callable[[Callable], None]] = None
    reapply_config: Optional[Callable] = None
    scheduler: Optional[Scheduler] = None
    proactive: Optional[Callable] = None
    # Typed Any rather than kokua.core.conversations.ConversationBook: that class lives under kokua.core,
    # and importing it here would import kokua/core/__init__.py, which imports core/build.py, which
    # imports this module -- the same cycle resolve_system_message's docstring works around by importing
    # kokua.toolsets.agents late. Left honestly untyped rather than worked around, unlike observer below.
    conversation_book: Optional[Any] = None
    turn_running: Optional[Callable[[str], bool]] = None
    # Cancels a scheduled task's in-flight firings, returning (how many, whether one was the run the call
    # came from). Assigned by the composition root, which owns the turn bookkeeping this reads.
    stop_task_runs: Optional[Callable[[str], tuple[int, bool]]] = None
    tool_approval: Optional[Callable] = None
    observer: Optional[SubagentObserver] = None
    registry: dict = field(default_factory=dict)
    # Rebuilds one agent's delegate after a runtime MCP change. Assigned by the composition root rather
    # than imported by the toolsets that need it, since it lives in core.build and core.build imports
    # them.
    refresh_workers: Optional[Callable[[Any], None]] = None
    # Every runtime-mutable setting this process knows, so the config toolset's update_config resolves a
    # key against the same declarations the applier uses. Assigned by the composition root,
    # which builds one table and shares it with the applier.
    settings_table: Optional[SettingsTable] = None

    @cached_property
    def memory_store(self) -> SemanticMemoryStore:
        return SemanticMemoryStore(persist_path=str(self.config.memory_path))

    @cached_property
    def document_store(self) -> DocumentStore:
        return DocumentStore(persist_path=str(self.config.documents_path))

    @cached_property
    def all_skills(self) -> SkillManager:
        """Every skill on disk, unscoped: the discovery authority the other two accessors build on.

        Separate from ``skill_manager`` because that one may be narrowed to what the entry agent
        declares, while a worker can declare any skill and so needs the full set resolvable.
        """
        return SkillManager(skill_dirs=[str(self.config.skills_dir)])

    @cached_property
    def skill_manager(self) -> SkillManager:
        """The SkillManager the entry agent's ``SkillAgent`` reads, scoped to what that agent declares.

        One instance shared by every conversation's agent -- deliberately, the same reasoning
        ``memory_store`` and ``document_store`` apply above. Skills are files in one user-owned
        directory, and a skill the user teaches in one conversation should be usable in every other one,
        not just the conversation that happened to author it. A manager per agent was the accident this
        replaces: it gave each conversation its own catalog cache, so a skill authored in one was
        invisible to another until that one happened to refresh on its own. Every conversation's agent is
        the entry agent, so one scope fits them all.

        **An agent that can author skills sees all of them.** Narrowing an author's catalogue would hide
        the skill it just wrote: ``add_skill_script`` tells the model its script is callable in the same
        turn, which cannot be true if the new skill falls outside an ``include`` set fixed at startup.
        So holding the authoring toolset opts out of scoping entirely, and only a non-authoring entry
        agent gets a catalogue limited to its declaration.

        Turns on different conversations run concurrently, and both ``author_skill`` and
        ``add_skill_script`` call ``manager.refresh()`` on this one shared instance. That is safe without
        a lock: ``refresh()`` only invalidates the cached catalog, and the next read rebuilds it by
        re-scanning the skills directory on disk, which is idempotent. Two concurrent refreshes therefore
        race at worst to assign an equivalent map, never a torn or corrupt one.
        """
        entry = self.config.agents.get(self.config.entry_agent)
        if entry is None:
            # Nothing to scope by. A real run cannot reach here: `validate_agents` rejects a config
            # whose [assistant].agent names no table, and owns that error. This keeps a LiveState
            # usable on its own, which is how the unit tests exercise the accessors above.
            return self.all_skills
        declared = entry.tools
        if _AUTHORING_TOOLSET in declared:
            return self.all_skills
        known = self.all_skills.skills
        return SkillManager(
            skill_dirs=[str(self.config.skills_dir)],
            include=[name for name in declared if name in known],
        )

    @cached_property
    def skill_tools(self) -> dict[str, list]:
        """Each skill's callable tools by skill name: its own scripts plus the shared ``activate_skill``.

        This is how a skill reaches an agent that is not a ``SkillAgent``. A spawned worker is a plain
        AIMU ``Agent``, so it has no skill machinery to hook, and the registry is its only route: a
        skill's toolset returns that skill's entry from this map.

        Built once, from the unscoped manager, so every declarable skill is present regardless of which
        agent asks. The sync ``MCPClient`` runs its in-process server through an anyio blocking portal on
        a background thread, so building this from the event-loop thread is safe; it is one in-process
        ``list_tools`` round-trip, paid on first access rather than per agent.
        """
        from aimu.skills import build_skills_server
        from aimu.tools import MCPClient

        manager = self.all_skills
        # Held for the lifetime of this state: the callables close over the client, and dropping the
        # last reference would close the portal underneath them.
        # Held until close(): the callables close over this client, and it owns a blocking portal that
        # must be released explicitly. Left to garbage collection it is released during interpreter
        # finalization, where stopping the portal cannot complete and blocks the process from exiting.
        self._skills_mcp_client = MCPClient(server=build_skills_server(manager, env=self._script_env()))
        by_name = {fn.__name__: fn for fn in self._skills_mcp_client.as_tools()}
        activate = [by_name["activate_skill"]] if "activate_skill" in by_name else []
        return {
            skill.name: activate + [by_name[tool] for tool in skill.script_tool_names() if tool in by_name]
            for skill in manager.skills.values()
        }

    def _script_env(self) -> dict[str, str]:
        """The host context every skill script gets, from this config.

        A script cannot discover where Kokua serves downloads from or which address it may mail, and
        deriving it would mean re-implementing the config and path resolution in ``config/paths.py``
        and drifting from it. Passing it means one source of truth stays one.

        ``KOKUA_EMAIL_PASSWORD`` is deliberately absent: it is already in this process's environment,
        which a subprocess inherits, so copying it here would duplicate a secret for no gain. Only
        settings that come from ``config.toml`` are passed.
        """
        env = {
            "KOKUA_DOWNLOADS_DIR": str(self.config.downloads_path),
            "KOKUA_IMAGES_DIR": str(self.config.images_path),
            "KOKUA_EMAIL_PORT": str(self.config.email_port),
            "KOKUA_EMAIL_USE_SSL": "1" if self.config.email_use_ssl else "0",
        }
        for key, value in (
            ("KOKUA_EMAIL_HOST", self.config.email_host),
            ("KOKUA_EMAIL_TO", self.config.email_to),
            ("KOKUA_EMAIL_USERNAME", self.config.email_username),
            ("KOKUA_EMAIL_FROM", self.config.email_from),
        ):
            if value:
                env[key] = value
        return env

    def close(self) -> None:
        """Release what this state owns that outlives garbage collection. Idempotent.

        Only the skills MCP client needs it: it owns a blocking portal on a background thread, and a
        portal released during interpreter finalization cannot be stopped, so the process never exits.
        Everything else here is either a plain object or holds no OS resource of its own.
        """
        client = getattr(self, "_skills_mcp_client", None)
        if client is not None:
            self._skills_mcp_client = None
            client.close()

    @cached_property
    def tasks(self) -> Any:
        """The scheduled-task lifecycle, shared by the ``scheduling`` toolset and the web task panel.

        One instance per process: it pairs each config write with the matching scheduler (un)arming,
        so two of them over one config file would let the panel disable a task the agent's copy keeps
        firing. The assistant reaches for ``arm_all`` at boot whether or not any agent declares the
        scheduling toolset, since a persisted task must still fire.
        """
        from kokua.scheduling import TaskService
        from kokua.toolsets.scheduling import DEFAULT_MAX_TASK_CONVERSATIONS

        return TaskService(
            self.scheduler,
            self.config.config_path,
            self.proactive,
            # A lambda rather than the value: read at fire time, so a change to the setting reaches the
            # next firing. The fallback covers a config built without ``resolve_config``, which is the
            # only thing that seeds a section for every declared toolset.
            default_max_conversations=lambda: self.config.toolset_settings.get("scheduling", {}).get(
                "max_task_conversations", DEFAULT_MAX_TASK_CONVERSATIONS
            ),
            stop_run=self.stop_task_runs,
            rename_conversations=(
                (lambda old, new: self.conversation_book.retag_task(old, new)) if self.conversation_book else None
            ),
        )


@dataclass(frozen=True)
class ToolsetContext:
    """One agent's view of ``LiveState``, passed to every ``Toolset.build``."""

    state: LiveState
    agent: Any

    @property
    def config(self) -> AssistantConfig:
        return self.state.config
