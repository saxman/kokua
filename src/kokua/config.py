"""Assistant configuration and the default prompts.

`AssistantConfig` is plain data describing one assistant: which model, where its state lives,
which tool groups and MCP servers to load, whether memory is on, and how it presents itself.
The CLI builds one of these from flags; tests build them directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import paths

DEFAULT_SYSTEM_MESSAGE = (
    "You are a personal assistant running on the user's own machine. Be concise and helpful. "
    "When the user teaches you a repeatable procedure worth remembering, call `author_skill` to save "
    "it as a reusable skill; name skills in kebab-case (lowercase words joined by hyphens, e.g. "
    "'weekly-review'), never with underscores or spaces. When a procedure can be automated, call "
    "`add_skill_script` to attach a runnable Python or shell script to a skill; the script becomes a "
    "tool you can run immediately, even in the same turn. If a script fails, fix it by calling "
    "`add_skill_script` again with the SAME filename to overwrite it (a different filename just "
    "creates a duplicate and leaves the broken script). Scripts run with full access to this "
    "machine, so only automate what the user asked for."
)

# Appended to the system message when sub-agents are enabled, so the model actually delegates (without
# explicit direction the tool sits unused). Each spawn is a fresh, context-free agent, so the guidance
# stresses giving it a complete, self-contained task.
SUBAGENT_GUIDANCE = (
    " For a request that splits into independent subtasks, call `spawn_subagent` to delegate each to a "
    "fresh sub-agent with its own isolated context, then synthesize their answers. A sub-agent shares no "
    "history with you, so give it a complete, self-contained task. Emit several `spawn_subagent` calls "
    "when subtasks are independent. Do the work yourself when it is small or must build on the "
    "conversation so far."
)

# Built-in sub-agent roles (AIMU agent_types). Each role's tools are its groups intersected with the
# assistant's enabled tool groups (see assistant._build_subagent_tool), so a role never grants a tool
# the user disabled globally. `description` becomes the role's menu line shown to the model; the
# `system_message` body guides the sub-agent. Users override or extend these via [subagents.roles.*].
DEFAULT_SUBAGENT_ROLES: dict[str, dict] = {
    "researcher": {
        "description": "Research specialist: gather and verify information from the web.",
        "groups": ["web", "misc"],
        "system_message": (
            "You are a research sub-agent. Investigate the task with web search and page lookups, "
            "verify claims against sources rather than memory, and return a concise, well-organized "
            "findings summary that names its sources."
        ),
    },
    "coder": {
        "description": "Coding specialist: read/write files and run code to complete a task.",
        "groups": ["fs", "compute"],
        "system_message": (
            "You are a coding sub-agent. Complete the task by reading and writing files and running "
            "code (Python, shell, or calculations). Report exactly what you did and the concrete result."
        ),
    },
    "generalist": {
        "description": "General-purpose helper with the full built-in toolset; use when no specialist fits.",
        "groups": ["web", "fs", "compute", "misc"],
        "system_message": (
            "You are a general-purpose sub-agent. Complete the self-contained task you are given and "
            "return a single, complete answer."
        ),
    },
}

DEFAULT_REMINDER_TEXT = "Proactively check in with the user with one short, useful suggestion for their day."

# Appended to the system message when memory is enabled, so the model actually uses the two stores
# (without explicit direction the tools sit unused). Two distinct stores: short facts about the user
# (semantic recall) vs. longer reference documents.
MEMORY_GUIDANCE = (
    " You have a persistent memory across conversations. When the user shares a durable fact about "
    "themselves or a preference worth remembering, call `store_memory` to save it, and call "
    "`search_memories` to recall such facts when they would help. For longer reference material the user "
    "provides (notes, documents), call `save_document` with a descriptive path and `search_documents` to "
    "find relevant passages later. Do not store transient chit-chat."
)


@dataclass
class AssistantConfig:
    model: Optional[str] = None
    system_message: str = DEFAULT_SYSTEM_MESSAGE
    reminder_seconds: Optional[float] = None
    reminder_text: str = DEFAULT_REMINDER_TEXT
    # Surface the model's reasoning and tool calls in the channel, not just the final answer.
    show_thinking: bool = True
    show_tools: bool = True
    # Deep planning is invoked per request (the web UI's Plan toggle or a "/plan <task>" message): the
    # turn first produces an explicit plan (tools/skills/MCP to use or build) before executing.
    # plan_review gates execution on the user's Approve/Edit/Reject; off is autonomous (plan shown, then
    # it proceeds).
    plan_review: bool = False
    # Adversarial review (deep planning). plan_review_agent: an independent, context-free agent critiques
    # the plan and Kokua re-plans on rejection. result_review: an independent agent checks the final answer
    # before it is shown (the loop still streams; only the final answer is withheld) and revises on reject.
    # review_rounds bounds each replan/revise loop.
    plan_review_agent: bool = False
    result_review: bool = False
    review_rounds: int = 2
    # Verbose trace (deep planning): stream every LLM call in a planned turn -- planner, each reviewer
    # (prose reasoning + verdict), executor, and every revision -- under labeled phase headers, showing
    # every intermediate version. Overrides result_review's "hide until vetted" gate. Off by default.
    show_reasoning: bool = False
    # AIMU built-in tool groups to expose (see build._TOOL_GROUPS; "all"/"none" also accepted).
    tools: list[str] = field(default_factory=lambda: ["web", "fs", "compute", "misc"])
    # Remote MCP server URLs to connect at startup; a bearer token (if set) is applied to all.
    mcp_servers: list[str] = field(default_factory=list)
    mcp_bearer: Optional[str] = None
    # Baseline model generation kwargs seeded from the [generation] TOML section. At runtime the web
    # settings panel overrides these (persisted separately to runtime-settings.json); see the
    # layering note there. Kept as a plain dict so provider-specific keys pass through untouched.
    generation: dict = field(default_factory=dict)
    # Persistent memory: a SemanticMemoryStore for facts + a DocumentStore for documents. On by default.
    memory: bool = True
    # Load tool-pack plugins discovered via the "kokua.tools" entry-point group.
    load_plugins: bool = True
    # Sub-agents: give the assistant a spawn_subagent tool so it can delegate an independent subtask to
    # a fresh, isolated agent (its own context/history) and get back a single answer. On by default.
    subagents: bool = True
    # Sub-agent roles (AIMU agent_types). User definitions here merge over DEFAULT_SUBAGENT_ROLES by
    # name (override an existing role or add a new one); empty means "just the defaults".
    subagent_roles: dict[str, dict] = field(default_factory=dict)
    # Run independent tool calls in one turn concurrently (so several spawn_subagent calls overlap).
    subagents_concurrent: bool = True
    # Tools that require interactive confirmation before each call (see assistant._approve). These
    # run with full machine access; an empty list disables approval. Proactive turns auto-deny them.
    confirm_tools: list[str] = field(default_factory=lambda: ["add_skill_script", "add_mcp_server", "execute_python"])
    # Front end to run and, for the web front end, its bind address.
    frontend: str = "cli"
    host: str = "127.0.0.1"
    port: int = 8000
    # Single root for all transient and user-provided content; the leaf paths below derive from it.
    data_dir: Path = field(default_factory=paths.data_dir)

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def sessions_path(self) -> Path:
        return self.data_dir / "sessions.json"

    @property
    def memory_path(self) -> Path:
        return self.data_dir / "memory"

    @property
    def documents_path(self) -> Path:
        return self.data_dir / "documents"

    @property
    def downloads_path(self) -> Path:
        """Generated binary artifacts (e.g. PDFs) the web UI serves at /download. Kept out of
        ``documents_path`` because the DocumentStore scans that folder as UTF-8 text at startup."""
        return self.data_dir / "downloads"

    @property
    def images_path(self) -> Path:
        """Uploaded and generated images the web UI serves at /images. Sessions store only a
        ``/images/<name>`` reference into this folder (never inline base64), so ``sessions.json`` stays
        small; the bytes are re-read here and base64-inlined only when a turn is sent to the model. Kept
        out of ``documents_path`` because the DocumentStore scans that folder as UTF-8 text at startup."""
        return self.data_dir / "images"

    @property
    def mcp_servers_path(self) -> Path:
        """Where runtime-added MCP servers are recorded so they reconnect across restarts."""
        return self.data_dir / "mcp-servers.json"

    @property
    def runtime_settings_path(self) -> Path:
        """Where the web settings panel persists runtime model settings across restarts."""
        return self.data_dir / "runtime-settings.json"
