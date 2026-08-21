"""Assistant configuration and the default prompts.

`AssistantConfig` is plain data describing one assistant: which model, where its state lives,
which agents exist and what each one holds, which MCP servers to load, and how it presents itself.
The CLI builds one of these from flags; tests build them directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from kokua.config import paths as paths

DEFAULT_SYSTEM_MESSAGE = "You are a personal assistant running on the user's own machine. Be concise and helpful."


@dataclass
class AgentConfig:
    """One agent, declared whole in ``config.toml``.

    ``tools`` names toolsets in the single registry namespace, so an entry may be an AIMU built-in
    group, a plugin, or a configured MCP server, and the agent does not say which. A non-empty
    ``delegates_to`` is what makes this agent a delegator: there is no separate switch that could
    disagree with it.

    ``model`` overrides ``[assistant].model`` for this agent alone; unset, the agent runs on that
    default (see ``AssistantConfig.model_for``). ``thinking`` does the same for reasoning effort
    (see ``AssistantConfig.thinking_for``), and is ``None`` rather than ``False`` when unset because
    ``False`` is itself a declaration -- "do not reason" -- that must be able to override a default.

    ``generation`` overrides ``[assistant.generation]`` *per key* (see ``AssistantConfig.generation_for``),
    so an agent that wants only a colder temperature keeps the default's context length.
    """

    description: str = ""
    system_message: str = ""
    model: Optional[str] = None
    thinking: Optional[Union[bool, str]] = None
    generation: dict = field(default_factory=dict)
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
    # The reasoning effort every agent runs at unless its own [agents.<name>].thinking overrides it.
    # AIMU's four value forms: None emits nothing (its own default), False asks the model not to reason
    # (and selects the model card's instruct-mode sampling profile where it declares one), True reasons
    # at the model's own default effort, and a level string requests that effort. A level is advisory on
    # a model whose card does not declare thinking_levels: AIMU warns once and reasoning is still on.
    thinking: Optional[Union[bool, str]] = None
    # Generation parameters every agent runs with, each overridable per key by an agent's own
    # [agents.<name>.generation] table. Only the keys config.toml declares are here: this dict becomes
    # `client.default_generate_kwargs`, which sits ABOVE the model card in AIMU's precedence chain, so
    # a key filled in with a default would shadow a card's own tuned profile. Empty is the normal case.
    generation: dict = field(default_factory=dict)
    system_message: str = DEFAULT_SYSTEM_MESSAGE
    # Set only by `--system` (never by config.toml, which has no key for it). `system_message` above
    # already has a value whether or not anyone set it -- its own default -- so "was --system passed"
    # cannot be answered by looking at that field; this one is None exactly when it wasn't. Wins over the
    # entry agent's declared `system_message` in assemble_system_message, since a prompt is not the
    # capability this design made [agents.*] the single source of; a worker's declared opener is untouched.
    system_message_override: Optional[str] = None
    # Surface the model's reasoning and tool calls in the channel, not just the final answer.
    show_thinking: bool = True
    show_tools: bool = True
    # Each toolset's own config section, by toolset name: every key the toolset declared, seeded with its
    # declared default and overlaid with what config.toml sets. A toolset (and a workflow) therefore
    # always reads a complete view, and Kokua's own dataclass carries no field for a capability that may
    # not be installed. Deep planning's flags are the first case, and the reason there is no plan_review
    # or review_rounds field above: they are the planning toolset's [planning] section, declared in
    # toolsets.planning.PLANNING_SETTINGS and read by the workflow through its context.
    toolset_settings: dict[str, dict] = field(default_factory=dict)
    # The toolset sections config.toml actually contained, captured before the declared defaults were
    # seeded. After seeding there is a bucket for every declared setting, so this is the only record of
    # which sections the user wrote -- which is what a startup warning about a section no installed
    # toolset owns has to know.
    configured_sections: tuple[str, ...] = ()
    # Remote MCP servers to connect at startup; each may name an env var holding its bearer token.
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    # Email (SMTP send). Recipients are LOCKED to email_to: the send_email tool takes no recipient, so
    # the assistant can only ever email the user. The password is read from KOKUA_EMAIL_PASSWORD (env),
    # never TOML. The email toolset self-gates: it offers no tool unless host + email_to are set and
    # that env var is present.
    email_host: Optional[str] = None
    email_port: int = 587
    email_username: Optional[str] = None  # SMTP login user; falls back to email_from, then email_to
    email_from: Optional[str] = None  # From: header; falls back to email_to
    email_to: Optional[str] = None  # the ONLY recipient the tool will ever send to
    # True -> SMTP_SSL (implicit TLS, usually port 465); False -> plain connect + STARTTLS (usually 587).
    email_use_ssl: bool = False
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
    # The config.toml this assistant reads and writes. The add_mcp_server tool and the assistant's own
    # update_config tool both persist here; set from --config / $KOKUA_CONFIG by the CLI, else the
    # default $KOKUA_HOME/config.toml.
    config_path: Path = field(default_factory=paths.config_path)
    # Tasks declared in [scheduling.task.<name>], keyed by name, validated at startup so a bad table
    # fails here rather than becoming a task that quietly never fires. TaskService does not read this:
    # it re-reads the file on every operation, because a cancel between arming and firing must win over
    # any snapshot.
    scheduled_tasks: dict = field(default_factory=dict)

    def model_for(self, agent_name: str) -> Optional[str]:
        """The model ``agent_name`` runs on: its own declaration, else the ``[assistant].model`` default.

        Resolution is per agent and never inherited down the delegation graph, so a delegator that pins
        a model does not drag its workers onto it: a worker declaring nothing runs on the same default
        every other undeclared agent does. ``None`` here means "no model configured anywhere", which
        AIMU resolves at client construction.
        """
        agent = self.agents.get(agent_name)
        return (agent.model if agent else None) or self.model

    def thinking_for(self, agent_name: str) -> Optional[Union[bool, str]]:
        """The reasoning effort ``agent_name`` runs at: its own declaration, else the ``[assistant]`` default.

        Resolved on ``is None`` rather than truthiness, which ``model_for`` above can afford but this
        cannot: ``thinking = false`` is a real declaration ("do not reason"), and an ``or`` would let a
        ``"high"`` default swallow it. This matches how AIMU's own per-run override tests the argument.

        Per agent and never inherited down the delegation graph, for the same reason the model is not:
        a delegator that reasons hard does not drag its workers up with it.
        """
        agent = self.agents.get(agent_name)
        declared = agent.thinking if agent else None
        return self.thinking if declared is None else declared

    def generation_for(self, agent_name: str) -> dict:
        """The generation parameters ``agent_name`` runs with: ``[assistant.generation]``, per-key
        overridden by its own ``[agents.<name>.generation]``.

        Merged per key rather than table-for-table, so a context length set once at the top still
        applies to an agent that only wanted a colder temperature. Per agent and never inherited down
        the delegation graph, for the reason ``model_for`` and ``thinking_for`` are not: a delegator's
        tuning is not its workers'.

        An empty dict is the normal case, and the invariant the design rests on: **a key absent from
        the file is absent from the request**, which is what leaves a model card's own tuned profile in
        force. This tier sits above that profile in AIMU's precedence chain, so anything defaulted here
        would silently replace a card's recommendation.

        A fresh dict every call: the caller assigns it to a live client's ``default_generate_kwargs``,
        which that client may then mutate.
        """
        agent = self.agents.get(agent_name)
        return {**self.generation, **(agent.generation if agent else {})}

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
    def logs_path(self) -> Path:
        """Directory for the rotating diagnostic log (kokua.log). See logging_setup.configure_logging."""
        return self.data_dir / "logs"
