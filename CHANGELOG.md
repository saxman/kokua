# Changelog

## 0.1.0 (unreleased)

First release. Kokua starts from AIMU's `examples/personal-assistant/` and restructures it into an
installable, modular application: a small transport-agnostic core with capability pushed into plugins.
Because there is no earlier release, this section describes what 0.1.0 *is* rather than what changed.
The pre-release development history is in the git log.

Requires Python 3.11+ and [AIMU](https://github.com/saxman/aimu) 0.13.1 or newer. Apache-2.0.

### Package and entry points

- `src`-layout `kokua` package with two console scripts: `kokua` runs the selected front end (default
  `cli`), `kokua-web` is a convenience for `kokua --frontend web`. Both route through `kokua.cli`, so
  both run the AIMU preflight described under [Diagnostics](#diagnostics-and-error-reporting).
- **Plugin system** (`kokua.plugins`). Front ends and toolsets are discovered through the
  `kokua.frontends` and `kokua.toolsets` entry-point groups. The built-in `cli` / `web` front ends and
  the five built-in toolsets register exactly as a third party's package would, so if the built-in path
  and the plugin path ever diverge, the plugin path is the broken one. Inspect with `--list-frontends`
  and `--list-toolsets`; disable discovery with `--no-plugins`.
- The stable public import surface is `kokua.plugins`, `kokua.config`, `kokua.core`,
  `kokua.channels.web`, and `kokua.images`. Everything else is internal and may move.

### Conversations and turns

- **Per-conversation agents.** Each conversation owns its AIMU `SkillAgent` and model client, built
  lazily and held in a bounded LRU registry (`agent_cache_cap`, default 8). Memory and documents stay
  shared across conversations.
- **Concurrent conversations.** A turn keeps running when you switch away: it streams only into the
  conversation you are viewing, persists to its own conversation, and posts a completion notification
  instead of streaming. Switching does not cancel it; only deleting a conversation does, and only its
  own turn. The invariants that make this safe are documented at the top of `core/turns.py`.
- **Switching into a running turn shows the turn.** A turn's messages are not in the store until it
  ends, so its output is recorded as it is produced -- the user bubble, reasoning, tool calls with their
  results, sub-agent cards, images, and the answer so far -- and replayed on switch-in, after which the
  rest streams into the bubble replay left open. The record is dropped once the turn reaches the store,
  so nothing is shown twice. Unattended scheduled turns record the same way, so switching into a running
  task shows the sub-agent work it has done.
- **`/stop`** cancels an in-flight reply and keeps the partial turn so the conversation continues (the
  web UI has a Stop button for the same). Built on AIMU's `aio.RunHandle`; reactive turns run as
  background tasks, so the channel keeps reading mid-turn -- which is also what lets a web approval
  reply reach the waiting tool call.

### Front ends

- **`cli`**: the terminal, via AIMU's `CLIChannel`. `/attach <path>` stages a local image onto the next
  message.
- **`web`** (behind the `web` extra): a Starlette + uvicorn WebSocket server with a streaming browser
  UI, bound to `127.0.0.1:8000` by default. The page is three files: `index.html` for the markup,
  `app.css`, and `app.js`, served from the same static-asset allowlist as the vendored libraries.
  - **A flat transcript, not a chat.** One left-aligned column: the user's turn is marked with a `>`
    rather than filled, prose takes a 76ch measure, and machine events are dim monochrome one-line rows
    set apart by a kind word and an indent. Tables and code blocks run wider than the prose measure,
    since they are scanned rather than read.
  - **Colour means one thing.** Twelve semantic tokens, of which the warm pair marks only a decision
    waiting on you: a tool approval or a plan review. Tool calls, sub-agent cards, and proactive
    messages are not warm. The disclosure triangle is the only glyph on the page; every block names its
    kind in words.
  - **Conversations** are listed in a sidebar, auto-titled from the first message, each row showing its
    relative age, with new / select / delete (deleting the active one switches to the most recently
    updated remaining conversation). The header strip names the conversation being viewed. The sidebar
    collapses to an icon rail and drag-resizes between 180 and 480px; the divider is keyboard-operable.
    Width, collapsed state, and theme are per-browser preferences applied before first paint, so there
    is no flash on load.
  - **The composer is one bordered box** with a text area that grows to eight rows: Enter sends,
    Shift+Enter inserts a newline (an IME's Enter does neither). Send is replaced by Stop for the
    duration of a turn rather than sitting beside a permanently disabled button, and switching
    conversations mid-turn updates it to match the conversation being viewed.
  - **Replies render as GitHub-flavored markdown** on turn completion, via vendored `marked` +
    `DOMPurify` (bundled, served locally, no CDN). The rendered HTML is sanitized, so model and tool
    output cannot inject scripts or markup, and links open with `rel="noopener"`. LaTeX math is typeset
    with vendored KaTeX after sanitization, with `trust:false` and a `maxExpand` cap; a malformed
    expression is left as source text rather than breaking the row.
  - **Auxiliary blocks fold, and the folded line carries the call.** Thinking, tool calls,
    verbose-trace phases, sub-agent cards, drafted plans, and agent-loop continuation markers render
    collapsed behind a one-line header: the kind word, then the call with its condensed arguments, then
    a metric (a result size, a line count, a status). Only the arguments ellipsize, so a row is never
    taller than one line however long they are, and a turn can be read without opening anything. The
    full untruncated arguments are inside the fold. Direct messages and the interactive approval and
    plan-review prompts are unaffected.
  - **A tool card shows what the call returned**, not just what it was asked to do. The result renders
    in a nested foldable labeled with its size (`output  2,148 chars`), filled on first open and
    clamped at 4000 characters with a `show all` control. Always plain text, never markdown, because a
    tool result is untrusted input. An error, an argument-binding failure, and a denied approval are
    results too and arrive the same way, so there is no separate failure rendering. All four surfaces
    that render a tool call carry it -- a live card, a background turn's catch-up replay, a sub-agent
    card's nested calls, and a reloaded transcript -- and replay rejoins a stored call to its result by
    `tool_call_id`, since concurrent dispatch appends results in completion order.
  - **Sub-agent work is visible.** A `spawn_subagent` call renders as
    `subagent  spawn_subagent(<role>)  <status>` over an argument line, then the nested thinking, tool calls,
    and answer the child produced, each individually foldable and built by the same builders and CSS as
    their top-level counterparts. The child's text streams in live and re-renders as markdown when the
    spawn ends. The parent's own duplicate `spawn_subagent` tool block is suppressed. Recording and
    display are independent, so a background turn's spawns are recorded even though its frames are
    muted, and switching in later shows the work.
  - **Rows carry a localized datetime caption**, revealed on hover (full precision in its tooltip) so
    the transcript is not dated line by line. On a foldable it rides the always-visible header, so it
    shows collapsed or expanded. Ephemeral chrome (notices, approval prompts, banners) is not stamped.
    Messages persisted before timestamps existed render without a caption.
  - **Agent-loop continuations are distinguished from user input.** The loop injects its own turns as
    `user`-role messages; these render as a dim `continuation` row showing the injected prompt rather
    than as the user's own turn. A proactive message keeps its uppercase label.
  - **A settings panel** (gear button) changes the model, the generation kwargs (`temperature`,
    `max_tokens`, `top_p`, `top_k`, `presence_penalty`, `repetition_penalty`), the display preferences,
    the planning toggles, and the theme at runtime. Server-backed changes take effect on the next turn
    and persist to `config.toml`; switching the model rebuilds the client and carries the conversation
    over. Provider support varies: thinking models ignore `top_p` / `top_k` and force `temperature`, and
    Anthropic does not support the penalty parameters.
  - Reloading the page replays the prior conversation, including reasoning and tool calls when
    `show_thinking` / `show_tools` are on.

### Agents and tools

- **One agent shape.** Every agent is a lean supervisor: it mounts the cross-cutting tools (skills, MCP
  management, memory, config, scheduling, conversation reads), the `time` group, and `spawn_subagent`,
  and nothing else. Specialized work is delegated to workers whose roles carry the scoped toolsets,
  which keeps the advertised per-request tool set small. Two consequences worth knowing:
  `[tools].groups` is a **ceiling on what workers may draw from**, not the assistant's own toolset, and
  an installed tool-pack or configured MCP server **reaches nothing until some role names it**.
- **Sub-agent roles come from `config.toml` only.** `researcher`, `coder`, and `generalist` ship as
  live tables in `config.example.toml` -- which is what `kokua config init` writes -- and are edited,
  deleted, or added to like any other setting. Because roles exist nowhere else and a supervisor with
  no workers cannot browse, read a file, or compute, **Kokua refuses to start without a config file, or
  with one that defines no roles**, naming `kokua config init` in the error rather than running
  something that looks alive and cannot work.
- **`spawn_subagent(agent_type, task)`** is typed. Each role clones the active model with its own tool
  subset: built-in `groups` intersected with the enabled `[tools].groups`, plus `tool_packs` and
  `mcp_servers` (by a server's optional `name` or its URL), with the parent-only memory / skills / MCP
  management withheld. Independent spawns in one turn run concurrently (`[subagents] concurrent`,
  default on). A worker's gated-tool call is routed to the parent for approval and is never run
  unattended.
- **Every agent can tell the time.** AIMU's clock and timezone converter form a `time` group, in the
  default set (`web,fs,compute,time,misc`). It is mounted on the supervisor whole and added to *every*
  sub-agent role on top of that role's own `groups`, since a worker that cannot resolve "by tomorrow
  morning" is broken whatever its domain. That addition is gated on `time` being in `[tools].groups`,
  so dropping it there still removes it everywhere. Note that a model with a calculator is not the same
  as one that uses it: the supervisor deliberately has no `calculate` / `execute_python` of its own and
  must delegate arithmetic to a worker.
- **The assistant can look across your other conversations.** Three read-only supervisor tools:
  `list_conversations` (ids, last-active times, message counts, titles), `read_conversation` (one
  transcript as plain text), and `search_conversations` (case-insensitive text across every saved
  conversation, with snippets). So "what did we decide about the deployment last week?" is answerable,
  and context from a past thread can be carried into a `spawn_subagent` task. They are supervisor-only,
  like memory and skills: a worker shares no history and has no conversation identity.

  Reads come from the last persisted snapshot, never a rebuilt agent -- building one to read it would
  allocate a model client, re-expand every stored image, and evict live agents from the LRU cache, and
  it would be *less* correct, since a running turn appends to its message list in place. Two markers
  keep the snapshot honest instead: a conversation with an in-flight turn is flagged as having unsaved
  messages, and the conversation you are in says so, since its current turn is not persisted yet and
  the model already has it in context. A transcript holds only what was said, with reasoning and tool
  calls left out and each image as `[image]`, one long message cut on its own so a pasted document
  cannot swallow a read, and the whole bounded by a `max_chars` the model can raise; when it does not
  fit, the *oldest* messages are dropped behind a count, so a read always ends with the most recent
  message. Search matches the phrase first and falls back to all-of-these-words within one message,
  saying which it used.
- **Agent tools are findable.** A module defining an `@aimu.tool` is either a subsystem's `tools.py`
  (`core/`, `config/`, `mcp/`, `scheduling/`) or a tool-pack under `toolpacks/`, so
  `grep -rl '@tool' src/kokua/` finds exactly those. Because about half the supervisor's 27 tools come
  from AIMU and cannot be grepped here at all,
  [docs/explanation/architecture.md](docs/explanation/architecture.md#the-supervisors-tools) carries the
  full inventory with the factory that builds each, and `tests/core/test_build.py` pins it as an
  **exact set**: adding a supervisor tool fails the suite until the table is updated in the same commit,
  and a tool-pack leaking onto the supervisor fails it too, naming the offender.
- **Skills**: AIMU's skill authoring plus runnable skill scripts.
- **Memory**, on by default: a `SemanticMemoryStore` for facts and a `DocumentStore` for documents,
  shared across conversations. Concurrent-turn safety lives inside AIMU's stores (a re-entrant per-store
  lock) rather than a Kokua-side wrapper.

### Built-in tool-packs

- **`pdf`**: `markdown_to_pdf` renders Markdown to a PDF (`fpdf2` + `markdown`, both pure-Python, no
  system libraries) in `data/downloads/`. The web front end serves that folder at
  `GET /download/<name>`, so the assistant can hand back a link; the tool also returns the absolute
  path for the CLI.
- **`image`**: `generate_image`, offered only when `AIMU_IMAGE_MODEL` is set (e.g. `gemini:nano-banana`
  or an `hf:<repo>` diffusers model).
- **`email`**: `send_email` over SMTP (stdlib `smtplib`). The recipient is locked to the configured
  `[email] to` address, so the tool takes no recipient and can only ever email you. The body is written
  in Markdown and delivered as HTML with a plain-text fallback; attachments are limited to files already
  in `data/downloads/` or `data/images/` (traversal-safe). Offered only when `[email] host` and `to` are
  set and `KOKUA_EMAIL_PASSWORD` is present -- the password is never read from the config file. Sending
  is ungated, so a scheduled digest can send.
- **`aimu_agents`**: mounts AIMU's prebuilt `CodeReviewAgent`, `ResearchReportAgent`, and
  `ContentCreationAgent` as the tools `code_review`, `research_report`, and `create_content`. It exists
  mainly as the worked example of wiring an AIMU-built agent into Kokua: every `Runner` exposes
  `.run(task) -> str`, so a tool-pack is the entire bridge and the core gains no new surface. Each call
  builds a fresh agent, reading `config.model` at call time so a runtime model switch reaches it. The
  caveats are documented in the module: the prebuilts are synchronous, so a nested run gets no sub-agent
  card, no `/stop`, and no approval gate on its workers.
- **`example`**: the template for writing your own.

Nothing a pack contributes is mounted until a role asks for it with `tool_packs = [...]`.

### Proactive work: scheduled tasks

- Durable, agent-managed tasks that fire an unprompted turn when due, persisted to
  `data/scheduled_tasks.json` and re-armed at startup. Schedules are one-shot, interval, daily, or
  weekly (no cron dependency).
- Managed by the assistant through `schedule_task`, `list_scheduled_tasks`, `get_scheduled_task`,
  `update_scheduled_task`, `cancel_scheduled_task`, `disable_scheduled_task` / `enable_scheduled_task`
  (pause without losing the task), and `run_scheduled_task` (run one now, reproducing a real firing, so
  a task can be dry-run before it is due).
- `update_scheduled_task` edits any subset of a task's fields in place, keeping its id, `created_at`,
  and dedicated conversation, and re-deriving the schedule fields you omit, so changing a weekly task's
  time keeps its day. It re-arms only when the schedule actually changes, so editing a prompt never
  restarts an interval countdown, and it rejects an invalid schedule, a past one-shot, or a name another
  task holds without writing anything.
- A per-task `target` selects where each firing runs: `active` (the currently-viewed conversation),
  `new` (a fresh conversation per firing), or `task` (one dedicated conversation, created on the first
  firing and reused after, so the task builds on its own history; a deleted one is recreated).
- A failing firing is reported and swallowed rather than propagating into the scheduler, which has no
  handler of its own.
- **A tasks section in the web sidebar**, below the conversation list, showing each task's name,
  schedule, and next firing, with disable/enable, run-now, and delete per row. It hides itself entirely
  when there are no tasks, collapses (remembered per browser), and scrolls independently of the
  conversation list so neither can crowd the other out. Creating and editing tasks stays in chat, where
  the model turns a natural-language schedule into a validated one.
- The panel's actions and the agent's tools share one implementation (`scheduling.TaskControls`), so a
  registry write and the scheduler (un)arming that must accompany it can never come apart. The action
  name arrives from the browser and is allowlisted rather than dispatched on.
- **Each task's conversations are nested under it** in that section and left out of the chat list, so
  the chat list holds only conversations you started. A conversation records the task that minted it
  (`task_id` in its metadata), which is the durable link a task name is not. Grouping happens on the
  page, not in the core: nothing is filtered out of `ConversationBook.list()`, so the agent's
  conversation tools still see every conversation. A conversation whose task has been deleted falls back
  into the chat list rather than becoming unreachable.

### Deep planning and adversarial review

- **Planning is per request, not a global mode**: the web UI's Plan toggle beside the message box (a
  sticky per-request switch), or `/plan <task>` in either front end. A planned turn first drafts an
  explicit plan -- which tools, skills, and MCP services to use, what to search for, where to author a
  skill or connect a server -- and then executes it. Planning is scratch work kept out of the saved
  conversation, which stores your actual request and the answer.
- **`plan_review`** pauses a planned turn for Approve / Edit / Reject; off runs the plan autonomously.
- **Adversarial plan and result review** (both off by default): an independent, context-free reviewer
  agent -- a fresh client seeing only the request plus the plan or answer -- critiques the plan and/or
  the final result. `plan_review_agent` re-plans on rejection up to `review_rounds`, surfacing leftover
  concerns. `result_review` checks the answer before it is shown and revises on rejection; because a
  result cannot be vetted and streamed at once, the executor's thinking and tool calls stream live while
  the final answer is withheld until it passes, then a clean transcript is committed.
- **Reviewers are tool-using and grounded.** Each runs a bounded tool-calling assessment over a curated
  verification toolset and then extracts its typed verdict in a follow-up structured call. The toolset
  is web lookup, `calculate`, and the current date and time; it deliberately excludes the user's memory
  and documents, skill authoring, MCP mutation, and `execute_python` -- a reviewer cannot be
  approval-gated, since an autonomous critic has nobody to ask mid-review, so it is given nothing that
  would need a gate. Both prompts warn that the reviewer's own knowledge may be stale, that disagreement
  with memory is not evidence of fabrication, and that a suspected inaccuracy must be verified with
  tools before flagging. The result reviewer is additionally shown an Evidence section -- the tool
  results the agent actually retrieved -- so it judges against real sources.
- **Verbose trace ("Show all reasoning")**, off by default: turns a planned turn into a labeled, live
  trace -- planner, each plan reviewer, executor, each result reviewer, and every revision -- showing
  every intermediate plan and result version. It overrides result review's hide-until-vetted gate; only
  the final approved answer is committed. The whole raw trace is recorded per turn and replayed on
  reload, so a reloaded verbose turn shows the same output it did live rather than a summary.
- In non-verbose turns the reviewers appear as their own cards that update in place from
  "reviewing..." to approved or rejected with the issues, so the otherwise-silent reviewer pauses are
  visible. Verdicts are recorded per turn and replayed in order on reload.

### Configuration

- **`config.toml` is the single source of settings, and the app writes it** -- there is no parallel
  store. Layering is built-in defaults < the TOML file < command-line flags. Writes are
  comment-preserving (via `tomlkit`, since stdlib `tomllib` cannot write TOML), so hand-written comments
  and layout survive an app write; a fresh file is seeded from the shipped example.
  `kokua config init` scaffolds `$KOKUA_HOME/config.toml` from `config.example.toml`, where every key
  sits at its built-in default, documented.
- **Strict parsing**: an unknown key or non-table section fails fast with a `ConfigError` naming the
  key, so typos and removed keys surface immediately instead of being silently ignored.
- **One runtime-settings table.** `config/table.py`'s `RUNTIME_SETTINGS` is the single declaration of
  what can change without a restart, driving the TOML schema, the panel sanitizer, the hot-apply set,
  the live-apply loop, the channel mirroring, and the persist path at once. Adding a setting is one
  entry, enforced by tests.
- **The assistant can inspect and repair its own configuration**: `read_config` / `update_config`.
  `update_config` validates and coerces the value, applies hot-appliable keys immediately and persists
  only after a successful apply (so a bad model is not saved), and reports "restart required" for
  everything else. A blocklist -- `[security] confirm_tools`, `[email] to`, `[paths] data_dir` -- can
  never be changed by the tool, only by hand. `update_config` is in the default `confirm_tools` list, so
  each write goes through the approval prompt.
- **Remote and custom model endpoints.** `[assistant] model` accepts AIMU's extended
  `provider:model_id[@base_url][;flags]` form, so Kokua can target a remote OpenAI-compatible server or
  a model id not in AIMU's catalog, e.g.
  `model = "llamaserver:qwen3-8b.gguf@http://gpu-box:8080/v1"`.
- **All state under one directory you own**: `$KOKUA_HOME`, default `~/.kokua`, replacing the example's
  reliance on `aimu.paths.output`. `data/` holds content only -- conversations, memory, documents,
  `images/`, `downloads/`, `logs/`, `scheduled_tasks.json`. Images and downloads live in their own
  folders so the binary files never disturb the `DocumentStore`, which scans the documents folder as
  text.

### MCP

- Remote MCP servers from a startup `--mcp <url>` (repeatable) or `[[mcp.server]]` tables in
  `config.toml`, plus `add_mcp_server` / `remove_mcp_server` at runtime. Servers connect before the
  first agent is built so config-declared servers reach workers, and a runtime add or remove rebuilds
  each live agent's `spawn_subagent` so roles pick the server up or drop it immediately.
- **Per-server bearer tokens via environment variables.** Each `[[mcp.server]]` takes a required `url`,
  an optional `name`, and an optional `token_env` naming the variable holding that server's token, read
  at startup so the secret stays out of `config.toml`. A `token_env` whose variable is unset logs a
  warning and connects tokenless rather than aborting startup.
- **OAuth**: `add_mcp_server` starts the OAuth flow for a server that signals its auth requirement with
  a non-standard response (a 400 plus "missing Authorization header", not only a 401/403). When the flow
  cannot complete because the server lacks dynamic client registration, it returns an actionable "provide
  a bearer token and add the server again" message instead of a raw `OAuthRegistrationError`, and its
  docstring tells the assistant to relay that, ask for a token, and retry with `bearer_token`.
- **Startup warns about a server no role names.** Since the supervisor mounts no MCP callables, such a
  server connects, spends its token on the handshake, and is then reachable by nobody. That is now a
  warning naming the server.

### Images

- Attach images and the assistant reads them (needs a vision-capable model); it can generate them when
  `AIMU_IMAGE_MODEL` is set. In the web UI, attach via the composer's paperclip or by pasting, with
  thumbnails previewed before send and images rendered inline live and on reload; in the CLI, `/attach`.
- Uploaded and generated images are stored content-addressed under `data/images/` and served at
  `GET /images/<name>`. A conversation keeps only a short `/images/<name>` reference, so the session
  store stays small; the bytes are re-inlined as base64 only when a turn is sent to the model, since a
  localhost URL is not fetchable by the provider.

### Security

Kokua can author and run Python and shell scripts as **real subprocesses with your user privileges and
no sandbox**, and can connect to remote MCP servers and run whatever tools they expose. Real capability
is the point of a personal assistant, but it means a prompt-injected or mistaken model can run arbitrary
code on your machine. Run it only with a model, inputs, and MCP servers you trust. The CLI prints a
notice on startup.

- **Tool approval.** Configured risky tools require confirmation before each call -- terminal `y/N`, web
  Allow/Deny -- built on AIMU's `ToolApproval` gate. The default set is `add_skill_script`,
  `add_mcp_server`, `execute_python`, and `update_config`; adjust with `[security] confirm_tools` or
  `--confirm-tools` (empty disables). Proactive and backgrounded turns auto-deny gated tools regardless,
  so a full-access tool is never run unattended, and a prompt only ever appears for the conversation you
  are currently viewing. The reply is routed through the single channel reader, so it is safe alongside
  `/stop`. Approval and plan review share one lock-guarded pending slot, so two concurrent requests
  cannot overwrite each other.
- **The reviewer toolset needs no gate.** An autonomous critic cannot pause to ask you mid-review, so
  rather than exempting it from the gate the reviewer is given nothing the gate exists to cover: web
  lookup, `calculate`, and the clock. A test pins this against the shipped `confirm_tools` default, so
  adding a name there fails the suite until the reviewer's toolset is re-checked.
- **Injection crosses conversations.** The assistant can read every saved conversation, and a transcript
  is untrusted text, so an injection that lands in one conversation can influence what the assistant
  does in another. The three read tools are ungated by default, since they only read and gating them
  would make an unattended scheduled run that reads history fail silently; add `read_conversation` and
  `search_conversations` to `[security] confirm_tools` to change that.

### Diagnostics and error reporting

- **An AIMU too old to run Kokua fails with an instruction, not a traceback.** The `aimu>=0.13.1`
  requirement covers a normal install, but a development checkout installs the sibling `../aimu`
  editable and that checkout can sit on an older commit. `kokua.aimu_compat` preflights both the version
  floor and one capability probe -- the version string of an editable install says what its branch
  claims, not what its code contains -- and names both fixes: update the sibling, or
  `uv sync --no-sources` to take AIMU from PyPI instead.
- **A failed model request reports its actual cause.** `kokua.core.errors.describe_error` walks the
  exception's `__cause__` chain to the root, so an unreachable local model server is diagnosable from
  the chat itself ("The request couldn't reach the model server: ModelConnectionError: Connection error.
  (caused by ... Connection refused)"). Reactive and proactive turns both surface the detail.
- **Model resolution failures surface cleanly.** With no `model` set, AIMU resolves
  `AIMU_LANGUAGE_MODEL`, else the first already-running local model (Ollama, then a local
  OpenAI-compatible server), and never a cloud model. When nothing resolves, or the model string is
  invalid, `Assistant.create` raises a `ModelClientError` carrying AIMU's actionable message: the CLI
  prints it and exits non-zero, the web UI shows it in the chat. Because agents are built lazily, the
  web UI's new / select / delete controls can hit the same error, and report it in the chat rather than
  tearing down the WebSocket. A web build failure also releases the single-connection guard, so a later
  connection is not wrongly refused as "busy in another tab".
- **Hang observability.** `/diag` reports the in-flight turn, elapsed time, whether the turn gate is
  held, and dumps a wedged turn's async stack. It is handled in the serve loop without taking the gate,
  so it answers even when a hung turn holds it. Diagnostic logs go to a rotating
  `data/logs/kokua.log` (5 × 2 MB) with turn-lifecycle lines, level set by `[logging] level`.
  `faulthandler` is enabled, so `kill -USR1 <pid>` dumps all thread stacks.

### Known limitations

- **A `target="task"` scheduled conversation grows without bound.** Reusing one conversation across
  every firing is the intended continuity tradeoff, but each firing replays the full, growing transcript
  to the model, so a high-frequency or long-lived task means steadily rising token cost and eventually
  the context window. There is no cap yet.
- **A gated tool call inside a sub-agent prompts at the top level**, not inside its card.
- **A tool result travels whole and only the DOM clamps it**, so the web `history` frame grows by every
  tool result in the conversation and is re-sent on every conversation switch, not only on reload. A
  conversation with dozens of large results (PDF extractions, email fetches, big MCP responses) makes
  that frame correspondingly large.
- **A message can bind to the wrong conversation if you switch immediately after sending.** A reactive
  turn binds to a conversation when the serve loop dequeues its message, while the web front end handles
  new / select / delete inline, so a control processed in that sub-millisecond window sends the reply to
  the conversation switched *to*. Not reachable by hand; surfaced deterministically by the Playwright
  suite, which waits for the turn to be observably running before switching.

### Internals and development

- `src/kokua/` is grouped by subsystem -- `core/`, `config/`, `planning/`, `mcp/`, `scheduling/`,
  `channels/`, `frontends/`, `toolpacks/` -- and `tests/` mirrors it exactly.
- `Assistant` is a composition root and the serve loop; it delegates to `ConversationBook` (store +
  agent cache + active pointer), `TurnRunner`, `HumanGate`, `SettingsApplier`, and `ChannelUI`.
  `ChannelUI` is the one adapter that probes each optional channel capability once and gives every rich
  frame a documented fallback, so there is no `isinstance(channel, WebChannel)` in `core/` or
  `planning/`. `channels/protocol.py` declares the rich surface for documentation and typing.
- The six principles that decide what belongs in the core, each with the code that backs it, are in
  [docs/explanation/design-principles.md](docs/explanation/design-principles.md); the architecture
  narrative is in [docs/explanation/architecture.md](docs/explanation/architecture.md).
- Task-oriented guides for the three ways to add capability are in
  [docs/how-to/](docs/how-to/index.md): [set up a toolset](docs/how-to/set-up-toolsets.md) (tool groups,
  sub-agent roles, writing a tool-pack), [add a skill](docs/how-to/add-skills.md), and
  [add an MCP service](docs/how-to/add-mcp-services.md). All three converge on the same rule: nothing
  reaches an agent until a `[subagents.roles.*]` table names it.
- **Verifiable without a model.** The default test suite is mock-only: no model, no network, no keys.
  This is why the model client is injectable and the builders are free functions. Client-side page JS is
  covered by an opt-in Playwright suite (`pytest -m e2e`) driving the real `index.html` in headless
  Chromium; it skips rather than errors when the browser or the `web` extra is absent, and it does not
  gate the default suite.
- CI lints, runs the suite on Python 3.11-3.13, and separately builds the sdist and wheel, checks their
  metadata, and installs the wheel into a clean environment to run both console scripts -- an editable
  install imports straight from `src/`, so it cannot catch a missing package-data entry or a console
  script pointing at a moved function.
