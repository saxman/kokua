"""A built-in toolset that mounts AIMU's prebuilt orchestrator agents as tools.

This is the worked example of wiring an agent built with AIMU into Kokua. Any `Runner` -- an `Agent`,
a `Chain`, a `Router`, an `OrchestratorAgent`, a remote A2A agent -- exposes `.run(task) -> str`, so a
toolset is all the bridge that is needed, and the core does not have to learn about it. Copy this
shape for your own agent: register a `Toolset` under the `kokua.toolsets` entry-point group, and name it
in an agent's `tools` list in config.toml's `[agents.<name>]` table to scope that agent to it.

Three tools, one per prebuilt: `code_review`, `research_report`, `create_content`. Each prebuilt is an
orchestrator that fans a task out to three specialist workers of its own and synthesizes their answers.

Two caveats worth knowing before leaning on these:

- **They are synchronous.** AIMU's prebuilts use the sync `ModelClient`, so the async agent dispatches
  them via `asyncio.to_thread`, exactly as it does the built-in tool groups. The nested run therefore
  gets no `SubagentObserver` streaming (the web sub-agent card stays empty for it), cannot be cancelled
  by `/stop`, and its workers run without the approval gate. That last one is harmless in practice --
  the only tools any of these workers get are `builtin.web`, none of which are in the default
  `confirm_tools` -- but it would matter if you gave them `compute`.
- **They overlap `spawn_subagent`.** An agent declaring `fs` + `compute` is a stronger code reviewer
  than `CodeReviewAgent`, whose workers have no tools at all. Reach for these as an illustration of the
  wiring, not because they beat a configured agent.
"""

from __future__ import annotations

from typing import Callable

import aimu
from aimu.agents.prebuilt import CodeReviewAgent, ContentCreationAgent, ResearchReportAgent
from aimu.tools import builtin, tool

from kokua.config import AssistantConfig
from kokua.toolsets import Toolset


def _research_worker_tools() -> list[Callable]:
    """Web tools for `ResearchReportAgent`'s workers, given unconditionally.

    `ResearchReportAgent` is the one prebuilt that takes worker tools, and a research agent without
    them is reduced to reciting training data. There is no longer a global tool policy that could cap
    this: an agent's capability is exactly what its ``tools`` table declares, so naming `aimu_agents`
    there is itself the consent for its research workers to reach the web. (Gating this on whether the
    declaring agent also holds the `web` toolset was considered and rejected: a worker's ``build``
    receives ``agent=None``, so the declaring agent's name is not reachable from the context.) Passing
    tools also switches the prebuilt's own workers to concurrent dispatch.
    """
    return list(builtin.web)


def build(config: AssistantConfig) -> list:
    """Return this toolset's tools: one per AIMU prebuilt orchestrator.

    Each tool constructs its agent inside the call rather than here, for two reasons. ``build`` runs
    once per conversation agent and every prebuilt creates one orchestrator client plus three worker
    clients, so building eagerly would mean twelve model clients per conversation -- and on an
    in-process provider (HuggingFace, LlamaCpp) constructing a client is what loads weights. Building
    per call also keeps each run isolated: a cached orchestrator's ``ModelClient.messages`` is shared
    mutable state, so two concurrent calls would interleave into one history.

    A prebuilt runs on ``config.default_model``, the default every agent falls back to, rather than on
    the declaring agent's own ``[agents.*].model``: an orchestrator AIMU builds is not one of Kokua's
    agents, and the context a toolset builds from does not name the agent that declared it. That is the
    same string Kokua's own agents are built from, resolved once, even though this client is the sync
    one: a prebuilt running on a different model than everything else because it resolved its own
    default separately is the surprise this avoids.
    """

    def run_prebuilt(agent_class, task: str, **kwargs) -> str:
        # A tool that raises breaks the agent's tool loop, so an unresolvable model comes back as text.
        try:
            client = aimu.client(config.default_model)
        except (ValueError, TypeError) as e:
            return f"Could not start the {agent_class.__name__}: {e}"
        return agent_class(client, **kwargs).run(task)

    @tool
    def code_review(code: str) -> str:
        """Review code for security, performance, and readability, and return a prioritized report.

        Args:
            code: The full source to review.
        """
        return run_prebuilt(CodeReviewAgent, code)

    @tool
    def research_report(topic: str) -> str:
        """Research a topic from several angles and return a structured report.

        Covers background, real-world examples, and counterarguments.

        Args:
            topic: The subject to research.
        """
        return run_prebuilt(ResearchReportAgent, topic, worker_tools=_research_worker_tools())

    @tool
    def create_content(brief: str) -> str:
        """Research, outline, and draft a piece of written content from a brief.

        Args:
            brief: What to write, including audience and purpose.
        """
        return run_prebuilt(ContentCreationAgent, brief)

    return [code_review, research_report, create_content]


TOOLSET = Toolset(
    name="aimu_agents",
    description="AIMU's prebuilt orchestrator agents (code review, research report, content creation) as tools.",
    build=lambda ctx: build(ctx.config),
)
