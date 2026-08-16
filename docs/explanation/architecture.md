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
  plugins.py           entry-point discovery for front ends and toolsets
  images.py            the on-disk image store and the /images/<name> reference
  logging_setup.py     rotating file log + a SIGUSR1 thread-stack dump
  config.example.toml  every key at its default, documented
  web_static/          the single-page web UI plus vendored marked/DOMPurify/KaTeX

  core/          the transport-agnostic runtime
    assistant.py         composition root + serve loop; delegates everything below
    conversations.py     ConversationBook: store + agent cache + active pointer
    tools.py             the `conversations` toolset: read-only sight of the user's other conversations
    turns.py             TurnRunner: reactive and proactive turns. Concurrency invariants live here.
    interaction.py       HumanGate: tool approval and plan review as lock-guarded single slots
    settings_runtime.py  SettingsApplier: read, apply live, persist, switch model
    commands are parsed inline in assistant._serve_channel (/stop, /diag, /plan)
    diagnostics.py       the /diag report
    build.py             free functions that assemble a model client and wire one declared agent
    subagents.py         SubagentReporter: sub-agent activity as display frames + recorded events
    agent_registry.py    per-conversation agent cache with LRU eviction and pinning
    turn_gate.py         writer-preferring readers-writer gate
    turn_registry.py     in-flight turn bookkeeping
    messages.py          transcript helpers: text extraction, titles, image compaction
    errors.py            describe_error: root-cause extraction for user-facing messages

  config/        settings: the schema, the file, the writers
    schema.py      AssistantConfig, AgentConfig, MCPServerConfig, the default prompt
    paths.py       the three locations that must resolve before config.toml can be read
    file.py        TOML discovery, parsing, schema validation
    store.py       comment-preserving tomlkit writes
    table.py       RUNTIME_SETTINGS: the one declaration of what is changeable at runtime
    tools.py       the assistant's own read_config / update_config

  planning/      runner.py (the /plan pipeline), reviewers.py (context-free reviewer agents)
  mcp/           servers.py (connect, attach, reconnect), tools.py (runtime add/remove), auth.py (ChatOAuth)
  scheduling/    recurrence.py (pure schedule math), registry.py (the JSON file), tools.py (agent tools)
  channels/      ui.py (ChannelUI), protocol.py (RichChannel), cli.py, web.py
  frontends/     cli.py, web.py -- registered as plugins, exactly like a third party's
  toolsets/      the one namespace of named capabilities
    registry.py    the Toolset dataclass, `register`, `select`, `build_tools`
    context.py     LiveState (process-wide shared state) and the per-agent ToolsetContext
    agents.py      builds the registry from every provider; resolves and validates one agent
    builtin.py     AIMU's tool groups, its two stores, and skills, wrapped as toolsets
    core.py        an index over the four TOOLSET constants in Kokua's subsystem tools.py files
    example.py, aimu_agents.py, pdf.py, image.py, email.py -- plugins, like a third party's
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
add/remove triggers, so a worker keeps reporting after its tools change. Displaying and
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

## Agents and delegation

Every agent is declared whole in `config.toml`, as one `[agents.<name>]` table carrying a
`description`, a `system_message`, a `tools` list of toolset names, and a `delegates_to` list. Nothing
about an agent is defaulted in code. `[assistant].agent` names the **entry agent**: the one Kokua
constructs directly, one per conversation. Every other agent exists only as a spawn target, built when a
delegator's `spawn_subagent` is constructed.

There is one agent shape, not two. `build.wire_agent` builds any agent from its declaration -- select its
toolsets, build their tools, attach the approval gate, attach its delegate if it has targets -- so a
conversation's agent cannot differ from a sibling's by accident, and there is no mode to branch on.

- **A declaration is the only way capability is granted.** A built-in group, an installed plugin toolset,
  and a connected MCP server all reach nothing until some agent's `tools` names them. Startup warns about
  a *provisioned* name nothing references (`agents.unreferenced_toolsets`, which excludes the AIMU and
  core toolsets that ship regardless), since a plugin loaded or a server connected to be unreachable is
  invisible otherwise and cost something.
- **Delegation nesting is Kokua's, not AIMU's.** AIMU's `max_depth` gives every level the same worker
  menu, which cannot express a graph where each agent has its own targets, so `build_agent_specs`
  recurses over `delegates_to` and calls AIMU with `max_depth=1` at every level. `validate_agents`
  proving the graph acyclic is what makes that recursion terminate, which is why a cycle is a startup
  error and not a style complaint.
- **A runtime MCP change is applied by rebuilding the delegate.** A worker's tools are snapshotted when
  `spawn_subagent` is built, so `add_mcp_server` fans `rebuild_delegation_tool` across every live agent
  rather than appending to any `agent.tools`.

At least one agent is therefore required, and `Assistant.create` refuses a config with none, or one whose
`[assistant].agent` names a table that does not exist.

What the *shipped* config declares is a lean entry agent: `kokua config init` gives
`[agents.assistant]` the cross-cutting toolsets (memory, documents, skills, config, `mcp-admin`,
scheduling, conversations, the clock) and no domain toolset, delegating web, filesystem, and compute work
to `researcher`, `coder`, and `generalist`. That keeps the always-on agent's tool context small, and the
prompt tells it so: `assemble_system_message` adds the "you are a lean supervisor, you MUST delegate"
clause only when every toolset the agent declared is `cross_cutting`. But that is a property of the
config, not a law of the code. Give `[agents.assistant]` a `tools = [..., "compute"]` and it gets
`execute_python`, loses the lean clause, and Kokua neither objects nor cares. The one structural
restriction is `entry_point_only`: `skills` works solely on the entry agent, because a spawned worker is a
plain AIMU `Agent` rather than a `SkillAgent`, so skill injection would have nothing to hook.

### How an agent's tools resolve

Four steps, all in `toolsets/`. The first two run once at startup, before Kokua opens or connects to
anything; the last two run once per agent, whenever one is built:

1. **Build the namespace.** `agents.build_registry(config)` collects toolsets from four labeled
   providers -- AIMU capability (`builtin.py`), core subsystem (`core.py`), MCP server (one per
   `[[mcp.server]]`, named by its required `name`), and plugin (the `kokua.toolsets` entry-point group,
   skipped entirely when `load_plugins` is off) -- and `registry.register` rejects a name two providers
   claim. A `Toolset` is a frozen dataclass: `name`, `description`, `build`, `guidance`, `cross_cutting`,
   `entry_point_only`.
2. **Validate.** `agents.validate_agents` runs before the session store is opened or any server
   connected, so an unknown toolset name, a missing entry agent, an unknown delegation target, a cycle,
   or `skills` on a worker fails with nothing written and nothing connected.
3. **Select.** `registry.select(names, registry, agent=, entry_point=)` returns the declared toolsets in
   declared order, deduplicated, raising on an unknown name (with the available ones listed) or an
   `entry_point_only` toolset declared elsewhere. It never drops: dropping is what the previous per-role
   lists did, and a typo silently produced a smaller toolset.
4. **Build.** `registry.build_tools(toolsets, ctx)` calls each `Toolset.build(ctx)` and concatenates,
   deduplicating by `__name__` and keeping the first, so declared order decides a collision. `ctx` is a
   `ToolsetContext`: the one process-wide `LiveState` plus this agent (`None` for a spawned worker).
   `build` must create only closures, never process state -- every shared singleton (the memory store,
   the document store, the `SkillManager`, the scheduler tools) is a lazy property on `LiveState`, so two
   agents declaring one toolset share one rather than constructing two, and the two stores are opened only
   because some agent declared them. The `SkillManager` and the scheduler are the exceptions to that last
   half, and not by accident: every agent is a `SkillAgent` and so takes the manager regardless, and
   `arm_tasks` has to fire a persisted scheduled task whether or not any agent can talk about scheduling.

The prompt is assembled from the same selected list, in `agents.assemble_system_message`. For the entry
agent, a `--system` flag wins outright over its declared opener; a worker's declared opener is never
touched by the flag. Absent an override, it is the agent's own `system_message` (falling back to
`[assistant].system_message`, then the built-in default), then each toolset's `guidance` in declared
order, then `DELEGATION_GUIDANCE` if `delegates_to` is non-empty, then `LEAN_DELEGATION_GUIDANCE` if every
selected toolset is `cross_cutting`. Guidance travelling with the capability is the point: installing a toolset brings the
instructions that make the model use it, and removing one takes them away, with no prompt constant to
keep in step by hand. `wire_agent` selects once and passes the same list to both the message and the
tools, so the two cannot resolve different toolsets for the same names.

#### The shipped entry agent's inventory

All 27 tools the shipped `[agents.assistant]` table resolves to, and where each comes from. This is what
`config.example.toml` declares, not a fixed list: a different `tools` line produces a different set.
Roughly half come from AIMU and so are not greppable in this repository, which is why this table exists
rather than a naming convention alone:

| Tools | Built by | Declared as |
|---|---|---|
| `author_skill`, `add_skill_script` | AIMU `make_skill_authoring_tool` / `make_skill_script_tool` | `skills` (entry agent only) |
| `store_memory`, `search_memories`, `list_memories` | AIMU `make_memory_tools` | `memory` |
| `save_document`, `read_document`, `list_documents`, `search_documents` | AIMU `make_document_tools` | `documents` |
| `get_current_date_and_time`, `convert_time` | AIMU `builtin.time` | `time` |
| `add_mcp_server`, `remove_mcp_server` | `mcp/tools.py` | `mcp-admin` |
| `read_config`, `update_config` | `config/tools.py` | `config` |
| `schedule_task`, `list_scheduled_tasks`, `get_scheduled_task`, `update_scheduled_task`, `cancel_scheduled_task`, `enable_scheduled_task`, `disable_scheduled_task`, `run_scheduled_task` | `scheduling/tools.py` | `scheduling` |
| `list_conversations`, `read_conversation`, `search_conversations` | `core/tools.py` | `conversations` |
| `spawn_subagent` | AIMU `make_async_subagent_tool` | implied by a non-empty `delegates_to` |

Two conventions keep this honest. Kokua-side agent tools live in a subsystem's `tools.py` (which also
exports that subsystem's `TOOLSET`) and nowhere else, so `grep -rl '@tool' src/kokua/` finds exactly
those four files plus `toolsets/`. And `test_entry_agent_toolset_is_exactly_the_documented_inventory` in
`tests/core/test_build.py` asserts the built agent's tool names as an **exact set** mirroring this table,
so adding a tool to the entry agent fails the suite until the table is updated, and a plugin toolset
leaking onto it fails too. Documentation alone would have rotted; the test is what makes the table
trustworthy.

The pattern for a new Kokua capability: a `make_*_tools(...)` factory in the owning subsystem's
`tools.py`, closing over the live state it needs, plus a `TOOLSET` in that same module whose `build`
pulls that state off the context, added to `toolsets/core.py`'s index. A capability that needs nothing but
`AssistantConfig` should be a plugin toolset instead. Either way it reaches an agent only when a
`[agents.*]` table names it.

### Reading across conversations

`core/tools.py` defines `list_conversations`, `read_conversation`, and `search_conversations`, wrapped as
the `conversations` toolset. Two decisions in it are worth knowing before changing them.

The shipped config gives it to the entry agent and to no worker, deliberately: a worker shares no history
and has no conversation identity, so "the user's other conversations" means nothing to it, and granting
the capability would widen a spawn's blast radius for no gain. When a worker needs history, the entry
agent reads first and puts the text into the spawn's task. Nothing in the code forbids declaring
`conversations` on a worker; a test pins that no shipped worker does.

Every read goes through the store, never `ConversationBook.agent_for`. Building an agent to read it
would allocate a model client, re-expand every stored image, mutate the LRU registry (so reading twenty
conversations would evict the live agent of one with a running turn), and can raise `ModelClientError`
for reasons unrelated to reading. It is also *less* correct: a running turn appends to
`agent.model_client.messages` in place, so a reader in another task can observe a half-written turn,
while the store holds a snapshot written once at the end of a turn. The two markers close the gap the
snapshot opens -- `turn_running` flags unsaved messages, and the active conversation is flagged as the
one whose current turn the model should read out of its own context. A test constructs a book with no
registry bound, so any accidental `agent_for` path fails rather than passing quietly.

`ConversationBook.sessions()` is the single place the store's key-then-get walk and the `updated_at`
ordering live; `list()` projects it and `most_recent_or_new` takes its head. It costs one store read per
conversation, which is already what a sidebar push costs. A bulk read belongs in AIMU's store, and this
is the seam it would land behind.

## Plugins

Two entry-point groups: `kokua.frontends` (a `FrontEnd` with `run(config, args)`) and `kokua.toolsets`
(a `Toolset` with `build(ctx)`). The built-in `cli`/`web` front ends and the five plugin toolsets are
registered in Kokua's own `pyproject.toml` exactly as a third party would register theirs;
`plugins.py` discovers them at runtime, and `kokua.plugins` re-exports `Toolset` and `ToolsetContext`
as the public surface a third party imports. Add a transport or new tools as a plugin, not by editing
the core -- see [toolsets/example.py](../../src/kokua/toolsets/example.py).

A plugin toolset is only distinguished from a built-in one by its provider label in `--list-toolsets`
and by one thing: a plugin's `build` is wrapped so a raised exception is logged and yields no tools,
while a core or AIMU toolset failing to build is a bug in this repository and stays loud.

A toolset is also how a whole *agent* arrives. Every AIMU `Runner` exposes `.run(task) -> str`, so
mounting one needs no core surface at all: `build()` returns a callable that runs it.
[toolsets/aimu_agents.py](../../src/kokua/toolsets/aimu_agents.py) does this for AIMU's three
prebuilt orchestrators and is the reference for wiring your own. It builds its agent inside the tool
call rather than in `build()`, because `build()` runs once per agent and constructing a
sync `ModelClient` is what loads weights on an in-process provider -- and because a cached
orchestrator's `messages` would be shared across concurrent calls.

## Configuration

Precedence is **CLI flag > TOML config file > built-in default**. `config/schema.py` holds
`AssistantConfig` (a plain dataclass, with leaf paths derived from `data_dir`); `config/file.py` finds
and parses the TOML into validated overrides; `cli.resolve_config` merges the file under the CLI
flags. Flag defaults are the `None` sentinel, so an unspecified flag defers to the file.

The file itself is **required**: `config/file.py::load` raises rather than returning no overrides when
it is missing. Agents live only in `[agents.*]` and the assistant cannot function without at least one,
so there is no useful unconfigured state to degrade to. `Assistant.create` enforces the companion rule
and refuses a config that defines zero agents, or whose `[assistant].agent` names none of them.
Individual keys keep their built-in defaults.

Keys the old per-role vocabulary used are not silently ignored. `[tools]`, `[subagents]`,
`[assistant].memory`, and a per-agent `groups` / `tool_packs` / `mcp_servers` each raise a targeted
`ConfigError` naming the replacement, checked ahead of the schema so an old file gets that message
rather than a generic unknown-key one.

`config.toml` is the single source of settings **and the app writes it**. `config/store.py` does
comment-preserving writes via `tomlkit` (stdlib `tomllib` cannot write). Three writers: the web
settings panel, the `add_mcp_server`/`remove_mcp_server` tools, and the assistant's own
`update_config`. `update_config` refuses a security blocklist (`confirm_tools`, `email.to`,
`data_dir`) plus the whole `[agents.*]` section, matched by section prefix since agent names cannot be
enumerated in advance, and applies hot-appliable keys live.

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
`config.mcp_servers`). Each one is also a toolset, named by its required `name`, which is how an agent
reaches it. The runtime `add_mcp_server` tool appends reconnectable servers there via
`config/store.py` (no secret on disk), so config.toml stays the one source; it writes a name derived
from the server's host and disambiguated against the names already on file, so a successful add can
never leave behind a config the registry's collision check would reject at the next boot. That derived
name reaches no agent until a human adds it to an `[agents.*]` table, since that section is hand-edit
only: the tool can connect a server but cannot grant itself the capability. `mcp/auth.py` handles OAuth
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

### The settings panel and the tasks section

Two page surfaces are pure front-end concerns and deliberately absent from `RichChannel`: the settings
panel (`settings` / `get_settings`) and the scheduled-tasks section (`tasks` / `get_tasks` / `task`).
The core never sends either frame, so there is no capability for `ChannelUI` to probe and no fallback to
document; `Assistant` exposes only accessors (`current_settings` / `apply_settings`,
`list_tasks` / `task_action`) and `frontends/web.py`'s pump handles the control frames. A new transport
that wants a task list implements its own; it is not a hole in the channel contract.

`task_action` is the seam that keeps the panel honest. Every task mutation pairs a registry write with
the scheduler (un)arming that must accompany it, and both the agent's tools and the panel go through the
same `scheduling.TaskControls` handle that `make_scheduler_tools` returns alongside the tools, so the two
cannot drift: a front end that edited `scheduled_tasks.json` directly would leave the in-memory scheduler
firing a task the registry calls disabled. The action name arrives from the browser, so it is looked up
in a table and raises for anything else rather than being dispatched on.

Grouping a task's conversations under it happens **on the page**, not in the core. `new_session` stamps
`metadata["task_id"]` on any conversation a firing mints (the task's id, not its name, since a name is
optional and `update_scheduled_task` can change it) and `ConversationBook.list()` projects it, but nothing
is filtered there -- the agent's read-only conversation tools walk `sessions()` and still see every
conversation. The page nests a conversation under a task when its `task_id` matches a task *currently in
the list*, or when it is the `session_id` a `target="task"` record remembers (the only link for a
conversation minted before `task_id` was recorded). Requiring the task to be present is what makes
deleting a task return its conversations to the chat list instead of hiding them: keying on `task_id`
alone would leave an orphan unreachable from the sidebar. Because the nesting is client-side, a firing's
new conversation appears under its task with no task re-fetch -- `TurnRunner.proactive` already pushes the
conversation list. The reverse is not true: a task the model schedules or cancels mid-chat does not push a
`tasks` frame, which is what the section's refresh button is for.

A `tool` frame carries what the call returned as well as what it was asked to do (`response`, added to
AIMU's base frame), because AIMU yields `TOOL_CALLING` only once the call has been dispatched. An error,
an argument-binding failure, and a denied approval are results too and arrive on that same key, so the
page needs no separate failure path and no second frame updating a card in place: the card is complete
when it appears. `renderTool` puts the result in a nested foldable of its own below the arguments, filled
on first expand and clamped to `OUTPUT_CLAMP` characters with a button for the rest, as plain text --
never markdown, since a tool result is untrusted input. Output travels whole and only the DOM clamps, so
a `history` frame grows by every tool result in the conversation and is re-sent on every conversation
switch; if that becomes a problem the fix is a server-side cap in `conversation_to_frames`, the path that
re-sends.

Replay reaches the same card by a different route. A stored transcript splits a call from its result
across an assistant message's `tool_calls` and a later `role: "tool"` message, joined by
`tool_call_id`; `_tool_results_by_call_id` builds that map so each replayed call carries its own output.
The join has to be by id rather than by position, since concurrent dispatch appends results in completion
order. A call with no matching result -- a transcript stored before results were replayed, a turn cut
short mid-dispatch -- gets `response: None` and renders the card exactly as it always did.

Muting a background turn happens per frame, in `WebChannel.send_frame`, which every live frame passes
through. The rule is a property of the frame's *type*: the `_TURN_FRAMES` set is turn output (tokens,
thinking, tool calls, the message, `done`, loop markers, images, plan bubbles, phases, sub-agent cards)
and is dropped when `streaming_conversation` names a conversation other than the one being viewed;
everything else is channel state (the sidebar, replayed history, settings, notifications, human-decision
prompts) and always goes out. Neither half can be decided by task context alone. Hoisting the check out
of a streaming loop -- taking it once when a reply starts -- is what let a switch mid-reply append the
rest of the old turn's tokens to the conversation the user had just moved to, since the viewed
conversation changes *during* the send that is being gated. And gating purely on the contextvar would
drop a background turn's sidebar refresh, because `TurnRunner._persist` pushes the conversation list from
inside that turn's own task.

Muting a turn is not losing it. Every turn frame is also folded into a per-conversation
`_CatchUpRecord`, which models the page's own append rules (consecutive thinking text collects into one
foldable; answer text collects into one open `partial` bubble that floats below later reasoning) and is
appended to the items of the `history` frame a switch-in sends. That is why a conversation whose turn is
still running replays the turn so far and then streams the rest into the same bubble: none of it is in
the store until `_persist`, and a `history` frame replaces the transcript wholesale. Riding on the
existing frame rather than replaying separate frames is deliberate -- a live frame from the running turn
can land between two awaits, which would both misorder the catch-up and duplicate that frame. The core
owns the record's lifetime (`ChannelUI.begin_catch_up` / `end_catch_up`), since only it knows when a turn
starts; it ends the record inside `_persist`, next to the write that supersedes it, so no switch-in can
land in a window where both the store and the record would render the same turn.

An unattended turn records the same way, and needs it most: a scheduled task running in its own
conversation is never the conversation being viewed, and its spawns' cards are the only display frames it
produces at all, so without a record switching into a running task showed an empty transcript and the
spawn's later `append` frames then arrived with no card to update. It opens the record *inside* its gate
hold rather than beside the turn contextvars, because a firing can queue behind a turn already running on
that conversation and would otherwise replace a record still standing in for live output.

Planning's reviewer verdicts and a turn's spawned sub-agents share one `subagent` frame type and one
persisted map (`metadata["subagent"]`); `task` on the create event is what tells the two apart.
`conversation_to_frames` replays that map on reload, interleaved right after its user bubble; a
verbose-traced turn suppresses the reviewer verdict cards (their content is already in the raw trace)
but still replays its own spawn cards, since a sub-agent it spawned is not part of that trace -- kept by
the create event's id, not by event shape, since a spawn whose text streamed closes with a status-only
event indistinguishable from a reviewer's verdict. The
page's `renderSubagent` builds or updates one foldable card per id, filling its body as `append` frames
arrive; nested reasoning honors `show_thinking` and nested tool calls honor `show_tools`, the same flags
the top-level turn uses, while the card itself and its generated text always show.

A card's body is built from the page's own top-level components, not from card-specific markup:
`addFoldable` and `renderTool` take an optional parent element (`opts.parent`), so a nested `thinking`
or `tool <name>` row is literally the same builder (and the same CSS) as its top-level counterpart --
including the nested output foldable, so a spawn's own tool calls show their results too. The
generated text is an assistant-styled markdown block. Each nests as its own foldable: thinking and tool
calls start collapsed, as they do at the top level, and the answer starts expanded, since it is what a
reader opens the card for. The card itself starts collapsed, and a spawn card takes the shape of the
`spawn_subagent` tool block the channel suppressed for it: a monospace
`subagent  spawn_subagent(<role>)  <status>` header over an argument line built by the same `toolArgs`
helper a tool block's body uses. The arguments are reconstructed on the page from the frame's `role`
and `task`, which is faithful because Kokua only ever builds AIMU's typed spawn tool. A reviewer card,
which no tool call backs, reads `review  <role>  <status>` instead, and that kind word is the only
at-a-glance difference between the two, which is why an e2e test pins it. Generated text streams in
chunk by chunk as plain text
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
