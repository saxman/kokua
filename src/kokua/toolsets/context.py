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
    tool_approval: Optional[Callable] = None
    observer: Optional[SubagentObserver] = None
    registry: dict = field(default_factory=dict)
    # Rebuilds one agent's delegate after a runtime MCP change. Assigned by the composition root rather
    # than imported by the toolsets that need it, since it lives in core.build and core.build imports
    # them.
    refresh_workers: Optional[Callable[[Any], None]] = None

    @cached_property
    def memory_store(self) -> SemanticMemoryStore:
        return SemanticMemoryStore(persist_path=str(self.config.memory_path))

    @cached_property
    def document_store(self) -> DocumentStore:
        return DocumentStore(persist_path=str(self.config.documents_path))

    @cached_property
    def skill_manager(self) -> SkillManager:
        """One SkillManager shared by every conversation's agent -- deliberately, the same reasoning
        ``memory_store`` and ``document_store`` apply above. Skills are files in one user-owned
        directory, and a skill the user teaches in one conversation should be usable in every other one,
        not just the conversation that happened to author it. A manager per agent was the accident this
        replaces: it gave each conversation its own catalog cache, so a skill authored in one was
        invisible to another until that one happened to refresh on its own.

        Turns on different conversations run concurrently, and both ``author_skill`` and
        ``add_skill_script`` call ``manager.refresh()`` on this one shared instance. That is safe without
        a lock: ``refresh()`` only invalidates the cached catalog, and the next read rebuilds it by
        re-scanning the skills directory on disk, which is idempotent. Two concurrent refreshes therefore
        race at worst to assign an equivalent map, never a torn or corrupt one.
        """
        return SkillManager(skill_dirs=[str(self.config.skills_dir)])

    @cached_property
    def _scheduling(self) -> tuple[list, Callable[[], None], Any]:
        """The scheduler tools, ``arm_all``, and the front-end task controls, built together.

        ``make_scheduler_tools`` returns all three from one closure over the live scheduler, so they
        cannot be built separately. The assistant needs ``arm_tasks`` at boot whether or not any agent
        declares the scheduling toolset, since a persisted task must still fire.
        """
        from kokua.scheduling import make_scheduler_tools

        return make_scheduler_tools(self.scheduler, self.config.scheduled_tasks_path, self.proactive)

    @property
    def scheduler_tools(self) -> list:
        return self._scheduling[0]

    @property
    def arm_tasks(self) -> Callable[[], None]:
        return self._scheduling[1]

    @property
    def task_controls(self) -> Any:
        return self._scheduling[2]


@dataclass(frozen=True)
class ToolsetContext:
    """One agent's view of ``LiveState``, passed to every ``Toolset.build``."""

    state: LiveState
    agent: Any

    @property
    def config(self) -> AssistantConfig:
        return self.state.config
