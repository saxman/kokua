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
    # AIMU built-in tool groups to expose (see assistant._TOOL_GROUPS; "all"/"none" also accepted).
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
    def history_path(self) -> str:
        return str(self.data_dir / "history.json")

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
    def mcp_servers_path(self) -> Path:
        """Where runtime-added MCP servers are recorded so they reconnect across restarts."""
        return self.data_dir / "mcp-servers.json"

    @property
    def runtime_settings_path(self) -> Path:
        """Where the web settings panel persists runtime model settings across restarts."""
        return self.data_dir / "runtime-settings.json"
