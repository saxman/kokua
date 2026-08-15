"""Assistant configuration and the default prompts.

`AssistantConfig` is plain data describing one assistant: which model, where its state lives,
which agents exist and what each one holds, which MCP servers to load, and how it presents itself.
The CLI builds one of these from flags; tests build them directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from kokua.config import paths as paths

DEFAULT_SYSTEM_MESSAGE = "You are a personal assistant running on the user's own machine. Be concise and helpful."


@dataclass
class AgentConfig:
    """One agent, declared whole in ``config.toml``.

    ``tools`` names toolsets in the single registry namespace, so an entry may be an AIMU built-in
    group, a plugin, or a configured MCP server, and the agent does not say which. A non-empty
    ``delegates_to`` is what makes this agent a delegator: there is no separate switch that could
    disagree with it.
    """

    description: str = ""
    system_message: str = ""
    tools: list[str] = field(default_factory=list)
    delegates_to: list[str] = field(default_factory=list)


@dataclass
class MCPServerConfig:
    """A remote MCP server to connect at startup.

    ``name`` is how the server enters the toolset namespace, so an agent declares it exactly as it
    declares any other capability. It is required: a server no agent can name reaches no agent.

    ``token_env`` names an environment variable holding a bearer token, resolved at connect time so
    the secret stays out of the config file. It is unset for an unauthenticated server, or one using
    the OAuth flow that triggers on an auth challenge.
    """

    url: str
    name: str
    token_env: Optional[str] = None


@dataclass
class AssistantConfig:
    model: Optional[str] = None
    system_message: str = DEFAULT_SYSTEM_MESSAGE
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
    # Remote MCP servers to connect at startup; each may name an env var holding its bearer token.
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    # Email (SMTP send). Recipients are LOCKED to email_to: the send_email tool takes no recipient, so
    # the assistant can only ever email the user. The password is read from KOKUA_EMAIL_PASSWORD (env),
    # never TOML. The email tool-pack self-gates: it offers no tool unless host + email_to are set and
    # that env var is present.
    email_host: Optional[str] = None
    email_port: int = 587
    email_username: Optional[str] = None  # SMTP login user; falls back to email_from, then email_to
    email_from: Optional[str] = None  # From: header; falls back to email_to
    email_to: Optional[str] = None  # the ONLY recipient the tool will ever send to
    # True -> SMTP_SSL (implicit TLS, usually port 465); False -> plain connect + STARTTLS (usually 587).
    email_use_ssl: bool = False
    # Model generation kwargs from the [generation] TOML section, layered over the provider's built-in
    # defaults. The web settings panel and update_config change these at runtime by writing back to
    # config.toml, so this is the single effective layer. Kept as a plain dict so provider-specific keys
    # pass through untouched.
    generation: dict = field(default_factory=dict)
    # Load toolset plugins discovered via the "kokua.toolsets" entry-point group.
    load_plugins: bool = True
    # Every agent, keyed by name, read whole from [agents.*]. Nothing is defaulted in code: an agent's
    # capability is exactly what its table declares, and a capability no agent names reaches nothing.
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    # The agent the user talks to, and the root of the delegation graph.
    entry_agent: str = "assistant"
    # Run independent tool calls in one turn concurrently, so several delegations overlap.
    concurrent_tools: bool = True
    # Tools that require interactive confirmation before each call (see assistant._approve). These
    # run with full machine access; an empty list disables approval. Proactive turns auto-deny them.
    confirm_tools: list[str] = field(
        default_factory=lambda: ["add_skill_script", "add_mcp_server", "execute_python", "update_config"]
    )
    # Front end to run and, for the web front end, its bind address.
    frontend: str = "cli"
    host: str = "127.0.0.1"
    port: int = 8000
    # Logging level for the rotating file log under logs_path (configure_logging in logging_setup.py).
    log_level: str = "INFO"
    # Max per-conversation agents kept live in memory (AgentRegistry LRU cap). Evicted agents rebuild
    # from persisted state on next access, so the cap bounds memory, not correctness.
    agent_cache_cap: int = 8
    # Single root for all transient and user-provided content; the leaf paths below derive from it.
    data_dir: Path = field(default_factory=paths.data_dir)
    # The config.toml this assistant reads and writes. The web settings panel, the add_mcp_server tool,
    # and the assistant's own update_config tool all persist here; set from --config / $KOKUA_CONFIG by
    # the CLI, else the default $KOKUA_HOME/config.toml.
    config_path: Path = field(default_factory=paths.config_path)

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
    def scheduled_tasks_path(self) -> Path:
        return self.data_dir / "scheduled_tasks.json"

    @property
    def logs_path(self) -> Path:
        """Directory for the rotating diagnostic log (kokua.log). See logging_setup.configure_logging."""
        return self.data_dir / "logs"
