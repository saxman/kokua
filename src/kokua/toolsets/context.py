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
from typing import Any, Callable, Optional

from aimu.memory import DocumentStore, SemanticMemoryStore
from aimu.skills import SkillManager

from kokua.config.schema import AssistantConfig


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
    scheduler: Optional[Any] = None
    proactive: Optional[Callable] = None
    conversation_book: Optional[Any] = None
    turn_running: Optional[Callable[[str], bool]] = None
    tool_approval: Optional[Callable] = None
    observer: Optional[Any] = None
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
