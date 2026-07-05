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
- **Distinguish agent-loop turns from user input (web)**: the agent loop injects its own continuation
  turns as `user`-role messages; using AIMU's inert `provenance` message key, the web UI now renders these
  as a muted `↻ continuation` marker at each loop-iteration boundary instead of as user bubbles, both live
  (keyed off `StreamChunk.iteration`) and on history replay. The marker shows the injected prompt text for
  inspection. Proactive turns are tagged and replay with their existing amber styling. Real user turns are
  unaffected.
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
- **Markdown-to-PDF tool**: a built-in `pdf` tool-pack contributes `markdown_to_pdf`, which renders
  Markdown to a PDF (via `fpdf2` + `markdown`, both pure-Python, no system libraries) saved in
  `data/downloads/`. Enabled by default like any tool-pack. The web front end serves that folder at
  `GET /download/<name>`, so the assistant can hand back a download link; the tool also returns the
  absolute path for the CLI. (Downloads live in their own folder, not `data/documents/`, so the binary
  PDFs never disturb the DocumentStore, which scans the documents folder as text.)
- **Deep planning mode**: when on, a turn first drafts an explicit plan (which tools/skills/MCP services
  to use, what to web-search for, and where to build a skill via `author_skill` or connect a server via
  `add_mcp_server`) and then executes it. `plan_review` pauses for Approve / Edit / Reject; off runs the
  plan autonomously. Toggle it in the settings panel or `[planning]` config, or invoke it for one request
  with `/plan <task>`. Built on Kokua's existing turn loop and tool-approval round-trip (AIMU already makes
  the agent plan-capable); planning is scratch work kept out of the saved conversation, which stores your
  actual request and the answer. `web-search`-for-MCP relies on the default `web` tools.
- **Adversarial plan + result review** (deep planning, both off by default): an independent, context-free
  reviewer agent (fresh client, sees only the request + plan/answer) critiques the plan and/or the final
  result. `plan_review_agent` re-plans on rejection up to `review_rounds`, surfacing leftover concerns (to
  the human gate when it's on, else noted with the plan). `result_review` checks the answer before it's
  shown and revises on rejection; because a result can't be vetted and streamed at once, it runs the
  executor non-streamed and commits a clean transcript. Reuses AIMU's structured output
  (`client.chat(schema=Verdict, use_tools=False)`) with no AIMU change; toggles in the settings panel and
  the `[planning]` config section.
- **App-owned state**: all state under `~/.kokua` (override `KOKUA_HOME`), replacing the example's reliance
  on `aimu.paths.output`.
- **Tests**: mock-only suite (assistant wiring, CLI parsing, MCP, memory, web channel + server round-trip,
  plugin discovery), with a vendored async mock model client (no reach into the AIMU repo).
