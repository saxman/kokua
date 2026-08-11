# Architecture

Kokua wraps [AIMU](https://saxman.info/aimu/) primitives into a single-user, always-on personal
assistant. The design goal is a small core with capability pushed into plugins; see
[design principles](design-principles.md) for why.

The AIMU pieces Kokua is built from are its
[personal-assistant primitives](https://saxman.info/aimu/how-to/build-personal-assistant/): a `Channel`
transport, a `Scheduler`, and a `SkillAgent` that authors its own
[skills](https://saxman.info/aimu/how-to/use-skills/). Kokua adds persistence, configuration,
multiple conversations, human-in-the-loop gates, and the plugin system around them. Capability
questions -- which providers, which tools, which models support vision -- are answered in
[AIMU's docs](https://saxman.info/aimu/), not here.

## Repository layout

```
src/kokua/
  cli.py               argparse surface, CLI-over-TOML merge, `config init`, main()
  plugins.py           entry-point discovery for front ends and tool-packs
  images.py            the on-disk image store and the /images/<name> reference
  logging_setup.py     rotating file log + a SIGUSR1 thread-stack dump
  config.example.toml  every key at its default, documented
  web_static/          the single-page web UI plus vendored marked/DOMPurify/KaTeX

  core/          the transport-agnostic runtime
    assistant.py         composition root + serve loop; delegates everything below
    conversations.py     ConversationBook: store + agent cache + active pointer
    turns.py             TurnRunner: reactive and proactive turns. Concurrency invariants live here.
    interaction.py       HumanGate: tool approval and plan review as lock-guarded single slots
    settings_runtime.py  SettingsApplier: read, apply live, persist, switch model
    commands are parsed inline in assistant._serve_channel (/stop, /diag, /plan)
    diagnostics.py       the /diag report
    build.py             free functions that assemble a model client, memory, tools, an agent
    subagents.py         SubagentReporter: sub-agent activity as display frames + recorded events
    agent_registry.py    per-conversation agent cache with LRU eviction and pinning
    turn_gate.py         writer-preferring readers-writer gate
    turn_registry.py     in-flight turn bookkeeping
    messages.py          transcript helpers: text extraction, titles, image compaction
    errors.py            describe_error: root-cause extraction for user-facing messages

  config/        settings: the schema, the file, the writers
    schema.py      AssistantConfig, MCPServerConfig, the default prompts
    paths.py       the three locations that must resolve before config.toml can be read
    file.py        TOML discovery, parsing, schema validation
    store.py       comment-preserving tomlkit writes
    table.py       RUNTIME_SETTINGS: the one declaration of what is changeable at runtime
    tools.py       the assistant's own read_config / update_config

  planning/      runner.py (the /plan pipeline), reviewers.py (context-free reviewer agents)
  mcp/           servers.py (connect, attach, reconnect, runtime add/remove), auth.py (ChatOAuth)
  scheduling/    recurrence.py (pure schedule math), registry.py (the JSON file), tools.py (agent tools)
  channels/      ui.py (ChannelUI), protocol.py (RichChannel), cli.py, web.py
  frontends/     cli.py, web.py -- registered as plugins, exactly like a third party's
  toolpacks/     example.py, pdf.py, image.py, email.py -- likewise
```

`tests/` mirrors this layout.

## The core

`Assistant` ([core/assistant.py](../../src/kokua/core/assistant.py)) is the composition root and the
serve loop, and little else. It owns:

- **`ConversationBook`** -- the session store, the per-conversation agent cache, and which
  conversation is being viewed. These move together on a switch, which is why they are one object.
- **`TurnRunner`** -- reactive turns (the user sent something) and proactive turns (a scheduled task
  fired). The six concurrency invariants are documented at the top of that module.
- **`HumanGate`** -- tool approval and plan review, each a lock-guarded single-slot request the serve
  loop resolves with the user's next message.
- **`SettingsApplier`** -- reading, applying, and persisting the runtime-mutable settings.
- **`ChannelUI`** -- the only view of the outside world.

Non-obvious control flow: the serve loop runs each reactive turn as a background `aio.RunHandle`, so
the channel keeps reading during a turn. That is what lets a `/stop` cancel an in-flight reply, and
what lets a web approval reply be routed back to the waiting tool call. Switching conversations does
**not** cancel a running turn: each conversation owns its own agent and client, so a backgrounded turn
persists to its own conversation, streams muted, and posts a notification when it finishes. Only
`delete_conversation` cancels, and only the deleted conversation's own turn.

A turn's spawned sub-agents surface the same way. `core/subagents.py`'s `SubagentReporter` implements
AIMU's `SubagentObserver` protocol; `Assistant` owns one instance per connection (`Assistant.create`
runs once per WebSocket connection), and `build.py` threads it into every per-conversation agent's
`spawn_subagent` as the `observer=` argument -- including across the rebuild a runtime MCP server
add/remove triggers, so a worker role keeps reporting after its toolset changes. Displaying and
recording are deliberately separate. `TurnRunner` sets a `subagent_events` ContextVar collector for the
duration of a turn, installed and reset alongside `streaming_conversation` under the same invariant (see
the module's concurrency invariants): the reporter appends each spawn's frames to whichever list is
current, and the turn records that list, synchronously, as the first action of whichever exit branch
runs -- success, cancellation, connection error, or generic error. On the cancelled and error branches,
which still await one more status send afterward (the "(stopped)" notice, or the failure message),
recording first means a second cancellation racing that send cannot propagate past the completed record
call and drop the turn's events. `ConversationBook.record_subagent_events` persists the result into
`session.metadata["subagent"][str(user_index)]`, the same map planning's
reviewer verdict cards write into (a shared merge helper extends rather than overwrites, since one turn
can produce both). A background turn's cards are muted like the rest of what it streams, but its spawns
are still recorded, so switching into that conversation later shows the work.

Two deviations from a literal live replay are deliberate. Reasoning and generated text are coalesced
into one entry per block when recorded -- matching how the page already concatenates consecutive chunks
of one kind when it displays them -- but streamed chunk by chunk when shown live, which keeps the stored
JSON proportional to text length rather than to token count. A block is closed by anything recorded
after it, so a multi-round spawn gets one answer entry per round for free: the round's own tool call
sits in between. And a turn's spawn cards replay grouped right after its user bubble on reload, not at
the exact point mid-turn where they appeared live. Separately, a gated tool
call inside a sub-agent (e.g. `execute_python`) still prompts at the top level, not inside its card: the
approval gate is forwarded to the parent's existing prompt, not rendered into the spawn's own card.

## Supervisor and workers

There is one agent shape, not two. Every per-conversation agent is a **lean supervisor**: it mounts
only the tools that mutate shared, per-conversation state (skills, MCP connections, memory, config,
scheduling), the `time` group, and the `spawn_subagent` delegate. It carries no built-in tool group, no
tool-pack tool, and no MCP callable. Those live on the **workers**, scoped by the roles in
`[subagents.roles.*]`, and `build.build_agent` is a straight line with no mode to branch on.

Three consequences follow, and they are the things that surprise people:

- **A role is the only way capability is granted.** A built-in group, an installed tool-pack, and a
  connected MCP server all reach nothing until some role names them. Startup warns about an MCP server
  no role references (`build.unreferenced_mcp_servers`), since that one is invisible otherwise and
  costs a live connection.
- **`[tools].groups` is a ceiling, not a toolset.** It bounds what a role may draw on; a role's own
  `groups` are intersected with it. Since nothing else expands that list any more,
  `build.enabled_tool_groups` is also the only place a typo in it is caught.
- **A runtime MCP change is applied by rebuilding the delegate.** Roles snapshot their toolsets when
  `spawn_subagent` is built, so `add_mcp_server` fans `rebuild_subagent_tool` across every live agent
  rather than appending to any `agent.tools`.

At least one role is therefore required, and `Assistant.create` refuses a config with none: a
supervisor with no workers cannot browse, read a file, or compute, so starting one produces something
that looks alive and cannot work.

## Plugins

Two entry-point groups: `kokua.frontends` (a `FrontEnd` with `run(config, args)`) and `kokua.tools` (a
`ToolPack` with `build(config)`). The built-in `cli`/`web` front ends and the five tool-packs are
registered in Kokua's own `pyproject.toml` exactly as a third party would register theirs;
`plugins.py` discovers them at runtime. Add a transport or new tools as a plugin, not by editing the
core -- see [toolpacks/example.py](../../src/kokua/toolpacks/example.py).

A tool-pack is also how a whole *agent* arrives. Every AIMU `Runner` exposes `.run(task) -> str`, so
mounting one needs no core surface at all: `build()` returns a callable that runs it.
[toolpacks/aimu_agents.py](../../src/kokua/toolpacks/aimu_agents.py) does this for AIMU's three
prebuilt orchestrators and is the reference for wiring your own. It builds its agent inside the tool
call rather than in `build()`, because `build()` runs once per conversation agent and constructing a
sync `ModelClient` is what loads weights on an in-process provider -- and because a cached
orchestrator's `messages` would be shared across concurrent calls.

## Configuration

Precedence is **CLI flag > TOML config file > built-in default**. `config/schema.py` holds
`AssistantConfig` (a plain dataclass, with leaf paths derived from `data_dir`); `config/file.py` finds
and parses the TOML into validated overrides; `cli.resolve_config` merges the file under the CLI
flags. Flag defaults are the `None` sentinel, so an unspecified flag defers to the file.

The file itself is **required**: `config/file.py::load` raises rather than returning no overrides when
it is missing. Sub-agent roles live only in `[subagents.roles.*]` and the assistant cannot function
without at least one (it delegates all specialized work), so there is no useful unconfigured state to
degrade to. `Assistant.create` enforces the companion rule and refuses a config that defines zero
roles. Individual keys keep their built-in defaults.

`config.toml` is the single source of settings **and the app writes it**. `config/store.py` does
comment-preserving writes via `tomlkit` (stdlib `tomllib` cannot write). Three writers: the web
settings panel, the `add_mcp_server`/`remove_mcp_server` tools, and the assistant's own
`update_config`. `update_config` refuses a security blocklist (`confirm_tools`, `email.to`,
`data_dir`) and applies hot-appliable keys live.

Which settings are hot is not a list maintained by hand in several places: it is
`config/table.py`'s `RUNTIME_SETTINGS`, and every consumer loops over it.

## State

Everything lives under `~/.kokua` (override with `KOKUA_HOME`). `config.toml` sits at the root;
`data/` holds only content: `sessions.json`, `skills/`, `memory/`, `documents/`, `downloads/`,
`images/`, `scheduled_tasks.json`.

## Images

`images.py` owns the on-disk store and the `/images/<name>` reference. Input images (web upload/paste,
CLI `/attach`) and generated images live under `images_path`; the web server serves them at
`/images/<name>`. Non-obvious: AIMU inlines a base64 data URL into stored message content, but a
persisted session must stay small and a localhost URL is not fetchable by the provider, so
`core/messages.py` rewrites data URLs to references on persist and re-inlines them before each
`agent.restore`.

## MCP

All servers come from `[[mcp.server]]` at startup (`mcp.reconnect_mcp_servers` is a single pass over
`config.mcp_servers`). The runtime `add_mcp_server` tool appends reconnectable servers (URL only, no
secret) there via `config/store.py`, so config.toml stays the one source. `mcp/auth.py` handles OAuth
by posting the authorization link into the chat and persisting tokens to disk.

## Planning

`/plan` (or the web Plan toggle) runs one turn through `planning/runner.py`: draft a plan, optionally
have an independent reviewer critique it and a human approve it, execute, optionally review the
result. There is one pipeline; how much of its work is shown is a `Presentation` value with two
instances, `SUMMARY` and `VERBOSE` (the latter selected by `show_reasoning` on a channel that can
render phase headers).

## Web front end

`frontends/web.py` is a Starlette + uvicorn WebSocket server (behind the `web` extra);
`channels/web.py`'s `WebChannel` subclasses
[AIMU's base `WebChannel`](https://saxman.info/aimu/how-to/build-personal-assistant/). The streaming transport
(`token`/`thinking`/`tool`/`done` frames and `send()`) lives in AIMU's base; Kokua's subclass adds the
`conversations`, `history`, and `approval` frames its richer page needs. The UI is a single
self-contained `web_static/index.html` served as package data, plus vendored `marked` + `DOMPurify`
(GitHub-flavored markdown, sanitized, rendered client-side on turn completion) and vendored KaTeX,
typeset after sanitization with `trust:false`. The server allowlists these assets: JS/CSS by name, the
woff2 fonts under `/fonts/`.

Planning's reviewer verdicts and a turn's spawned sub-agents share one `subagent` frame type and one
persisted map (`metadata["subagent"]`); `task` on the create event is what tells the two apart.
`conversation_to_frames` replays that map on reload, interleaved right after its user bubble; a
verbose-traced turn suppresses the reviewer verdict cards (their content is already in the raw trace)
but still replays its own spawn cards, since a sub-agent it spawned is not part of that trace. The
page's `renderSubagent` builds or updates one foldable card per id, filling its body as `append` frames
arrive; nested reasoning honors `show_thinking` and nested tool calls honor `show_tools`, the same flags
the top-level turn uses, while the card itself and its generated text always show.

A card's body is built from the page's own top-level components, not from card-specific markup:
`addFoldable` and `renderTool` take an optional parent element, so a nested `💭 thinking` or
`🔧 <name>` block is literally the same builder (and the same CSS) as its top-level counterpart, and the
generated text is an assistant-styled markdown block. Each nests as its own foldable: thinking and tool
calls start collapsed, as they do at the top level, and the answer starts expanded, since it is what a
reader opens the card for. The card itself starts collapsed, and a spawn card takes the shape of the
`spawn_subagent` tool block the channel suppressed for it: a monospace `🔧 spawn_subagent(<role>) —
<status>` header over an argument line built by the same `toolArgs` helper a tool block's body uses.
The arguments are reconstructed on the page from the frame's `role` and `task`, which is faithful
because Kokua only ever builds AIMU's typed spawn tool. A reviewer card, which no tool call backs,
keeps its plain `🔎 <role> — <status>` header. Generated text streams in chunk by chunk as plain text
and is re-rendered as markdown
when the spawn hits its terminal status, the same two steps the assistant's own reply takes, so a
replayed card (which also ends with a terminal status) lands on the same DOM.

## Testing

Tests are mock-only. `tests/helpers.py` provides `MockAsyncModelClient`; `tests/channels.py` and
`tests/fakes.py` hold the shared channel and MCP/client doubles; `tests/conftest.py` redirects
`KOKUA_HOME` to a temp dir so tests never touch real state. The mock **fakes tool-call rounds** rather
than running AIMU's real dispatch, so features that hook dispatch (the tool-approval gate) are tested
by calling `agent._prepare_run()` then `agent.model_client._handle_tool_calls([...])` directly.

Client-side page JS is covered by an **opt-in** end-to-end suite
(`tests/frontends/test_web_e2e.py`, marked `e2e`, deselected by default): it drives the real
`index.html` in headless Chromium against a live server backed by a mock client. Run it with
`uv run pytest -m e2e` (needs the `web` extra + `uv run playwright install chromium`); it is skipped,
not errored, when those are absent, and it does not gate the default suite.

## See also

- [Design principles](design-principles.md): why the shape above is the shape.
- [AIMU documentation](https://saxman.info/aimu/): the library everything above is built on --
  [providers and model strings](https://saxman.info/aimu/how-to/switch-providers/),
  [tools](https://saxman.info/aimu/reference/api/tools/),
  [MCP](https://saxman.info/aimu/how-to/use-mcp-tools/),
  [memory](https://saxman.info/aimu/how-to/use-semantic-memory/),
  [sub-agents](https://saxman.info/aimu/how-to/spawn-subagents/), and the
  [environment variables](https://saxman.info/aimu/reference/env-vars/) Kokua inherits.
