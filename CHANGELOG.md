# Changelog

## 0.1.0 (unreleased)

Initial release. Kokua starts from AIMU's `examples/personal-assistant/` and restructures it into an
installable, modular application.

- **Package**: `src`-layout `kokua` package with console scripts `kokua` (runs the selected front end) and
  `kokua-web`. Apache-2.0, Python 3.11+.
- **Assistant core** (`kokua.assistant`): the transport-agnostic `Assistant` wiring an AIMU `SkillAgent`
  with skill authoring + runnable skill scripts, persistent conversation history, a proactive scheduler,
  remote MCP servers (startup `--mcp` + runtime `add_mcp_server`), and persistent memory (a
  `SemanticMemoryStore` for facts + a `DocumentStore` for documents, on by default).
- **Plugin system** (`kokua.plugins`): front ends and tool-packs discovered via the `kokua.frontends` and
  `kokua.tools` entry-point groups. Built-in `cli` and `web` front ends and an `example` tool-pack are
  registered as plugins; third parties add their own by publishing a package. `--list-frontends`,
  `--list-tool-packs`, `--no-plugins`.
- **Front ends**: `cli` (terminal via AIMU's `CLIChannel`) and `web` (Starlette + uvicorn WebSocket server
  with a streaming browser UI, behind the `web` extra). Reloading the web page replays the prior
  conversation (user messages, answers, and reasoning/tool calls when `show_thinking` / `show_tools` are
  on); the assistant already restored its context across reconnects, this makes it visible. Assistant
  replies render as GitHub-flavored markdown when a turn completes (tables, nested lists, code, task
  lists, strikethrough, links), via vendored `marked` + `DOMPurify` (bundled in `web_static/`, served
  locally, no CDN); the rendered HTML is sanitized so model/tool output cannot inject scripts or markup,
  and links open with `rel="noopener"`. Light and dark themes: a theme selector in the settings panel
  (auto / light / dark; auto follows the OS preference) sets a per-browser choice remembered locally and
  applied before first paint (no flash on load, no new dependencies).
- **Multiple web conversations**: the web UI lists conversations in a sidebar (auto-titled from the
  first message) and lets you start a new one or select an existing one to continue, backed by AIMU's
  `sessions` store. Memory stays shared across conversations. An existing single-conversation
  `history.json` is imported once as the first conversation. CLI multi-conversation is a later change.
- **Stop an in-flight reply**: send `/stop` (the web UI also has a Stop button, enabled only while a reply
  is being processed) to cancel the current turn;
  the partial turn is kept so the conversation can continue. Built on AIMU's `aio.RunHandle`; reactive
  turns run as background tasks so the channel keeps reading mid-turn.
- **Tool approval**: configured "risky" tools require confirmation before each call (terminal `y/N` or
  web Allow/Deny), built on AIMU's `ToolApproval` gate. Default set `add_skill_script`, `add_mcp_server`,
  `execute_python`; configurable via `[security] confirm_tools` / `--confirm-tools` (empty disables).
  Proactive turns auto-deny gated tools. The reply is routed through the single channel reader, so it is
  safe alongside `/stop`.
- **Web settings panel**: a gear button in the web header opens a panel to change, at runtime, the model
  generation kwargs (`temperature`, `max_tokens`, `top_p`, `top_k`, `presence_penalty`,
  `repetition_penalty`), display prefs (`show_thinking` / `show_tools` plus the auto/light/dark theme),
  and the active model. Server-backed changes take effect on the next turn (switching the model rebuilds
  the client and carries the conversation over) and persist across restarts to
  `data/runtime-settings.json`, layered over the optional `[generation]` config section
  (`provider defaults < config.toml < the panel`); `config.toml` is never rewritten by the app. The theme
  is a per-browser choice (stored locally, not server-side). Provider support varies: thinking models
  ignore `top_p`/`top_k` and force `temperature`, and Anthropic does not support the penalty parameters.
- **App-owned state**: all state under `~/.kokua` (override `KOKUA_HOME`), replacing the example's reliance
  on `aimu.paths.output`.
- **Tests**: mock-only suite (assistant wiring, CLI parsing, MCP, memory, web channel + server round-trip,
  plugin discovery), with a vendored async mock model client (no reach into the AIMU repo).
