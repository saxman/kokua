# Changelog

## 0.1.0 (unreleased)

- **A tool card shows what the call returned, not just what it was asked to do.** The result was
  reaching the channel all along -- AIMU yields `TOOL_CALLING` only once a call has been dispatched, so
  the chunk carries the result -- and was being dropped. Worse, it wasn't even reconstructible on
  reload: a stored `role: "tool"` message was skipped entirely by replay, except for the `/images/`
  reference a generated image carries.

  The result now rides the `tool` frame as `response` and renders in a nested foldable below the
  arguments, labeled with its size (`↳ output (2,148 chars)`) so a reader knows what opening it costs.
  The body fills on first open and clamps at 4000 characters with a `show all` button for the rest, as
  plain text and never markdown, since a tool result is untrusted input. An error, an argument-binding
  failure, and a denied approval are results too and arrive on the same key, so there is no separate
  failure rendering to learn.

  All four surfaces that render a tool call carry it: a live card, a background turn's catch-up replay,
  a sub-agent card's nested calls, and a reloaded transcript. Replay rejoins a stored call to its result
  by `tool_call_id` rather than by position, since concurrent dispatch appends results in completion
  order. A call with no matching result -- a conversation stored before this shipped, a turn cut short
  mid-dispatch -- renders the card exactly as it always did. `show_tools` still gates the whole card,
  output included, and the tool-approval prompt is unchanged: it fires before dispatch, so there is no
  result yet.

  One cost worth knowing: output travels whole and only the DOM clamps, so a `history` frame grows by
  every tool result in the conversation and is re-sent on every conversation *switch*, not only on
  reload. A conversation with dozens of large results (PDF extractions, email fetches, big MCP
  responses) makes that frame correspondingly large.

  Requires the matching AIMU change (`response` on its base web `tool` frame), which is on AIMU's
  `main`.

- **Breaking: one agent shape, and roles are the only switch.** The `subagents` and `lean_supervisor`
  flags are gone, along with the `--subagents` / `--no-subagents` CLI flags and flat mode entirely.
  Every agent is now a lean supervisor: it mounts the cross-cutting tools (skills, MCP management,
  memory, config, scheduling), the `time` group, and `spawn_subagent`, and nothing else. Two flags that
  could contradict each other and each other's roles are replaced by one fact -- whether
  `[subagents.roles.*]` defines anything.

  Two consequences to know about. **`[tools].groups` is a ceiling on what workers may draw from, not
  the assistant's own toolset**, and **an installed tool-pack or a configured MCP server reaches
  nothing until some role names it** (`tool_packs = [...]` / `mcp_servers = [...]`). If you ran with
  `lean_supervisor = false`, add roles covering the tools you relied on.

- **Breaking: sub-agent roles come from `config.toml` only, and `config.toml` is now required.**
  `researcher`, `coder`, and `generalist` were hardcoded in `config/schema.py` and merged under
  whatever `[subagents.roles.*]` defined, so the roles you actually ran with were invisible in the file
  that is supposed to be the single source of settings. That constant is gone; the three roles ship as
  live tables in `config.example.toml`, which is what `kokua config init` writes, and you edit, delete,
  or add to them like any other setting.

  Because roles exist nowhere else and a supervisor with no workers cannot browse, read a file, or
  compute, **Kokua now refuses to start without a config file, or with one that defines no roles**,
  naming `kokua config init` in the error rather than running something that looks alive and cannot
  work. **On upgrade, an existing `config.toml` with no `[subagents.roles.*]` section will fail to
  start** -- copy the roles from the updated `config.example.toml`.

- **A scheduled task can be read in full and edited in place.** Asking to change a task's wording
  produced a rewritten-from-scratch prompt, because nothing could show the model the original: the only
  view was `list_scheduled_tasks`, which cut every prompt to 60 characters with no mark to say it had
  cut anything, and the only way to "edit" was `cancel_scheduled_task` plus a fresh `schedule_task`.
  Two new tools close that. **`get_scheduled_task`** shows one task whole -- complete prompt, schedule,
  target, next fire, dedicated conversation. **`update_scheduled_task`** edits any subset of a task's
  fields in place, keeping its id, `created_at`, and dedicated conversation, and re-deriving the
  schedule fields you omit from the record, so changing a weekly task's time keeps its day. It re-arms
  only when the schedule actually changes, so editing a prompt never restarts an interval countdown, and
  it rejects an invalid schedule, a past one-shot, or a name another task holds without writing anything.
  The listing now marks a truncated prompt with `...` and points at `get_scheduled_task`.

- **Fixed: switching conversations mid-reply streamed the rest of that reply into the conversation you
  switched to.** Backgrounding a turn is supposed to mute it, but the mute decision was taken once, when
  the reply's send began, and the streaming loop then ran to completion under that stale answer. Switching
  away mid-reply replaced the page with the new conversation's history and every later token, thinking
  block, tool call, plan bubble, and sub-agent card of the old turn was appended there. The decision is
  now made per frame, as each is sent, so a switch takes effect on the next token: the abandoned turn
  finishes silently and announces itself with the usual "Reply ready in ..." notification. The page had a
  matching bug of its own -- replaying a conversation removed the streaming bubble from the transcript but
  kept writing into it, so the *next* turn in any conversation resurrected the abandoned reply's text
  above its own.

- **Switching into a conversation mid-turn now shows the turn.** Its messages aren't in the store until
  the turn ends, so a switch-in used to replay the conversation as though the turn had never started: no
  user bubble, and (before the fix above) the answer resuming mid-sentence with its beginning missing,
  staying truncated until the next reload. The turn's output is now recorded as it is produced -- the user
  bubble, reasoning, tool calls, sub-agent cards, uploaded and generated images, and the answer so far --
  and rides along on the same frame the switch-in replays, after which the rest of the reply streams into
  the bubble that replay left open. The record is dropped the moment the turn reaches the store, so
  nothing is ever shown twice.

- **Startup warns about an MCP server no role names.** Since the supervisor mounts no MCP callables, a
  server that no `[subagents.roles.*]` lists in `mcp_servers` connects, spends its token on the
  handshake, and is then reachable by nobody. That was silent; it is now a warning naming the server.

- **AIMU's prebuilt agents, wired in as a tool-pack.** The new built-in `aimu_agents` pack mounts
  `CodeReviewAgent`, `ResearchReportAgent`, and `ContentCreationAgent` as the tools `code_review`,
  `research_report`, and `create_content`. It exists mainly as the worked example of wiring an agent
  built with AIMU into Kokua: every `Runner` exposes `.run(task) -> str`, so a tool-pack is the entire
  bridge and the core gains no new surface -- no `[[agents]]` table, no import-by-config. Nothing is
  mounted until a role asks for it with `tool_packs = ["aimu_agents"]` (the supervisor mounts no pack
  tools), and `config.example.toml` ships that role commented out. `research_report`'s
  workers get `builtin.web` only when the `web` group is enabled, so a pack-mounted agent respects
  `[tools].groups` exactly as a role does. Each call builds a fresh agent, reading `config.model` at
  call time so a runtime model switch reaches it. The caveats are real and documented in the module:
  the prebuilts are synchronous, so a nested run gets no sub-agent card, no `/stop`, and no approval
  gate on its workers -- and a `coder` role with `fs` + `compute` is a stronger reviewer than the
  tool-less `CodeReviewAgent`.

- **A sub-agent card looks like the tool call it replaces.** The web channel already suppresses the
  parent's `spawn_subagent` tool block because the card stands in for it, but the card looked like
  nothing else on the page. A spawn now renders as `🔧 spawn_subagent(<role>) — <status>` in the same
  monospace a tool block's header uses, over an argument line (`agent_type="…", task="…"`) built by
  the same helper that fills a tool block's body, and then the nested thinking / tool / answer blocks
  it already showed. The task moved off the header, where it was truncated to 60 characters, onto
  that argument line, where it is shown whole. Planning reviewer verdicts share the card frame but
  are backed by no tool call, so they keep their plain `🔎 <role> — <status>` header.

- **Every agent can tell the time.** AIMU's clock and timezone converter moved out of `builtin.misc`
  into their own `builtin.time` group, which Kokua now exposes as the `time` tool group and adds to the
  default set (`web,fs,compute,time,misc`). Two agents previously could not reach these tools at all,
  which is what prompted the change: the **lean supervisor** mounts no built-in groups, and force-added
  only `get_current_date_and_time`, so `convert_time` never reached the assistant no matter what
  `[tools].groups` said; and any **sub-agent role** whose `groups` omitted `misc` (the built-in `coder`,
  which is `fs`+`compute`, and every role defined purely by a tool-pack or MCP server) got a worker with
  no clock. The lean supervisor now mounts the whole `time` group, and the group is added to *every*
  sub-agent role on top of its own `groups`, since a worker that cannot resolve "by tomorrow morning" is
  broken whatever its domain and a role should not have to remember to ask. That addition is gated on
  `time` being in `[tools].groups`, so it still never grants a tool the user disabled globally; drop
  `time` there and no agent gets it, workers included. Roles need not list `time` themselves. Note that
  a model with a calculator is not the same as one that uses it: the lean supervisor still has no
  `calculate`/`execute_python` of its own by design, and must delegate arithmetic to a worker.

- **Design principles written down**: [docs/explanation/design-principles.md](docs/explanation/design-principles.md)
  states the six principles that decide what belongs in Kokua's core, each with the code that backs
  it, what it excludes, and what it means for a contributor.
  [docs/explanation/architecture.md](docs/explanation/architecture.md) now holds the architecture
  narrative that used to live only in `CLAUDE.md`, so there is one place to keep current.

- **Modularization**: `src/kokua` is grouped by subsystem instead of 23 flat modules -- `core/`,
  `config/`, `planning/`, `mcp/`, `scheduling/`, beside the existing `channels/`, `frontends/`,
  `toolpacks/`. No entry-point paths changed, so plugin registration and the console scripts are
  untouched. `kokua.assistant` remains for one release as a `DeprecationWarning` shim re-exporting
  `Assistant` from `kokua.core`. Retires TODO #6. The stable public import surface is now declared:
  `kokua.plugins`, `kokua.config`, `kokua.core`, `kokua.channels.web`, `kokua.images`.

- **`Assistant` decomposed** from an 872-line class mixing nine concerns into a composition root plus
  `ConversationBook` (store + agent cache + active pointer), `TurnRunner` (reactive and proactive
  turns), `HumanGate` (approval and plan review), `SettingsApplier`, and `ChannelUI`. Its public
  surface is unchanged: front ends call the same twelve members.

- **Bug fix -- concurrent plan reviews clobbered each other.** Tool approval was guarded by a lock so
  concurrent gated calls could not overwrite the pending slot; plan review never was, so two
  concurrent planned turns would lose one of the two prompts (the first waiter hung, and the answer
  landed on the wrong request). Both now share one lock-guarded `PendingRequest`.

- **Bug fix -- a failing scheduled task could take down the scheduler.** Only `target="active"`
  firings had error handling; the `"new"`/`"task"` path returned before reaching it, so a model error
  propagated into the scheduler job, which has no handler. A `target="task"` firing that failed also
  forgot the conversation it had just created and minted a new one on every later firing. Both fixed
  by unifying the proactive paths; the key is now returned even on failure.

- **One runtime-settings table.** The set of settings changeable without a restart was spelled out in
  nine places. `config/table.py`'s `RUNTIME_SETTINGS` is now the single declaration, driving the TOML
  schema, the panel sanitizer, the hot-apply set, the live-apply loop, the channel mirroring, and the
  persist path. Adding a setting is one entry, enforced by tests.

- **One planned-turn pipeline.** The verbose and summary `/plan` flows were mirrored implementations
  (~150 duplicated lines). They are now one pipeline plus a `Presentation` record with two instances.
  Behavior is unchanged, pinned by frame-sequence characterization tests written before the change.

- **`ChannelUI`** replaces 17 scattered `getattr(channel, "send_x", None)` / `hasattr` sites with one
  adapter that probes each capability once and gives every optional frame a documented fallback.
  `channels/protocol.py` declares the rich surface for documentation and typing.

- **Dead code removed**: six unused functions in `paths.py` (every leaf path is a derived
  `AssistantConfig` property), `images.save_file`, `TurnInfo.background`, two `TurnTracker` methods,
  and the legacy `new_session` fallback in the scheduled-task registry.

- **Tests** mirror the source layout; the 2346-line `test_assistant.py` is split along the same seams.

- **Memory store thread-safety moved upstream**: concurrent-turn safety for the shared memory and
  document stores now lives inside AIMU's `SemanticMemoryStore`/`DocumentStore` (a re-entrant per-store
  lock) rather than a Kokua-side wrapper. `build_memory` returns the stores' tools directly again.

Initial release. Kokua starts from AIMU's `examples/personal-assistant/` and restructures it into an
installable, modular application.

- **Package**: `src`-layout `kokua` package with console scripts `kokua` (runs the selected front end) and
  `kokua-web`. Apache-2.0, Python 3.11+.
- **Assistant core** (`kokua.assistant`): the transport-agnostic `Assistant` wiring an AIMU `SkillAgent`
  with skill authoring + runnable skill scripts, persistent conversation history,
  remote MCP servers (startup `--mcp` + runtime `add_mcp_server`), and persistent memory (a
  `SemanticMemoryStore` for facts + a `DocumentStore` for documents, on by default).
- **Model-failure surfacing**: a failed model request now reports its actual cause instead of a fixed
  "Sorry, something went wrong handling that." A new `kokua.errors.describe_error` walks the exception's
  `__cause__` chain to the root and the turn handler sends it (e.g. "The request couldn't reach the model
  server: ModelConnectionError: Connection error. (caused by ... Connection refused)"), so an unreachable
  local model server is diagnosable from the chat itself. Reactive and proactive (scheduled) turns both
  surface the detail; a proactive failure is reported and swallowed so it can't crash the scheduler.
  Relies on AIMU's new `ModelConnectionError` (re-exported as `kokua.assistant.ModelConnectionError`).
- **Hang observability**: a `/diag` chat command reports the in-flight turn, elapsed time, whether the
  turn lock is held, and dumps a wedged turn's async stack — handled in the serve loop without the lock,
  so it answers even when a hung turn holds it (`Assistant._diag_report`). Diagnostic logs now go to a
  rotating `data/logs/kokua.log` (5 × 2 MB) with turn-lifecycle lines (submitted / lock acquired /
  done / error), configured via `configure_logging` (`kokua.logging_setup`) at startup and the
  `[logging] level` config key. `faulthandler` is enabled so `kill -USR1 <pid>` dumps all thread stacks.
- **Typed, concurrent sub-agents**: `spawn_subagent` is now typed — `spawn_subagent(agent_type, task)`
  with built-in `researcher` / `coder` / `generalist` roles, each cloning the active model with its own
  tool subset (role groups intersected with the enabled `[tools]` groups; parent-only memory/skills/MCP
  withheld). Override or add roles under `[subagents.roles.*]`. Independent spawns in one turn run
  concurrently (`[subagents] concurrent`, default on); the tool-approval gate serializes only the gated
  `confirm_tools`, so gated calls still prompt one at a time. A sub-agent's gated-tool calls (the
  `confirm_tools`, e.g. `execute_python`) are routed to the parent for approval and are not run
  unattended. Fixed: `[tools] groups = ["all"]` now correctly enables all tool groups for sub-agent
  roles (previously the clamping logic treated `"all"` as a literal group name, leaving roles with no
  tools).
- **Lean supervisor mode** (`[assistant] lean_supervisor`, **on by default**; set false for the flat
  agent): a supervisor/worker architecture that shrinks the per-request tool context. When on, the per-conversation agent keeps
  only its cross-cutting tools (memory, skills, MCP management, config, scheduling, date/time) plus the
  single `spawn_subagent` delegate, and delegates all specialized work to workers whose roles carry the
  scoped toolsets — dropping the advertised tool set from ~30+ to ~10-12. Sub-agent roles can now be
  assigned specific MCP servers (`mcp_servers`, by a server's new optional `[[mcp.server]].name` or its
  URL) and tool-packs (`tool_packs`) in addition to built-in `groups`, so a role can own e.g. a trading
  MCP server or the `pdf`/`email` packs. In lean mode `[tools] groups` defines the universe of built-in
  groups workers may draw from. Default (flat) wiring is unchanged. MCP servers now connect before the
  first agent is built so config-declared servers reach workers; adding or removing a server at runtime
  (`add_mcp_server`/`remove_mcp_server`) rebuilds each live agent's `spawn_subagent` so worker roles
  pick up or drop it immediately (and in lean mode the server's raw tools are not mounted on the
  supervisor).
- **Scheduled tasks**: the assistant can schedule durable, agent-managed tasks (`schedule_task` /
  `list_scheduled_tasks` / `cancel_scheduled_task`) that fire an unprompted turn when due, persisted to
  `data/scheduled_tasks.json` and re-armed at startup. Schedules are one-shot, interval, daily, or
  weekly (no cron dependency). A per-task `target` selects where each firing runs: `active` (default,
  the currently-viewed conversation), `new` (a fresh conversation per firing), or `task` (one dedicated
  conversation, created on the first firing and reused on every later firing so the task builds on its
  own history; a deleted conversation is recreated on the next firing). `disable_scheduled_task` /
  `enable_scheduled_task` pause a task (it stops firing but stays in the registry) and resume it later,
  without losing the task; disabled tasks are skipped at startup and show as `disabled` in the listing.
  `run_scheduled_task` runs an existing task now, on demand, without changing its schedule: it reproduces
  the real firing (honoring `target`, auto-denying gated tools), so a task can be dry-run before it is
  due; disabled tasks can be run too.
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
  and links open with `rel="noopener"`. LaTeX math (`$...$`, `$$...$$`, `\(...\)`, `\[...\]`, common in
  Gemini output) is typeset with vendored KaTeX (JS + CSS + woff2 fonts bundled in `web_static/`, served
  locally); it runs after DOMPurify with `trust:false` + a `maxExpand` cap, so untrusted output stays
  safe, and `throwOnError:false` leaves a malformed expression as source text instead of breaking the
  bubble. Light and dark themes: a theme selector in the settings panel
  (auto / light / dark; auto follows the OS preference) sets a per-browser choice remembered locally and
  applied before first paint (no flash on load, no new dependencies). Each conversation bubble shows a
  datetime caption ("Jul 23, 3:45 PM", localized via `toLocaleString`, full precision on hover); it sits
  below a regular bubble and on the always-visible header line of a foldable (thinking/tool/phase/
  sub-agent) so it shows whether the block is collapsed or expanded. The time is the message's
  append-time `timestamp` (AIMU's inert message key, now populated on the async path), threaded into the
  `history` frame per item by `conversation_to_frames` and rendered on both live and replayed bubbles;
  live bubbles with no server time yet are stamped client-side. Messages persisted before this change
  carry no timestamp and render without a caption. Ephemeral chrome (system notices, approval prompts,
  banners) is not stamped.
- **Multiple web conversations**: the web UI lists conversations in a sidebar (auto-titled from the
  first message) and lets you start a new one or select an existing one to continue, backed by AIMU's
  `sessions` store. Memory stays shared across conversations. CLI multi-conversation is a later change.
  Each conversation row has a delete (`×`) control (with a confirmation prompt); deleting the active
  conversation switches to the most-recently-updated remaining one, or a fresh empty one if none remain.
  Backed by a new `delete(key)` on AIMU's `SessionStore`.
- **Collapsible, resizable sidebar (web)**: the left panel can be collapsed to a narrow icon rail (a
  `«`/`»` toggle) and drag-resized via the divider between it and the chat (clamped to 180-480px;
  dragging below the threshold snaps to the rail, dragging back out re-expands). The divider is also
  keyboard-operable (arrows resize, Enter toggles). Width and collapsed state are a per-browser
  preference (localStorage), applied before first paint like the theme so there is no flash on load.
- **Distinguish agent-loop turns from user input (web)**: the agent loop injects its own continuation
  turns as `user`-role messages; using AIMU's inert `provenance` message key, the web UI now renders these
  as a muted `↻ continuation` marker at each loop-iteration boundary instead of as user bubbles, both live
  (keyed off `StreamChunk.iteration`) and on history replay. The marker shows the injected prompt text for
  inspection. Proactive turns are tagged and replay with their existing amber styling. Real user turns are
  unaffected.
- **Foldable auxiliary blocks (web)**: the non-direct blocks in the chat transcript (thinking, tool
  calls, `↻ continuation` markers, verbose-trace phases, sub-agent cards, and drafted plans) now render
  collapsed, each with a click-to-expand header. The header keeps the identifying label (tool name,
  phase name) so the transcript stays scannable; expanding reveals the verbose detail. Direct user and
  assistant messages and the interactive approval / plan-review prompts are unaffected. Fold state is
  per-block and not persisted across reloads. Client-side only; no protocol change.
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
  the client and carries the conversation over) and persist across restarts by writing back into
  `config.toml` (see the config-consolidation entry below). The theme is a per-browser choice (stored
  locally, not server-side). Provider support varies: thinking models ignore `top_p`/`top_k` and force
  `temperature`, and Anthropic does not support the penalty parameters.
- **Config consolidation (single source of settings)**: `config.toml` now holds all settings and is
  written by the app as well as hand-edited, replacing the separate `data/runtime-settings.json` and
  `data/mcp-servers.json` state files (both removed, along with `mcp_registry.py`). `config_store.py`
  does comment-preserving writes via `tomlkit` (new dependency; stdlib `tomllib` cannot write TOML), so
  hand-written comments and layout survive app writes; a fresh file is seeded from the shipped example on
  first write. The web settings panel and the `add_mcp_server`/`remove_mcp_server` tools now persist here
  (runtime-added MCP servers land in `[[mcp.server]]`, URL only). `data/` holds only content now.
- **Assistant config tools**: `read_config` / `update_config` (`kokua.config_tools`) let the assistant
  inspect and repair its own configuration in-conversation. `update_config` validates and coerces the
  value, applies hot-appliable keys (model, `[generation]`, display and planning flags) to the running
  session immediately (persisting only after a successful apply, so a bad model isn't saved) and reports
  "restart required" for everything else. A security blocklist (`[security].confirm_tools`, `[email].to`,
  `[paths].data_dir`) can never be changed by the tool, only by hand-edit; `update_config` is in the
  default `confirm_tools` list, so each write goes through the approval prompt.
- **Remote/custom model endpoints**: `[assistant].model` accepts AIMU's extended
  `provider:model_id[@base_url][;flags]` form, so Kokua can target a remote OpenAI-compatible server
  (e.g. a llama.cpp `llama-server` on another host) or a model id not in AIMU's catalog. Example:
  `model = "llamaserver:qwen3-8b.gguf@http://gpu-box:8080/v1"`. Documented in `config.example.toml`.
- **Markdown-to-PDF tool**: a built-in `pdf` tool-pack contributes `markdown_to_pdf`, which renders
  Markdown to a PDF (via `fpdf2` + `markdown`, both pure-Python, no system libraries) saved in
  `data/downloads/`. Enabled by default like any tool-pack. The web front end serves that folder at
  `GET /download/<name>`, so the assistant can hand back a download link; the tool also returns the
  absolute path for the CLI. (Downloads live in their own folder, not `data/documents/`, so the binary
  PDFs never disturb the DocumentStore, which scans the documents folder as text.)
- **Email tool**: a built-in `email` tool-pack contributes `send_email`, letting the assistant email
  information to you (digests, summaries, reports) over SMTP (stdlib `smtplib`, no extra dependency). The
  recipient is locked to your configured `[email] to` address, so the tool takes no recipient and can only
  ever email you. The body is written in Markdown and delivered as HTML with a plain-text fallback;
  attachments are limited to files already in `data/downloads/` or `data/images/` (traversal-safe). The
  tool is offered only when `[email] host` and `to` are set and the `KOKUA_EMAIL_PASSWORD` env var is
  present (the password is never read from the config file). Sending is ungated, so scheduled/proactive
  turns can send (e.g. a daily digest).
- **Image input and output**: attach images and the assistant reads them (vision), and it can generate
  images. In the web UI, attach via the composer's paperclip or by pasting; thumbnails preview before
  send and images render inline in the chat (live and on reload). In the CLI, `/attach <path>` stages a
  local image onto the next message. Vision needs a vision-capable model (Claude models qualify);
  generation needs the `AIMU_IMAGE_MODEL` env var (e.g. `gemini:nano-banana` or a HuggingFace diffusers
  `hf:<repo>`), and the built-in `image` tool-pack contributes `generate_image` only when it is set.
  Uploaded and generated images are stored under `data/images/` (content-addressed) and served at
  `GET /images/<name>`; a conversation keeps only a short `/images/<name>` reference, so `sessions.json`
  stays small (the bytes are re-inlined as base64 only when a turn is sent to the model, since a
  localhost URL is not fetchable by the provider). Like downloads, images live in their own folder so the
  binary files never disturb the DocumentStore.
- **Deep planning (per request)**: a planned turn first drafts an explicit plan (which tools/skills/MCP
  services to use, what to web-search for, and where to build a skill via `author_skill` or connect a
  server via `add_mcp_server`) and then executes it. Planning is invoked per request, not as a global
  mode: use the web UI's **Plan** toggle next to the message box (a sticky per-request switch), or send
  `/plan <task>` in either front end. `plan_review` pauses a planned turn for Approve / Edit / Reject; off
  runs the plan autonomously. Built on Kokua's existing turn loop and tool-approval round-trip (AIMU
  already makes the agent plan-capable); planning is scratch work kept out of the saved conversation,
  which stores your actual request and the answer. `web-search`-for-MCP relies on the default `web` tools.
- **MCP OAuth fallback fix**: `add_mcp_server` now starts the OAuth flow for a server that signals its
  auth requirement with a 400 + "missing Authorization header" (rather than a standard 401). The
  auth-challenge heuristic previously matched only `unauthor`/401/403, so such a server surfaced the raw
  "bad request: missing required Authorization header" error instead of posting an authorization link; it
  now also matches `authoriz`/`authentic`.
- **Bearer-token guidance for OAuth-incapable servers**: when a server requires authentication but its
  OAuth flow can't complete because it lacks dynamic client registration (the `/register` endpoint 404s),
  `add_mcp_server` now returns an actionable "provide a bearer token and add the server again" message
  instead of a raw `OAuthRegistrationError`. Its docstring tells the assistant to relay the message, ask
  the user for a token, and retry with `bearer_token`.
- **Per-service MCP bearer tokens via env vars**: the `[mcp]` config is now an array of `[[mcp.server]]`
  tables, each with a required `url` and an optional `token_env` naming an environment variable that holds
  that server's bearer token (read at startup, so the secret stays out of `config.toml`). This replaces
  the single `[mcp] servers`/`bearer` keys, which applied one token to every server. `--mcp <url>` still
  adds token-less servers from the CLI; the `--mcp-bearer` flag is removed (put authenticated servers in
  `config.toml`). A configured `token_env` whose variable is unset logs a warning and connects tokenless
  rather than aborting startup.
- **Adversarial plan + result review** (deep planning, both off by default): an independent, context-free
  reviewer agent (fresh client, sees only the request + plan/answer) critiques the plan and/or the final
  result. `plan_review_agent` re-plans on rejection up to `review_rounds`, surfacing leftover concerns (to
  the human gate when it's on, else noted with the plan). `result_review` checks the answer before it's
  shown and revises on rejection; because a result can't be vetted and streamed at once, it runs the
  executor with the agentic loop (thinking/tool calls) still streaming live but the final answer withheld
  until it passes review, and commits a clean transcript. Reuses AIMU's structured output
  (`client.chat(schema=Verdict, use_tools=False)`) with no AIMU change; toggles in the settings panel and
  the `[planning]` config section.
- **Verbose trace ("Show all reasoning")**: an opt-in planning toggle (default off) that turns a planned
  turn into a labeled, streamed trace -- planner, each plan reviewer, executor, each result reviewer, and
  every revision stream their thinking + output live under phase headers, and every intermediate plan and
  result version is shown. Reviewers stream a free-text prose assessment (readable, and their thinking when
  the model emits it). It overrides result review's "hide until vetted" gate (you see every version); only
  the final approved answer is committed to the transcript. Thinking is model-dependent (adaptive models
  may skip it on easy prompts). The whole raw trace (each phase's label + streamed text) is now recorded
  per turn in `session.metadata["trace"]` and replayed on reload, so a reloaded verbose turn shows the
  same raw output it did live -- not a summary. Verbose turns show only this raw trace; they do not emit
  the summary reviewer cards below.
- **Sub-agent activity in the web UI** (non-verbose turns): the adversarial reviewers show up in the chat
  stream as their own cards -- "Plan reviewer / Result reviewer -- reviewing..." that update in place to
  approved / rejected (with the issues) -- so the otherwise-silent reviewer pauses are visible. Added
  via a generic `subagent` WebSocket frame (no change to the model conversation schema); reviewer
  verdicts are recorded per turn in `session.metadata` and replayed in order on reload. (With the verbose
  trace on, the raw streamed reasoning replaces these cards.)
- **Spawned sub-agent activity, shown live and on reload**: a `spawn_subagent` call now gets its own
  foldable card in the conversation that spawned it, updated in place as the child agent runs -- nested
  reasoning (gated by `show_thinking`) and nested tool calls (gated by `show_tools`), with the card and
  its final answer always shown regardless of those flags. No new setting. `core/subagents.py`'s
  `SubagentReporter` implements AIMU's `SubagentObserver` and is the one instance every per-conversation
  agent's `spawn_subagent` reports through, including after a runtime MCP add/remove rebuilds it.
  Recording (`ConversationBook.record_subagent_events`, a new method sharing the same
  `metadata["subagent"]` map the reviewer cards above write into) and display are independent: a
  background turn's cards are muted like everything else it streams, but its spawns are still recorded,
  so switching into that conversation later shows the work. This depends on AIMU's unreleased `main`
  (the `SubagentObserver` protocol and the `observer=` parameter on `make_async_subagent_tool`, see
  AIMU's own changelog); `build.py` imports `SubagentObserver` unconditionally, so an AIMU install that
  predates this seam fails at Kokua startup with an `ImportError`, not a silent no-cards fallback --
  another symptom of the stale-same-version-string trap `CLAUDE.md`'s AIMU-dependency section already
  warns about. Known limitation: a gated tool call inside a sub-agent (e.g. `execute_python`) still
  prompts at the top level, not inside its card. The parent's own `🔧 spawn_subagent(...)` tool card is
  suppressed (its role/task/result are pure duplication of the `🤖` card): `WebChannel.send_frame`
  drops the `tool` frame for `spawn_subagent` specifically (covering both live paths, since the base
  `send()` and Kokua's `stream_activity()` both route through it), and `conversation_to_frames` drops
  the matching replay item. Inside a card, the nested reasoning, tool calls, and generated text are the
  page's own top-level blocks reused: `addFoldable` and `renderTool` take a parent element, so a nested
  `💭 thinking` or `🔧 <name>` block is the same builder and the same CSS as its top-level counterpart
  rather than a card-specific style, and the generated text is its own assistant-styled markdown block.
  Each nests as an individually foldable block: thinking and tool calls start collapsed as at the top
  level, the answer starts expanded (it is what the reader opened the card for), and the card itself
  still starts collapsed showing only role, status, and the truncated task. The sub-agent's text now
  streams in live token by token and is re-rendered as markdown when the spawn ends, exactly as the
  assistant's own reply is; recorded events coalesce consecutive chunks into one entry per block, so a
  reloaded card looks the same while the stored JSON stays proportional to text length. The task is no
  longer duplicated between the (still-visible-when-collapsed) header and the body.
- **Tool-using reviewers**: the adversarial reviewers are now tool-enabled agents rather than a single
  tool-less call. Each runs a bounded tool-calling assessment over a curated verification toolset
  (`review.REVIEWER_TOOLS`: current date/time, web lookup, and computation) and then extracts the typed
  verdict in a follow-up structured call. This fixes reviewers rejecting correct answers they couldn't
  verify -- most visibly recency claims, since a context-free reviewer had no way to know today's date
  (Kokua injects the date into no prompt; it must be fetched via `get_current_date_and_time`, exactly as
  the main agent does). The toolset deliberately excludes the user's memory/documents, skills, and MCP
  mutation, so the reviewer stays an independent critic with no access to user state. Known limitation
  (see README): the toolset includes `execute_python` for calculations, and unlike the main agent the
  reviewer has no approval gate, so it can run code unattended during a review -- an intentional
  short-term tradeoff to revisit (sandbox the reviewer, or drop to `calculate`-only).
- **Reviewers grounded in fresh information**: two changes so reviewers stop rejecting correct answers as
  "hallucinated" by trusting their own stale training knowledge. (1) Both reviewer prompts now warn that
  the reviewer's built-in knowledge may be outdated, that disagreement with memory is not evidence of
  fabrication, and that it must verify a suspected inaccuracy with its tools before flagging (and note
  unverifiable claims as such rather than rejecting on suspicion). (2) The result reviewer is now shown an
  Evidence section -- the tool results the agent actually retrieved to produce its answer (extracted by
  `assistant._tool_evidence` from the executor transcript, each result truncated) -- so it judges against
  real sources, while still spot-checking with its own tools.
- **App-owned state**: all state under `~/.kokua` (override `KOKUA_HOME`), replacing the example's reliance
  on `aimu.paths.output`.
- **Strict config parsing**: an unknown key or non-table section in `config.toml` now fails fast with a
  `ConfigError` instead of being warned-about and ignored, so typos and removed keys surface immediately.
- **Model resolution failures surface cleanly**: with no `model` set, AIMU resolves `AIMU_LANGUAGE_MODEL`,
  else the first already-running local model (Ollama, then a local OpenAI-compatible server), and never a
  cloud model. When nothing resolves (or the model string is invalid), `Assistant.create` now raises a
  `ModelClientError` carrying AIMU's actionable message; the CLI prints it and exits non-zero and the web
  UI shows it in the chat, instead of a traceback. The web build failure also releases the single-connection
  guard, so a later connection is not wrongly refused as "busy in another tab".
- **Tests**: mock-only suite (assistant wiring, CLI parsing, MCP, memory, web channel + server round-trip,
  plugin discovery), with a vendored async mock model client (no reach into the AIMU repo).
- **Per-conversation agents**: each conversation now has its own AIMU `SkillAgent`, built lazily and kept
  in a bounded LRU `AgentRegistry` (new `agent_cache_cap` config, default 8), instead of a single shared
  agent that was swapped and `restore()`d between conversations on switch. Lays the groundwork for
  concurrent per-conversation turns in a follow-up. Because building an agent is now lazy, the web UI's
  new/select/delete-conversation controls can also hit a `ModelClientError` (e.g. a since-broken model
  string); these are now caught and reported in the chat, like the existing settings-panel handling,
  instead of tearing down the websocket connection.

- **Concurrent conversations**: a turn keeps running when you switch away, streaming only into the
  conversation you are viewing. Background turns post a completion notification instead of streaming.
  Switching conversations no longer cancels the in-flight turn. Tool approval prompts only for the
  conversation you are viewing; background and scheduled/proactive turns always auto-deny gated tools.
  Switching into (or connecting into) a conversation with a turn already running in the background shows
  a "working" indicator until that turn's next frame arrives. Shared memory and document tools are
  serialized with one lock so concurrent turns can't race on the stores.
