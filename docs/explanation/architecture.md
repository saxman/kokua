# Architecture

Kokua wraps [AIMU](https://saxman.info/aimu/) primitives into a single-user, always-on personal
assistant. The design goal is a small core with capability pushed into plugins, and behind that goal is
the reason the project exists: people should be able to learn how an agentic system works by reading,
running, and extending a real one. See
[why Kokua exists](design-principles.md#why-kokua-exists) and the six principles that serve it.

This page is the reading path for the first of those three. It goes in dependency order rather than
alphabetically, so each section can assume the one above it.

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
  transcript_export.py render_markdown: a saved conversation as Markdown a person can read and judge,
                        imports no channel and no front end, so the CLI export works without `web`
  config.example.toml  every key, one line each (long form: docs/reference/configuration.md)
  web_static/          the single-page web UI plus vendored marked/DOMPurify/KaTeX

  core/          the transport-agnostic runtime
    assistant.py         composition root + serve loop; delegates everything below
    conversations.py     ConversationBook: store + agent cache + active pointer, and id resolution
    transcripts.py       reading a stored conversation as text (flatten, truncate, search) or as full
                          replay items (replay_items), shared by the web channel's history replay and
                          the Markdown export
    turns.py             TurnRunner: reactive and proactive turns. Concurrency invariants live here.
    interaction.py       HumanGate: tool approval and a workflow's own decision, as lock-guarded single slots
    settings_runtime.py  SettingsApplier: read, apply live, persist
    commands: /stop, /diag, and the three conversation commands are parsed inline in
              assistant._serve_channel; a workflow's own command (e.g. /plan) dispatches through
              self._workflows, built from the toolset registry
    diagnostics.py       the /diag report
    conversation_commands.py  what /new, /conversations, and /switch print
    build.py             free functions that assemble a model client and wire one declared agent
    agents.py            assembles the registry from every provider; resolves, validates, and
                         prompts one declared agent, and builds its delegation tool
    subagents.py         SubagentReporter: sub-agent activity as display frames + recorded events
    agent_registry.py    per-conversation agent cache with LRU eviction and pinning
    turn_gate.py         writer-preferring readers-writer gate
    turn_registry.py     in-flight turn bookkeeping
    messages.py          transcript helpers: text extraction, the placeholder title, image compaction
    titles.py            the generated conversation title: one context-free model call
    errors.py            describe_error: root-cause extraction for user-facing messages
    metrics.py           TurnMetrics + record_event: what a turn cost, accumulated from AIMU's
                          ModelTurnFinished events into calls/tokens/seconds

  config/        settings: the schema, the file, the writers
    schema.py      AssistantConfig, AgentConfig, MCPServerConfig, the default prompt
    paths.py       the three locations that must resolve before config.toml can be read
    file.py        TOML discovery, parsing, schema validation
    store.py       comment-preserving tomlkit writes, the write policy, and apply_setting
    table.py       SettingsTable, built from CORE_RUNTIME_SETTINGS plus every toolset's declared hot
                   settings: the one declaration of what is changeable at runtime
    settings_sources.py  joins a toolset's declared settings into the table; the one module under
                   config/ that imports upward, so the rest of the layer stays at the bottom

  workflows/     protocol.py (Workflow, WorkflowContext, WorkflowResult, is_rich), critics.py (the
                 shared context-free reviewer), planning/ (runner.py's PlanningWorkflow, prompts.py,
                 critics.py's thin wrappers over the shared reviewer)
  mcp/           servers.py (connect, attach, reconnect, runtime add/remove), auth.py (ChatOAuth)
  scheduling/    recurrence.py (pure schedule math), tasks.py (TaskService, over the `[scheduling.task.*]`
                 tables in config.toml)
  channels/      ui.py (ChannelUI), protocol.py (RichChannel), cli.py, web.py
  frontends/     cli.py, web.py -- registered as plugins, exactly like a third party's
  registry/      the machinery a toolset is built against, and no toolsets
    registry.py    the Toolset and Setting dataclasses, `register`, `select`, `build_tools`
    context.py     LiveState (process-wide shared state) and the per-agent ToolsetContext
  toolsets/      the named capabilities themselves, and nothing else: one file per toolset, each file
                 named for the toolset it declares, each exporting a module-level TOOLSET, and each
                 listed in pyproject.toml's kokua.toolsets entry-point table, which is the only index
    audio.py, compute.py, documents.py, fs.py, memory.py, misc.py, skills.py, speech.py, time.py,
                   transcription.py, web.py -- wrappers over AIMU's tool groups and stores
    capabilities.py, config.py, conversations.py, mcp.py, scheduling.py -- one Kokua subsystem's logic
                   each, as agent tools (capabilities wraps the registry itself)
    planning.py    a Workflow instead of tools: the only toolset that contributes no tool at all
    aimu_agents.py, benchmark.py, github_backup.py, image.py -- Kokua's own, needing only the config
```

`tests/` mirrors this layout.

## The core

`Assistant` ([core/assistant.py](https://github.com/saxman/kokua/blob/main/src/kokua/core/assistant.py)) is the composition root and the
serve loop, and little else. It owns:

- **`ConversationBook`** -- the session store, the per-conversation agent cache, and which
  conversation is being viewed. These move together on a switch, which is why they are one object.
- **`TurnRunner`** -- reactive turns (the user sent something) and proactive turns (a scheduled task
  fired). The seven concurrency invariants are documented at the top of that module.
- **`HumanGate`** -- tool approval and a workflow's own decision, each a lock-guarded single-slot request
  the serve loop resolves with the user's next message.
- **`SettingsApplier`** -- reading, applying, and persisting the runtime-mutable settings.
- **`ChannelUI`** -- the only view of the outside world.

Non-obvious control flow: the serve loop runs each reactive turn as a background `aio.RunHandle`, so
the channel keeps reading during a turn. That is what lets a `/stop` cancel an in-flight reply, and
what lets a web approval reply be routed back to the waiting tool call. Switching conversations does
**not** cancel a running turn: each conversation owns its own agent and client, so a backgrounded turn
persists to its own conversation, streams muted, and posts a notification when it finishes. Only
`delete_conversation` cancels, and only the deleted conversation's own turn.

The three conversation commands (`/new`, `/conversations`, `/switch <id>`) are dispatched in that same
loop, beside `/stop` and `/diag`, and go through the same `new_conversation` / `select_conversation`
the web UI's sidebar buttons call. They are core commands rather than terminal ones on purpose: a
conversation is a core concept, a channel has no route to the book that owns one, and the alternative
would have been a second implementation living in `channels/cli.py` with its own idea of what a switch
means. Two consequences follow from putting them here. They sit *above* the pending-answer check, so
typing `/new` while an approval prompt is on screen leaves that question behind
(`HumanGate.abandon_all`) exactly as clicking New does. And because a switch made by the core is one
a front end did not initiate, the core repaints: `ChannelUI.push_conversations` refreshes a sidebar and
`ChannelUI.show_history` replaces what a page is displaying, with `ChannelUI.show_working` behind it
because that repaint clears the page's "a turn is already running here" indicator, all three no-ops on
a channel that prints as it goes. The notice itself is sent between the history frame and the working
one: a page wipes its transcript on the first and treats an ordinary message as the end of a turn, so
either edge of that gap loses something. What the terminal gets instead is that sentence and nothing
else, because muting is the one part of a background turn it cannot do:
`ChannelUI.mutes_background_turns` is false there, so the reply to `/new` says the turn you left keeps
printing here, that `/stop` now reaches the conversation you moved to, and that a tool call needing
approval is denied meanwhile.
The wording of all three replies lives in `core/conversation_commands.py`, for the reason
`core/diagnostics.py` holds the `/diag` report.

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
`session.metadata["subagent"][str(user_index)]`, the same map a rich-tier workflow's
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

### What a turn cost

`core/metrics.py`'s `TurnMetrics` accumulates what a turn's model calls cost: how many, which models,
how many seconds of model time, and (when a provider reports them) input and output tokens. It is
itself an AIMU event sink, one callable reading `ModelTurnFinished` and ignoring every other event, so
an AIMU upgrade that adds a new event member cannot turn this sink into an outage.

The wiring follows `subagent_events`'s own split between a durable seam and a per-turn scope, on purpose:
`core.metrics.record_event` is a module-level forwarder with no turn state of its own, so it is safe to
assign to `agent.model_client.events` (AIMU's own words are "the durable, always-shared setting")
permanently, unconditionally, with nothing to detach afterward. What *is* opened and closed around a
turn is the `current_metrics` ContextVar: `TurnRunner` opens a fresh `TurnMetrics`, sets it on that
ContextVar for the turn's duration alongside `subagent_events`, and resets it in the same `finally`.
Concurrent turns on different conversations therefore never share an accumulator, by the same
ContextVar-per-execution-context mechanism `subagent_events` relies on. The client-side sink itself never
needs that dance: it sits on the client permanently, inert whenever the ContextVar it reads is unset, so
no failure path can strand it there by failing to take it back off.

Because the sink lives on the client rather than being threaded through `agent.run()`, a planned turn's
model calls count for free: `PlanRunner` drives `ctx.agent`, the conversation's own agent, so its calls
land on the same client the reactive path just instrumented. An unattended (scheduled) turn opens its own
`TurnMetrics` and its own `current_metrics` scope in `_unattended_body`, since it runs in a child task with
no counterpart to the reactive path's outer `finally`.

`TurnRunner._record_provenance` turns the accumulator into a stored record at the moment it is called,
not earlier: it takes the sink and the turn's start time, calls `TurnMetrics.record(wall_seconds=...)`
itself, and hands the result to `ConversationBook.record_turn_provenance` as `usage`, stored under
`session.metadata["usage"][str(user_index)]`. Every exit branch of a turn (success, cancellation, a
connection error, a generic error) calls it with the same two arguments, which is what makes the
wall-clock figure honest on the paths a reader most wants to examine: a turn that raised still cost
what it cost up to the point it stopped. `record()` returns `None` when the turn made no model call, and
`record_turn_provenance` then writes nothing, so a turn that never reached the model is not misrecorded
as a free one.

A spawned sub-agent and a workflow critic each build their own client, which puts both outside the
family `agent.model_client.events` reaches: without their own wiring, delegated and reviewed work would
be invisible to `TurnMetrics` and a heavily delegating turn would read as cheap. Both take `record_event`
directly rather than a `LiveState` field or a threaded parameter, for the same reason the top-level
wiring does: `record_event` is a module-level constant with no turn state of its own, so a tool or agent
built once, at composition time, reports into whichever turn is actually running when an event fires,
with nothing to plumb back to the caller. `core.agents._spawn_tool` and `make_delegation_tool` pass it as
`make_async_subagent_tool(events=record_event)` (needs `aimu>=0.25.0`, below the current floor; see
[The model every agent runs on](#the-model-every-agent-runs-on) for what the probe checks today), and
`workflows.critics.reviewer_agent` passes it as both `aio.Agent(events=record_event)` and
`client.events = record_event`, unconditionally rather than through a parameter a caller could forget
to pass; `review` and `stream_review` need no change, since both build their agent through
`reviewer_agent`. The client-level assignment is what covers any direct call a caller makes on the
client rather than through `run()`, since the agent's own `events` override only applies inside a
`run()` call; `finalize_verdict`'s `client.chat(..., schema=Verdict)` is exactly such a call. It does
not, however, close the gap for that specific call: AIMU's structured (`schema=`) path returns before a
turn event is ever emitted, on any client, so no sink, however wired, can observe it. A `/plan` turn's
recorded cost is therefore short by exactly one model call per review round, with nothing able to flag
the gap, since the missing call never enters the count a partial-report qualifier could point at. Every
call that *is* recorded attributes by the event's `agent` field, so a
delegated or reviewed turn's `TurnMetrics.record()` carries a non-empty `by_agent` breakdown where an
undelegated turn's does not (`test_omits_by_agent_when_only_the_entry_agent_ran` pins the undelegated
case).

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
  and a connected MCP server all reach nothing until some agent's `tools` names them, and nothing warns
  about a name nothing references. Startup used to, for names the user had provisioned specifically to
  be reachable, but telling those from the ones that merely ship meant a provenance rule over the whole
  namespace, and a toolset nobody declares costs nothing to leave unnamed. A configured MCP server does
  cost something (a handshake, and a held credential), which is the case that lost a signal here; see
  [Add MCP services](../how-to/add-mcp-services.md).
- **A composed sub-agent is the one exception, and it is entered by declaration.** `compose_subagent`
  ([toolsets/capabilities.py](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/capabilities.py)) resolves a sub-agent's tools from
  names the model picked out of the registry rather than from a table, and runs one task on it through
  AIMU's subagent machinery instead of `wire_agent`. Only an agent whose own `tools` names `capabilities`
  holds that tool; it may not be handed `capabilities` itself, since how far composition nests is
  `[capabilities].max_depth`'s decision (default 3, `0` off) and not the model's; and its tools
  still route through `[security].confirm_tools`. It is built per call and discarded with the call, so
  what widens is one task's reach, never a persistent agent's.
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
`[agents.assistant]` the cross-cutting toolsets (memory, documents, skills, config, `mcp`,
scheduling, conversations, `planning`, `capabilities`, the clock) and no domain toolset, delegating web
work to `researcher` and filesystem and compute work to `coder`. There is deliberately no catch-all role
alongside them: a task neither specialist covers is what `compose_subagent` is for, and a `generalist`
declared next to it would claim the same slot more cheaply and win. That keeps the always-on agent's
tool context small, and the prompt tells it so: `assemble_system_message` adds the "you are a lean
supervisor, you MUST delegate" clause only when every toolset the agent declared is `cross_cutting`. But
that is a property of the config, not a law of the code. Give `[agents.assistant]` a
`tools = [..., "compute"]` and it gets `execute_python`, loses the lean clause, and Kokua neither objects
nor cares. The one structural
restriction is `entry_point_only`: `skills` works solely on the entry agent, because a spawned worker is a
plain AIMU `Agent` rather than a `SkillAgent`, so skill injection would have nothing to hook.

### How an agent's tools resolve

Four steps, all in `toolsets/`. The first two run once at startup, before Kokua opens or connects to
anything; the last two run once per agent, whenever one is built:

1. **Build the namespace.** `agents.build_registry(config)` collects toolsets from six labeled
   providers: AIMU capability (`builtin.py`), core subsystem (`core.py`), MCP server (one per
   `[[mcp.server]]`, named by its required `name`), skill (one per skill on disk, so a skill name sits in
   the same namespace as everything else), built-in toolset, and plugin. The last two are both the
   `kokua.toolsets` entry-point group, split by which distribution registered them: Kokua's own four
   (`aimu_agents`, `benchmark`, `github_backup`, `image`) take the built-in label so
   `--list-toolsets` can group them separately. That label is the only difference between the two, and
   every toolset keeps the `build` its author wrote: nothing is wrapped, so a toolset that fails to import or to build takes startup down
   naming itself, whichever route it arrived by. **Nothing gates any of this either.** Registration is unconditional because installing a
   distribution that registers an entry point is the consent, and because the switch that used to exist
   could not do what it claimed: `resolve_config` imports every entry point before the file is parsed
   (see [Configuration](#configuration)), so withholding the names afterwards ran the
   same code and only turned a working `tools` declaration into an unknown-toolset error.
   `registry.register` then rejects a name two providers claim. A `Toolset` is a frozen dataclass:
   `name`, `description`, `build`, `guidance`, `cross_cutting`, `entry_point_only`, `workflow`,
   `settings`.
2. **Validate.** `agents.validate_agents` runs before the session store is opened or any server
   connected, so an unknown toolset name, a missing entry agent, an unknown delegation target, a cycle,
   or `skills` on a worker fails with nothing written and nothing connected.
3. **Select.** `registry.select(names, registry, agent=, entry_point=)` returns the declared toolsets in
   declared order, deduplicated, raising on an unknown name (with the available ones listed) or an
   `entry_point_only` toolset declared elsewhere. It never drops: dropping is what the previous per-role
   lists did, and a typo silently produced a smaller toolset.
4. **Build.** `registry.build_tools(toolsets, ctx)` calls each `Toolset.build(ctx)` and concatenates,
   deduplicating by `__name__` and keeping the first, so declared order decides a collision. `ctx` is a
   `ToolsetContext`: the one process-wide `LiveState`, this agent's live object (`None` for a spawned
   worker), and its `agent_name`, which unlike the object is known at every construction site and is how
   a toolset scopes itself to its own holder (`benchmark` asks the config what `agent_name` runs on).
   `build` must create only closures, never process state -- every shared singleton (the memory store,
   the document store, the `SkillManager`, the `TaskService`) is a lazy property on `LiveState`, so two
   agents declaring one toolset share one rather than constructing two, and the two stores are opened only
   because some agent declared them. The `SkillManager` and the `TaskService` are the exceptions to that
   last half, and not by accident: every agent is a `SkillAgent` and so takes the manager regardless, and
   `tasks.arm_all()` has to fire a persisted scheduled task whether or not any agent can talk about
   scheduling.

The prompt is assembled from the same selected list, in `agents.assemble_system_message`. For the entry
agent, a `--system` flag wins outright over its declared opener; a worker's declared opener is never
touched by the flag. Absent an override, it is the agent's own `system_message` (falling back to
`[assistant].system_message`, then the built-in default), then each toolset's `guidance` in declared
order, then `DELEGATION_GUIDANCE` if `delegates_to` is non-empty, then `LEAN_DELEGATION_GUIDANCE` if every
selected toolset is `cross_cutting`. Guidance travelling with the capability is the point: installing a toolset brings the
instructions that make the model use it, and removing one takes them away, with no prompt constant to
keep in step by hand.

Two of those sentences exist for one failure, a model answering a question it should have looked up, and
they are split across the two halves because the two halves reach different agents. `DELEGATION_GUIDANCE`
tells a delegating agent that a request counts as specialized whenever its answer could have moved since
training or the user could check it against a source, and to delegate it "even when you think you know";
the `web` toolset's own guidance tells whoever holds the tools to look it up rather than recall it. An
agent that delegates but holds no web tools gets only the first, a worker holding `web` gets only the
second, and the shipped `[agents.assistant]` and `[agents.researcher]` are exactly that pair. Both state
the trigger as a property of the *question* (could it have changed, could the user check it) rather than
as the model's own confidence, which is the signal a model is worst at reporting: `LEAN_DELEGATION_GUIDANCE`
already named the *activity* ("web research"), and naming an activity only helps once the model has
decided the question needs the web. `wire_agent` selects once and passes the same list to both the message and the
tools, so the two cannot resolve different toolsets for the same names.

#### What a skill script sees

A skill's scripts run as subprocesses, and a script cannot discover where Kokua serves downloads from or
which address it is allowed to mail. `LiveState.script_env()` is the one place those facts are turned
into environment variables (`KOKUA_DOWNLOADS_DIR`, `KOKUA_IMAGES_DIR`, and the `[email]` settings);
deriving them inside a script would mean re-implementing `config/paths.py` and drifting from it.
`KOKUA_EMAIL_PASSWORD` is deliberately not among them, since a subprocess already inherits it from this
process and copying it would duplicate a secret for nothing.

That map has to travel by two separate routes, which is the part worth knowing before changing either.
A spawned worker is a plain `Agent`, so its skill tools come from the registry, and `LiveState.skill_tools`
passes the env to `build_skills_server` when it builds that server. The entry agent is a `SkillAgent`,
which builds its *own* skills server (on first run, and again on `reload_skills`), so nothing outside it
can reach that call; `wire_agent` hands the same map to `SkillAgent(script_env=...)` instead. A route that
forgets raises nothing anywhere: the script simply runs with the settings missing and reports itself
unconfigured, which is what `email-report` did on the entry agent until the second route was wired.
`tests/core/test_build.py` pins it, because nothing else would notice.

#### The shipped entry agent's inventory

All 31 tools the shipped `[agents.assistant]` table resolves to, and where each comes from. This is what
`config.example.toml` declares, not a fixed list: a different `tools` line produces a different set.
Twelve of the 31 come from AIMU, more than a third, and so are not greppable in this repository (more
once skills are installed, since AIMU injects a tool per skill script on top of this set), which is why
this table exists rather than a naming convention alone:

| Tools | Built by | Declared as |
|---|---|---|
| `author_skill`, `add_skill_script` | AIMU `make_skill_authoring_tool` / `make_skill_script_tool` | `skills` (entry agent only) |
| `store_memory`, `search_memories`, `list_memories` | AIMU `make_memory_tools` | `memory` |
| `save_document`, `read_document`, `list_documents`, `search_documents` | AIMU `make_document_tools` | `documents` |
| `get_current_date_and_time`, `convert_time` | AIMU `builtin.time` | `time` |
| `add_mcp_server`, `remove_mcp_server` | `toolsets/mcp.py` | `mcp` |
| `read_config`, `update_config` | `toolsets/config.py` | `config` |
| `schedule_task`, `list_scheduled_tasks`, `get_scheduled_task`, `update_scheduled_task`, `cancel_scheduled_task`, `enable_scheduled_task`, `disable_scheduled_task`, `run_scheduled_task`, `stop_scheduled_task` | `toolsets/scheduling.py` | `scheduling` |
| `list_conversations`, `read_conversation`, `search_conversations` | `toolsets/conversations.py` | `conversations` |
| `list_capabilities`, `compose_subagent` | `toolsets/capabilities.py` | `capabilities` |
| `benchmark_model` | `toolsets/benchmark.py` | `benchmark` |
| `spawn_subagent` | AIMU `make_async_subagent_tool` | implied by a non-empty `delegates_to` |

Two conventions keep this honest. Every Kokua-side agent tool lives under `toolsets/` and nowhere else,
so `grep -rl '@tool' src/kokua/` finds only files in that one directory. And
`test_entry_agent_toolset_is_exactly_the_documented_inventory` in
`tests/core/test_build.py` asserts the built agent's tool names as an **exact set** mirroring this table,
so adding a tool to the entry agent fails the suite until the table is updated, and a plugin toolset
leaking onto it fails too. Documentation alone would have rotted; the test is what makes the table
trustworthy.

The pattern for a new Kokua capability is two modules, split by who reads the output. The logic goes in
the owning subsystem (`core/`, `config/`, `mcp/`, `scheduling/`) and returns data or raises a typed
error; it holds only what agents and front ends both need, and formats nothing. The agent tools go in
`toolsets/<name>.py`, one file named for the toolset: a `make_*_tools(...)` factory closing over the live
state, the docstrings that steer the model, every sentence it reads, and a `TOOLSET` whose `build` pulls
that state off the context, plus one line in `pyproject.toml`'s `kokua.toolsets` table, which is the only
registration there is. A capability needing nothing but `AssistantConfig` skips the first module and is
just the toolset. Either way it reaches an agent only when a `[agents.*]` table names it.

The split earns its keep where the two readers diverge. A scheduled task's next firing is a `status` to
`TaskService`, "~3600s" to the model, and "in 1h" in the sidebar; when the service returned one sentence,
the sidebar was showing prose written to steer a model.

### Reading across conversations

`toolsets/conversations.py` defines `list_conversations`, `read_conversation`, and
`search_conversations` over `core/transcripts.py` (flattening, truncation, search) and
`ConversationBook.resolve` (an id or a unique prefix). Two decisions in it are worth knowing before
changing them.

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

### Titling a conversation

A conversation gets its title twice. `ConversationBook.persist` derives the placeholder the moment the
first user message lands (`messages.derive_title`: that message, truncated to 40 characters) and returns
that it did, which is what makes `TurnRunner._persist` push the conversation list. It then asks
`Assistant._spawn_title` for the real one, and that runs as a background task: `core/titles.py` puts the
opening message to a fresh, context-free, tool-less client on the same model the conversation runs on,
and `ConversationBook.retitle` swaps the placeholder for what comes back, followed by a second sidebar
push. A conversation a scheduled task minted is pre-titled with the task's name, so it derives nothing
and is never retitled; on a channel with no conversation list a firing runs in the viewed conversation
instead, and that one is titled like any other first turn.

Three things about that shape are deliberate. It is **in the background** because the reply is what the
user is waiting for and a title is worth neither a round-trip on the end of their first turn nor an error
they have to read: every failure path -- an endpoint that is down, a model string naming nothing, an
answer that sanitizes to nothing -- returns None, and the placeholder stands. It is **guarded on the way
in**: `retitle` writes only if the title it was built to replace is still there, so a conversation
deleted while the model was writing is not resurrected by a store whose `get` answers a missing key with
a fresh empty session, and a rename, if Kokua ever grows one, wins over a title in flight. And it takes **that conversation's
own turn slot** for the write, like `delete` and for the same bounded reason, because the store saves
whole sessions: an unsynchronized read-modify-write would revert whatever the next turn persisted between
the read and the write. Shutdown cancels a title still in flight rather than awaiting it, for the reason
invariant 7 gives about the store closing under a task nobody is watching.

`[assistant] generate_titles` (default `true`) turns the second half off, leaving the placeholder as the
title. It is the one entry in `CORE_RUNTIME_SETTINGS`, and it is there rather than in a toolset because
no capability owns it: `Assistant._spawn_title` is the only reader. It is hot rather than startup-only
because that read happens per conversation, so a change genuinely applies without a restart, which is
the test the model itself fails. There is no setting for *which* model writes the title, because the
conversation already answers that.

### Branching a conversation

`ConversationBook.branch(conversation_id, user_index)` forks a conversation at one of its turns: it
writes a new session holding a copy of the parent's transcript through the end of that turn, titles it
`Branch of <the parent>`, and activates it through the same `_activate` path `create` uses, so a branch
whose agent fails to build reverts the pointer rather than stranding the view. The parent is not
touched, and the two are ordinary independent conversations afterwards.

Three things decide the shape. The cut is **a turn boundary**, found by `turn_end`: the next message the
user actually sent, skipping the `user`-role nudges the agent loop injects between tool-calling
iterations (`messages.is_user_turn`). That is the only cut a transcript survives, because anywhere else
can fall between an assistant message holding `tool_calls` and the `tool` messages answering them, which
a provider rejects on the branch's *next* request rather than at the fork, where it could still be
reported. The per-turn metadata maps (`subagent`, `trace`, `model`, `thinking`, `failure`, `usage`) are
**filtered, not remapped**, because a prefix copy leaves every index where it was: the branch replays its
inherited turns exactly as the parent does, cards and costs included. And `task_id` is **not inherited**,
because a branch of a scheduled run is the user's conversation rather than another run of that task;
inheriting it would nest the branch under the task and enter it into that task's retention pruning.

Branching reads the store, never `agent_for`, for the reason the conversation tools do, and takes no
turn-gate hold: it reads a snapshot of the parent and writes a brand-new key, so there is nothing for
another turn's `persist` to collide with. A turn in flight on the parent is invisible to it, which is
correct, since the cut is always behind that turn. Switching into the branch backgrounds a turn running
in the parent rather than cancelling it, exactly as `select` does.

The web page names a turn by the position of its user message, which `replay_items` already stamps on
replayed items and which the `turn_saved` frame supplies for a turn that just finished live. That frame
is published by `TurnRunner._persist`, after the store write and only when `ConversationBook.branchable`
says the stored transcript really has a user turn there, so the page is never offered a branch point the
store cannot serve.

### Truncating a conversation

`ConversationBook.truncate(conversation_id, user_index)` is branching's mirror image: a branch keeps
`messages[:turn_end(...)]` in a new conversation, and a truncation keeps `messages[:user_index]` in this
one, deleting the named turn and every turn after it. The conversation keeps its id and its place in the
sidebar, so the view does not move.

Four things decide the shape, and three of them are branching's answers read backwards. The cut is **a
turn boundary**, validated through the same `turn_end`, because everything before a message the user
actually sent is complete turns: any other cut can leave the transcript ending between an assistant
message holding `tool_calls` and the `tool` messages answering them, which a provider rejects on the
conversation's next request rather than at the click. The per-turn metadata maps are **filtered, not
remapped**, since a prefix cut leaves every surviving index where it was. The **title is dropped** when
no user turn survives, which makes an emptied conversation indistinguishable from a fresh one instead of
leaving it named after a turn that is gone (the system message survives such a cut, so "emptied" is
about user turns rather than about an empty list). And the conversation's **cached agent is dropped, not
discarded**: `AgentRegistry.drop_agent` forgets the agent and keeps the per-conversation lock, because
the truncation is holding that lock while it writes and replacing it would let a queued turn and a later
one serialize against different objects. The agent rebuilds from the shortened store, which is how the
deleted turns leave the model's context as well as the display.

One consequence is recorded rather than guarded. A branch stores `branched_from`, the
`{conversation_id, message_index}` it was forked at, and truncating the *parent* can delete the index a
child points to. Nothing reads the key today, and the user is allowed to tidy a parent without it
silently breaking a child, so a later change that reads it (sidebar nesting is the obvious one) has to
tolerate a fork point that no longer resolves.

Concurrency splits into two halves that answer different questions. The write is held under the
conversation's own `gate.turn` slot, like `delete` and `retitle`, because the store saves whole sessions
and a read-modify-write racing a turn's `persist` would revert one of them. A conversation with a turn
actually *in flight* is then refused, in `Assistant.truncate_conversation`, and that half is not about
correctness: a turn holds the same slot across its own persist, so the hold already makes an interleaving
impossible. The refusal keeps the wait bounded (the web front end applies controls on the one task
reading its socket) and keeps a deletion from applying minutes later, silently taking a turn that
arrived in between with it.

## Plugins

Two entry-point groups: `kokua.frontends` (a `FrontEnd` with `run(config, args)`) and `kokua.toolsets`
(a `Toolset` with `build(ctx)`). The built-in `cli`/`web` front ends and the three plugin toolsets are
registered in Kokua's own `pyproject.toml` exactly as a third party would register theirs;
`plugins.py` discovers them at runtime, and `kokua.plugins` re-exports `Toolset` and `ToolsetContext`
as the public surface a third party imports. Add a transport or new tools as a plugin, not by editing
the core; see [toolsets/image.py](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/image.py).

A third party's toolset is distinguished from one Kokua ships by exactly one thing: its provider label
in `--list-toolsets`, which comes from which distribution registered the entry point. Nothing branches on
it. Both arrive through the same group, both keep the `build` their author wrote, and either one failing
to import or to build stops startup naming itself.

A toolset is also how a whole *agent* arrives. Every AIMU `Runner` exposes `.run(task) -> str`, so
mounting one needs no core surface at all: `build()` returns a callable that runs it.
[toolsets/aimu_agents.py](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/aimu_agents.py) does this for AIMU's three
prebuilt orchestrators and is the reference for wiring your own. It builds its agent inside the tool
call rather than in `build()`, because `build()` runs once per agent and constructing a
sync `ModelClient` is what loads weights on an in-process provider -- and because a cached
orchestrator's `messages` would be shared across concurrent calls.

## Configuration

Precedence is **CLI flag > TOML config file > built-in default**. `config/schema.py` holds
`AssistantConfig` (a plain dataclass, with leaf paths derived from `data_dir`). `cli.resolve_config`
builds the settings table first (`config.settings_sources.build_settings_table()`, over the installed
toolsets), parses the TOML file against that table (`config/file.py`'s `load`, which needs it to know
which sections are runtime-settable and which of a toolset's remaining keys are merely cold), merges the
result under the CLI flags into the constructed `AssistantConfig`, and only then seeds every declared
setting's default onto it (`settings_sources.seed_toolset_defaults`) for whatever the file left unset.
Building the table imports every installed toolset, on every run, because a config file naming an
installed toolset's section has to stay parseable. That is also why nothing gates registration: a switch
withholding those names from the registry afterwards would have executed exactly the same code, which is
what retired the old `load_plugins` key. Flag defaults are the `None` sentinel, so an unspecified flag
defers to the file.

The file itself is **required**: `config/file.py::load` raises rather than returning no overrides when
it is missing. Agents live only in `[agents.*]` and the assistant cannot function without at least one,
so there is no useful unconfigured state to degrade to. `Assistant.create` enforces the companion rule
and refuses a config that defines zero agents, or whose `[assistant].agent` names none of them.
Individual keys keep their built-in defaults.

Keys the old per-role vocabulary used are not silently ignored. `[tools]`, `[subagents]`,
`[assistant].memory`, and a per-agent `groups` / `tool_packs` / `mcp_servers` each raise a targeted
`ConfigError` naming the replacement, checked ahead of the schema so an old file gets that message
rather than a generic unknown-key one.

### Which model an agent runs on, and how hard it thinks

`[assistant].model` is the default every agent runs on; an agent naming its own `[agents.<name>].model`
runs on that instead. `AssistantConfig.model_for(name)` is the single resolution, and it is per agent and
never inherited down the delegation graph: a delegator that pins a model does not drag its workers onto
it, so a worker declaring nothing runs on the same default every other undeclared agent does. That is
also why `make_delegation_tool` builds the delegate with the default rather than the delegator's own
model, and `build_agent_specs` sets a spec's `model` key only for a worker that declared one -- AIMU
reads a missing key as "the model the spawn tool was built with". `validate_agents` resolves every
declared model string at startup (offline, no client and no key), so a typo names its table instead of
surfacing later as a failed spawn.

#### The model every agent runs on

`[assistant].model` is optional, and leaving it out is a supported way to run: AIMU resolves a default
from `AIMU_LANGUAGE_MODEL`, or from a probe of the local servers when that is unset, which is what lets
one `config.toml` be shared across machines that serve different models. `AssistantConfig.default_model`
is where that resolution happens, once per process, and `model_for` falls back to it, so **`model_for` is
total: it answers with a string whatever the file says.**

That totality is the point, not a convenience. The alternative is to hand `None` to AIMU at client
construction and ask the built client afterwards what it became, and a client cannot answer: `client.model`
is a resolved `Model` enum, which names a catalogued id and carries nothing else. A default may be an
extended string -- `ollama:qwen3.8:27b@http://gpu-box:11434` (see
[`model`](../reference/configuration.md#model)) -- and the `@base_url` is gone by the time anything reads
it back off a client. Kokua did read it back, in five places, and the result was that every spawned
sub-agent, every composed worker, and both planning reviewers were rebuilt against the *provider default*
while the entry agent talked to the override. On a machine with a local server running it produced
answers, from the wrong model, with nothing reported anywhere; with no local server it surfaced as
`All connection attempts failed` from a tool call whose parent turn was working fine.

So the rule is: **anything needing the default asks the config, never a live client.** `default_model`
resolves with `include_hf_cache=False`, matching what `aio.client` asks for, because the aio surface
cannot construct an in-process `hf:` model from a string. The answer is cached on the config, since the
no-env-var path probes over HTTP and `compose_subagent` asks on every call. `build.model_label` renders
that same string for `/diag` and the stored record, which is why a session with nothing declared now
reports `ollama:qwen3.8:27b@http://gpu-box:11434` rather than `OllamaModel.QWEN_3_8_27B`.

Both are read once, at startup. No live client is rebound to another model, which is why the model is
not a `RuntimeSetting`: a hot setting is one the change reaches in the same session, and this one cannot.
It is an ordinary cold key, so `update_config` writes it and reports that a restart is needed.

`/diag` reports the entry agent's model plus every override, since a running session otherwise has no
surface that names one. `build.model_label` is the single renderer, and it renders `model_for`'s string,
which is also what the stored record uses.

A stored conversation records what produced it, in `session.metadata` rather than on any message dict:
`metadata.model.<user_index>` is the model that answered that turn, and each sub-agent card carries its
own worker's model (`SubagentReporter` resolves it, since AIMU's observer callbacks do not carry one).
Metadata, deliberately -- AIMU strips only its own inert keys before a request, and Ollama and
OpenAI-compatible providers forward any other message-dict key verbatim.

**Reasoning effort follows the same shape, with three differences.** `[assistant].thinking` is the
default and `[agents.<name>].thinking` the override, resolved by `AssistantConfig.thinking_for(name)`,
read once at startup, reported by `/diag`, and recorded per turn under `metadata.thinking.<user_index>`
and on each spawn card. The values are AIMU's own four: absent emits nothing, `false` asks the model not
to reason, `true` reasons at the model's default effort, and `"low"` / `"medium"` / `"high"` request a
level. What differs:

1. **Resolution tests `is None`, not truthiness.** `thinking = false` is a declaration ("do not
   reason"), so an `or` the way `model_for` uses one would let a `"high"` default swallow it. Every
   guard on the value follows suit -- in `thinking_for`, `build_agent_specs`, `record_turn_provenance`,
   `SubagentReporter.spawned`, and `_thinking_line`.
2. **`build_agent_specs` writes the *resolved* value into every spec, not just a declared one.** This is
   the reverse of `model`: AIMU reads a missing spec `model` as the spawn tool's own model, which is the
   default, but a missing spec `thinking` as `None`, because a spawn tool has no thinking tier. Left to
   AIMU, an undeclared worker would skip the default rather than inherit it.
3. **It reaches the reviewers.** `critics.reviewer_agent` takes a `thinking`, and the four planning
   wrappers thread `config.thinking` to it. A reviewer is not an `[agents.*]` agent, so the `[assistant]`
   tier is the only one it has -- exactly as it is for the model it already ran on.
4. **A turn can ask for its own.** The web composer's Think picker and the CLI's `/think` put a level on
   `ChannelMessage.metadata["thinking"]`; `config.file.thinking_request` normalizes it (the softer sibling
   of the `_thinking` validator: unrecognized degrades to the configured effort rather than raising), and
   `TurnRunner.reactive` passes it to `agent.run(..., thinking=...)` as a per-run override. A per-run
   argument rather than a write to `agent.thinking`, because the planning workflow drives the same agent
   object, so the field would leak the request into a planned turn's planner and executor phases. The same
   resolved value goes to `_record_provenance`, which is why that method takes the effort as a required
   argument now instead of reading the config: with a per-turn request the two can disagree, and the
   record has to say what the turn did, not what it would have done. A workflow turn resolves to the
   config regardless, so a request that reaches one is neither applied nor recorded.

One consequence worth knowing: `false` also selects a model card's instruct-mode sampling profile where
the card declares one (`select_profile` in AIMU). Only the Qwen 3.5/3.6/3.8 cards do today; every other
model has a single profile and is unaffected. Any key `[assistant.generation]` or an agent's own
`[agents.<name>.generation]` sets still applies over that profile -- it is a tier above whichever profile
`select_profile` picked, not a replacement for the mechanism -- so it is only a parameter nobody set that
the profile switch decides outright.

The per-agent half needs `aimu>=0.17.0`, which added the `"thinking"` key to the `agent_types` spec.
An AIMU that predates it ignores an unknown spec key in silence, so a per-worker effort would simply not
apply with nothing raised -- and a dict key is invisible to both a name lookup and a signature check. The
same release closes a spec's keys to a published set (`SUBAGENT_SPEC_KEYS`), which is what the startup
probe moved to at the time: a symbol, and the set the depended-on key belongs to. The probe has moved on
twice since (see below); the floor is what still covers this release. See `kokua.aimu_compat`.

### Generation parameters

`[assistant.generation]` sets `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`,
`repetition_penalty`, `max_tokens`, and `context_length` for every agent; an agent's own
`[agents.<name>.generation]` overrides it **per key**, not as a whole-table replacement, so an agent
naming only `temperature` still inherits the default's `context_length`. `AssistantConfig.generation_for(name)`
is the single resolution -- `{**self.generation, **(agent.generation if agent else {})}` -- and, like
`model_for` and `thinking_for`, it is per agent and never inherited down the delegation graph. It returns
an empty dict when nothing is declared anywhere, which is the normal case: a key a config never mentions
stays absent from the request, so a model card's own tuned sampling profile survives untouched.

Three places apply the result, all of them startup-only reads:

1. **Every client the factory builds.** `core.build.build_model_client` serves the entry agent and each
   per-conversation client alike, and right after construction it sets `client.default_generate_kwargs =
   config.generation_for(agent_name)` -- but only `if generation:`, so a config that declares nothing
   leaves the attribute as AIMU built it and the model card's own profile is what `select_profile`
   returns. Writing an empty dict would be writing this tier, and this tier sits above the card. An
   injected client (tests, `make_agent_builder`) is left alone: these parameters live on the client, so
   one built elsewhere already carries whatever its own factory chose.
2. **Each spawned worker's spec.** `build_agent_specs` writes the *resolved* value into
   `specs[name]["generate_kwargs"]`, not just a declared one, for the same reason `thinking` does: AIMU
   reads a spec without the key as "no generation parameters", so an undeclared worker would skip the
   default rather than inherit it. Omitted entirely when nothing resolves, since an empty dict is itself
   a written tier that would still sit above the card's profile.
3. **The two planning reviewers.** `workflows/planning/runner.py` threads `config.generation` (not
   `generation_for`, since a reviewer is no agent's own table) to both the plan-reviewer and the
   result-reviewer, exactly as it already threads `config.thinking`. `[assistant.generation]` is the only
   tier a reviewer can have.

The per-agent half needs `aimu>=0.18.0`, which added the `generate_kwargs` key to the `agent_types` spec.
That key was the probe's surface for a release: 0.17.0 published `SUBAGENT_SPEC_KEYS` itself, so the set's
existence no longer proved this capability, and the probe checked `generate_kwargs`'s membership in it
instead -- a membership check, the third shape it has taken after a name lookup and a signature check.
AIMU 0.20.0 then carried two capabilities Kokua depends on. The first is a sub-agent honouring a
`provider:model@base_url` string (see [`model`](../reference/configuration.md#model)), a behavioural fix
inside a private function with no symbol, parameter, or set member to grip; while it was the newest
surface the probe gripped `endpoint_kwargs`, the mapping the fix routes through, and said in its own
docstring what that left uncovered. The second arrived later in the same release with a handle of its
own: `SkillAgent(script_env=...)`, the parameter that carries the `[email]` settings and the downloads
folder into a skill script the *entry* agent runs (see
[What a skill script sees](#what-a-skill-script-sees) above). That was a signature check, the shape this
probe has taken twice.

AIMU 0.21.0 raised the floor for the `resolve_default_text_model` export that
[`default_model`](#the-model-every-agent-runs-on) calls, and while that was the newest surface the probe
gripped that name. It is the cleanest case this preflight has had: the capability *is* the exported
symbol, so a plain name lookup asks exactly the question that matters, where the shapes before it each
had to settle for the nearest available handle.

AIMU 0.23.0 renamed the channel flags `show_thinking` / `show_tools` to `stream_thinking` /
`stream_tools` and flipped both defaults to `True`. Kokua deleted its own display settings in the same
change and constructs both channels bare, so that default is what puts reasoning and tool calls in
front of a user at all. The probe moved to a signature check on `aio.WebChannel.__init__` for
`stream_thinking`: the parameter is not itself the capability (the default value is), but the rename and
the flip shipped together, so the new name dates a checkout past both. Against an older AIMU the bare
construction still works and streams neither phase, and since Kokua no longer reads `self.show_thinking`
anywhere there is not even an `AttributeError` to notice, which is the failure mode this preflight
exists for.

The floor was `aimu>=0.27.0` until 0.28.0, and it was the first floor whose *reason* and whose *probe*
were different capabilities from different releases. That split is worth following, because it is the
shape of every future case where a bug fix rather than a feature moves the floor.

The floor moved for **0.26.0**: AIMU's tool loop no longer strands an un-dispatched tool call before its
forced wrap-up. Exhausting `max_iterations` on a turn that had requested tools used to leave those calls
unanswered and then append the wrap-up's *user* prompt directly on top of them, which Anthropic rejects
with ``messages.N: `tool_use` ids were found without `tool_result` blocks immediately after``. Kokua felt
it as sub-agents failing rather than answering, and search-heavy ones most of all, since a run that
spends every round calling tools is the one still holding a pending call when the cap lands. There is no
handle on that fix a probe could honestly grip: `_settle_pending_tools` is a private method on a private
class, exactly the kind of internal a later refactor would rename, and a probe pointed at it would turn
this preflight into a wall in front of a *newer, working* AIMU. That is the trap the 0.20.0 paragraph
above describes, so the floor carries this one alone.

The probe therefore gripped **0.27.0**'s `ModelRefusalError` instead, a plain name lookup on `aimu.aio`
and only the second time this probe had had that shape (0.21.0's `resolve_default_text_model` was the
first). The capability *is* the exported name. Anthropic returns a refusal as HTTP 200 with
`stop_reason: "refusal"` and no content, so an AIMU that does not raise for it returns an empty string,
which an agent loop cannot tell from a degenerate turn: the continuation nudge fires and the run spends
its iterations being refused again. `core/turns.py` branches on the class at three sites so a declined
request reads as declined rather than as a generic failure, and an AIMU without the name fails at import
instead of degrading quietly. 0.27.0's other half is the floor's job for the same reason as 0.26.0's:
every provider now reports how a turn ended, so `TruncatedTurnError` fires outside Ollama for the first
time and `client.last_stop_reason` carries the provider's own word for it, but that is an attribute on a
live client rather than a module symbol and Kokua reads it nowhere directly.

`make_async_subagent_tool`'s `events` parameter was the surface while 0.25.0 was the floor, and it is the
floor's responsibility now, as every capability older than the current probe becomes. What that probe
could not see, and what the floor now covers: whether a spawned worker's own spawn tool forwards `events`
on to a grandchild it delegates to in turn, so a recursive delegation could go uncounted one level down.
The probe covers one surface at a time; the version floor covers every earlier release's. (That
capability first shipped tagged 0.24.0, but the number collided with a different, unrelated 0.24.0 that
AIMU's own `main` released first; the branch carrying `events` rebased past it and renumbered to 0.25.0,
so a real, released 0.24.0 correctly failed that probe rather than exposing a gap in it.)

The floor is now `aimu>=0.28.0`, and unlike 0.27.0 it is a floor whose *reason* and whose *probe* are the
same capability again. It moved for AIMU's `CONTINUING` chunk, the phase a streamed driver yields for a
round the loop injected itself (a continuation nudge, or the forced wrap-up at the round cap) rather than
one the model asked for. No other seam could carry it: Kokua constructs nothing differently against an
older AIMU, so `channels/web.py` and `core/subagents.py` simply never see the phase, and nothing raises,
which is the silent-degradation shape this preflight exists to catch.

The probe is a membership check on `StreamingContentType.CONTINUING`, the third shape it has taken and
the second time membership has answered (0.18.0's `SUBAGENT_SPEC_KEYS` was the first). `StreamingContentType`
itself predates this floor by a long way, so its mere presence proves nothing; only whether it carries
this member dates a checkout, the same argument `SUBAGENT_SPEC_KEYS` made one container kind over. It
reads `__members__` rather than testing membership directly, because `in` on an enum compares *values* on
Python 3.12 and raises `TypeError` for a plain string on 3.11, which Kokua still supports, and the
capability here is a member's *name*, not its value.

What the probe leaves to the floor: whether both streamed drivers emit the chunk, and whether both
injection kinds (a continuation nudge and a forced wrap-up) do. A checkout carrying the member but wired
to only one driver, or emitting it for only one injection kind, still passes, the same shape of gap
0.25.0's `events` parameter left one level down, where only the first hop being wired was everything that
probe could ask.

0.28.0 brought a second capability Kokua depends on, and it is worth reading for what a probe *cannot*
claim. The `"max_iterations"` entry AIMU added to `SUBAGENT_SPEC_KEYS` is the spec key `core/agents.py`
writes for an agent declaring its own tool-loop cap; without it a per-agent cap has nowhere to go, since
AIMU's own cap was one value shared by a whole spawn tool. That set is *closed* and validated at
factory-call time, which for Kokua is `wire_agent` building a conversation's agent, so an AIMU predating
0.28.0 raises `ValueError` naming the key at startup already: no silence to convert into noise, and no
mid-session failure to pull forward. A probe there would buy the wording rather than the timing, which is
why the one slot goes to the phase above, which has no such escape hatch, and this key is left to the
floor. The global tier needed none of it: the factory argument behind `[assistant].max_iterations` has
existed since 0.12.0.

Two application facts worth knowing beyond the parameters themselves. `max_tokens` and `context_length`
are different knobs that share one window: `max_tokens` caps *generated* tokens, `context_length` sizes
the whole window the prompt and the output share, so `context_length = 32768` with `max_tokens = 4096`
leaves roughly 28k for the system prompt, the tool block, and history. And a parameter a backend cannot
take is dropped by AIMU with a warning naming the remedy -- Ollama's SDK has no `min_p`, the Anthropic API
has no penalties, and only Ollama's native API sizes the context window per request, so `context_length`
is a no-op with a warning everywhere else. That warning reaches one place: the rotating file log
(`logs_path/kokua.log`, i.e. `data/logs/kokua.log` under `$KOKUA_HOME`), since `logging_setup` attaches a
file handler and nothing else. It is not surfaced in the chat, in the terminal, or in `/diag`, which
reports what the config *declares*, not what survived to the wire. A user whose parameter never applies
finds out by reading that log.

`[assistant.generation]` is also the first sub-table `config/file.py`'s `_sections` handles: `tomllib`
nests a dotted TOML header like `[assistant.generation]` inside `assistant`, so a flat key loop over
`[assistant]` would read it as one key holding a table rather than as its own section. `_sections`
re-enters it as `assistant.generation`, which is what lets one schema entry per parameter serve it and
what makes an unknown key inside it report `[assistant.generation].<key>` rather than a generic
`[assistant]` type complaint. Read only at startup, like the model and the reasoning effort: `update_config` can write the default
tier (a cold key, applying on the next start) but not an agent's own `[agents.<name>.generation]`, which
is locked the same way the rest of `[agents.*]` is by default.

`config.toml` is the single source of settings **and the app writes it**. `config/store.py` does
comment-preserving writes via `tomlkit` (stdlib `tomllib` cannot write). Two writers: the
`add_mcp_server`/`remove_mcp_server` tools, and the assistant's own `update_config`. `update_config`
refuses whatever `[security].locked_config_keys` matches, via `config/store.py`'s `locked_by`, with
`("security", "locked_config_keys")` itself locked by axiom regardless of what the list says. See
[the configuration reference](../reference/configuration.md#who-may-change-which-key) for the shipped
patterns and what removing each one permits. `update_config` applies hot-appliable keys live.

Which settings are hot is not a list maintained by hand in several places: it is
`config/table.py`'s `SettingsTable`, built once at startup from `CORE_RUNTIME_SETTINGS` plus every
toolset's own hot `Setting`s, and every consumer -- the schema, the incoming-payload sanitizer, the
live-apply loop, and the persist path -- loops over that one instance. `CORE_RUNTIME_SETTINGS` holds one
entry, `[assistant].generate_titles`; every other hot setting a run holds arrives from a toolset, so the
table's contents depend on which toolsets are installed. The
sanitizer predates the removal of the web settings window and outlived it: `update_config` is now its
only caller, so "panel" in the surrounding code names a surface that no longer exists.

## State

Everything lives under `~/.kokua` (override with `KOKUA_HOME`). `config.toml` sits at the root;
`data/` holds only content: `sessions.json`, `skills/`, `memory/`, `documents/`, `downloads/`,
`images/`. Scheduled tasks are the one declared, user-editable exception to that split: they live in
`config.toml` as `[scheduling.task.<name>]` tables, not under `data/`.

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
name reaches no agent until a human adds it to an `[agents.*]` table, since that section is locked by
default: the tool can connect a server but cannot grant itself the capability. `mcp/auth.py` handles OAuth
by posting the authorization link into the chat and persisting tokens to disk. It also carries the one
piece of that flow a single-machine library gets to assume away: `OAuthSettings` holds where the
provider's redirect lands, because FastMCP's default (loopback, a random port per process) sends the
approved browser to *its own* machine, which is the wrong one whenever Kokua is not where you browse.
The two settings behind it are `[mcp].oauth_callback_host` / `oauth_callback_port`.

## Workflows

A workflow is a named turn strategy carried by a `Toolset` (its `workflow` field): declaring that
toolset in an agent's `tools` is what gives the agent the workflow's `/`-command, resolved by
`toolsets.agents.build_command_map` from the entry agent's own declared toolsets -- the same list
that resolves its tools, so a turn strategy is granted exactly the way a tool is. A workflow's `build`
returns an `aimu.aio.AsyncRunner`, AIMU's abstract base for every agent and workflow, so AIMU's own
`aimu.aio.workflows` (`Chain`, `Parallel`, `Router`, `EvaluatorOptimizer`, `PlanExecuteEvaluator`) work
here with no adapter. `workflows.is_rich` splits a runner into two tiers by probing for a `run_turn`
method, the way `ChannelUI` probes an optional channel frame rather than doing an `isinstance` check:
a **base-tier** runner is driven by `TurnRunner` itself, which streams `run()` into the reply and owns
catch-up, at the cost that the runner never appends to the agent's own transcript, so a base-tier turn
has no message to anchor to and does not survive a reload. A **rich-tier** runner additionally
implements `run_turn()` and is handed a `WorkflowContext` carrying the channel, a `decide()` slot for
a human decision (the asker supplies its own reply parser, so no one workflow's vocabulary lives in
the core), and control of the agent's transcript. A workflow that also wants to reach the model as a
callable tool needs `AsyncRunner.as_tool()`, a concrete method the base class provides rather than a
name Kokua looks up, so it is available only to a runner that actually subclasses `aio.AsyncRunner`,
not one that merely matches its shape by duck-typing `run` and `messages`.

`toolsets/planning.py` is the first workflow, and the only toolset that carries one instead of
tools: declaring `"planning"` in `[agents.<name>].tools` is what gives that agent the `/plan` command
(the web UI's Plan toggle sends the same command, and an agent whose `tools` omits `"planning"` has
neither). Its runner, `PlanningWorkflow` (`workflows/planning/runner.py`), is rich tier -- it shows
phases and reviewer cards, pauses for a human approve/edit/reject, and rewrites the transcript so a
planned turn is saved as a plain user/assistant pair rather than as planner scaffolding -- and
subclasses `aio.AsyncRunner` for the `as_tool()` reason above, even though nothing currently offers it
to the model as a tool. `/plan` drafts a plan, optionally has an independent reviewer critique it and
a human approve it, executes, and optionally reviews the result. There is one pipeline; how much of
its work is shown is a `Presentation` value with two instances, `SUMMARY` and `VERBOSE` (the latter
selected by the `planning` toolset's own `show_reasoning` setting, read through `ctx.settings`, on a
channel that can render phase headers). The reviewer itself is
generic and workflow-independent: `workflows/critics.py` runs a fresh, context-free agent over a
curated verification toolset and extracts a typed `Verdict`; `workflows/planning/critics.py` supplies
only the two prompts (`review_plan`, `review_result`) that make it deep planning's own standard rather
than a shared one.

## Web front end

`frontends/web.py` is a Starlette + uvicorn WebSocket server (behind the `web` extra);
`channels/web.py`'s `WebChannel` subclasses
[AIMU's base `WebChannel`](https://saxman.info/aimu/how-to/build-personal-assistant/). The streaming transport
(`token`/`thinking`/`tool`/`done` frames and `send()`) lives in AIMU's base; Kokua's subclass adds the
`conversations`, `history`, `approval`, and `turn_saved` frames its richer page needs. The UI is a single
self-contained `web_static/index.html` served as package data, plus vendored `marked` + `DOMPurify`
(GitHub-flavored markdown, sanitized, rendered client-side as the answer streams) and vendored KaTeX,
typeset after sanitization with `trust:false` once the turn completes. The server allowlists these assets: JS/CSS by name, the
woff2 fonts under `/fonts/`.

The page sends an `input` frame (`{"type": "input", "text", "images"?, "thinking"?}`) for any message
carrying more than its own text, and the bare string for everything else, including `/stop` and approval
replies. `frontends/web.py`'s `_parse_input` decodes it and `WebChannel.feed_input` queues it; both shapes
converge on one `ChannelMessage` in `receive`, so nothing downstream knows which path a message took.

### Reading the socket, and applying what arrives

`pump` is two tasks over one queue: one reads the WebSocket, one applies frames in arrival order. They are
split because they fail differently. Applying a frame can take arbitrarily long, since a control that
waits on the turn gate waits for in-flight turns to drain, which on a local model is minutes. Reading has
to keep happening regardless, because a disconnect *is* a read and this is the only task positioned to
notice one.

Applied inline, as they were, a slow control stopped the reading with it, and the result was worse than
slow. The disconnect queued behind the control was never seen, so the one-connection guard in
`ws_endpoint` was never released, the reload that would have recovered the page was refused as "busy in
another tab", and killing the process was the only way out of a UI that had merely been asked to wait.
The trigger was a conversation delete during a long turn: `ConversationBook.delete` took the gate's
*exclusive* hold, which drains every in-flight turn, so deleting one conversation waited out an unrelated
conversation's turn. It now takes that conversation's own `gate.turn` instead, which `cancel_turn` has
already asked to stop, so the wait is bounded by that cancellation landing.

One queue and one applying task, rather than a task per frame, because order is part of the contract:
selecting a conversation and then sending a message has to bind the message to the conversation just
selected. A slow control therefore still delays the frames behind it, `/stop` included. That is the cost
of ordering, and it is now a delay rather than a wedge. Whichever half finishes first ends the connection,
in both directions: a disconnect ends the reader, and an unexpected error ends the applier and has to end
the reader with it, since a reader left running would queue frames into a drain nobody reads, which is the
same wedge with the halves swapped.

`new`, `select`, `delete`, and `branch` (which carries `id` and `message_index`) all end the same way:
each resyncs the view rather than describing what changed, so the applying task never has to reason
about a diff. `delete` takes that same path even when the conversation it removes is not the one on
screen, where the resync only refreshes the sidebar; the four share one mechanism regardless of which of
them actually changes what the user sees.
Among the frames the server pushes, `turn_saved` (`{"conversation_id", "message_index"}`) takes no such
resync, because it names one turn that already rendered rather than replacing anything: it exists only to
say where that turn now lives in the store, so a bubble already on screen can grow a branch control.

### Reconnecting after a dropped socket

The page opens its socket through a `connect()` function that reassigns the `ws` binding on every
attempt, so restarting Kokua under an open browser does not leave a page whose only recovery is a reload.
`ws.onclose` retries with backoff, 500ms doubling to a 10s ceiling and then retrying indefinitely, and
shows one notice for the whole outage rather than one per attempt.

Almost none of the work is on the client, because the server was already written for this: it resyncs its
entire view on every connection (`_sync_view` sends the conversation list and the active conversation's
history, followed by the settings and the tasks), and the page's `history` handler clears the transcript
before replaying it. So a reconnected page repaints rather than appending a second copy of what it already
showed, and the retry notice goes out with the rest of the old log.

What a reconnect does *not* restore is a turn that was in flight when the socket dropped. `build_app`
constructs an `Assistant` per connection and `serve_connection` ties its serve loop to that connection's
lifetime, so the page comes back to what the turn persisted, not to the stream it was watching.

Two of the ways a socket can close are not worth retrying, and the page tells them apart from a restart by
whether anything arrived before the close. A server that is not up yet closes having sent nothing, which
is the ordinary restart case and is exactly what the backoff is for. A server that answered and *refused*
sends one message frame and closes without ever syncing: the one-connection guard's "busy in another tab",
or a config error reported in place of a session. Retrying a refusal would thrash a server that is up and
working, and every attempt would append the refusal again, burying the sentence that explains it under
repetitions of itself. So a refused page says to reload, and stops.

### Streaming the answer

`token` frames accumulate into `streamingText`, which is the source of truth; the bubble's DOM is a
view derived from it. `renderStreaming` reparses that whole buffer and replaces the bubble's
`innerHTML`, on a 50ms timer coalesced by `scheduleStreamRender`, so a reader watches structure appear
(headings, lists, tables, code) instead of waiting for `done`.

Reparsing the whole buffer looks wasteful next to committing each finished block as it arrives, and the
waste is the point. **A newline is not a block boundary in CommonMark**, so there is no cheap
"everything before here is settled" mark to commit at:

- With `breaks:false`, a lone newline continues the paragraph. Committing there splits one `<p>` in two.
- A newline inside a fenced code block is inside an open block, so the commit would carry an unclosed
  fence.
- A newline after a list item leaves the list open, giving consecutive `<ul>`s in place of one; the same
  holds for a table that has only its header row so far.
- A setext heading (`Title` over `===`) is the sharpest case: the *next* line decides how the previous
  one parses, so no prefix ending at that newline is settled at all.

A blank line is a real boundary, but not inside a fence, and a loose list continues across one. Every
one of those failures is permanent, because committed HTML is what the reader keeps. Reparsing has no
such state to get wrong: it converges on the finalized render by construction, and `finalizeStreaming`
runs the same `renderMarkdown` over the same buffer, so the last live tick and the final render agree.
An unterminated fence needs no special handling for the same reason the spec gives it none, since a
fence with no closer runs to the end of the input.

Two things are deliberately *not* reparsed. `typesetMath` is deferred to `finalizeStreaming`, because
KaTeX is the expensive half of the render and a half-typed `$x^` is noise rather than math, so
mid-stream an expression stands as the source the model wrote. And `finalizeStreaming` cancels the
pending timer before it stamps the bubble: a tick landing after the stamp would reset `innerHTML` and
drop the caption. The `history` replay path cancels it too, since it drops the bubble reference the
tick would have written into.

### Saying a turn is under way

A sent message used to leave the transcript unchanged until the first frame of the reply landed.
`.working-inline` fills that gap: a dim row at the foot of the log carrying a spinner and the turn's
elapsed seconds, indented into the same column as the thinking and tool rows it sits beneath.

**The row is pinned for the turn's whole life, and that was measured rather than assumed.** The first
version cleared it on the first thing that rendered, on the reasoning that content should replace it.
Instrumenting the page (a `MutationObserver` on `#log` against a tap on every inbound frame) showed the
row living for 23 milliseconds on a local endpoint, whose first token arrives about that fast. The
question the row answers is "is a turn running", and that stays true until the turn ends, so that is its
life. Its elapsed count is the turn's age, not the age of the current lull, which is what makes it
readable as "this is slow" rather than "this just started".

Three details are load-bearing:

- **Every renderer goes through `appendToLog`,** which inserts a top-level block *before* the row
  rather than appending after it. That is the whole of the pinning: the row stays last without anything
  ever moving it. The alternative, re-appending the row after each render, does the same work with a
  reflow and a flicker per block.
- **`lastContentElement()` looks past the row.** A `token` frame decides whether to close the open
  bubble by asking whether anything has landed since the last one, and it reads the log's last element
  to find out. With the row pinned, that element is *always* the indicator, so the unguarded check
  would close the bubble on every token and render the answer one bubble per token.
- **The row is created and destroyed, not shown and hidden.** A permanently present element would be
  the log's last child even when idle, so both points above would need a second condition; and the
  ticker interval that drives the spinner has to stop when the turn does, which destruction handles for
  free.

`ChannelUI.show_working` carries a duration and not a flag: `None` means nothing is running, and a
number is how long the turn has already been going, from `Assistant.turn_elapsed`. That is what lets a
switch into a background turn count on from its real age instead of restarting at zero, and it makes
"idle, and it has been running twelve seconds" unsayable rather than merely wrong. The `working` frame
on the wire keeps a boolean, because that is what the page branches on, with `elapsed` riding along
only when there is a turn to time.

### The tasks section, and the settings frames behind it

Two page surfaces are pure front-end concerns and deliberately absent from `RichChannel`: the
scheduled-tasks section (`tasks` / `get_tasks` / `task`) and the settings frames
(`settings` / `get_settings`). The core never sends either, so there is no capability for `ChannelUI` to
probe and no fallback to document; `Assistant` exposes only accessors (`list_tasks` / `task_action`,
`current_settings` / `apply_settings`) and `frontends/web.py`'s pump handles the control frames. A new
transport that wants a task list implements its own; it is not a hole in the channel contract.

The shipped page uses only the first pair. There is no settings window: the header's theme button is a
per-browser `localStorage` preference that never reaches the server, and the runtime-mutable settings
are changed through `update_config`, which hot-applies them by the same `SettingsApplier` path. The
`settings` / `get_settings` control frames remain part of the web transport's contract for a front end
that wants them, and `tests/frontends/test_web.py` keeps covering the server half.

### Exporting a conversation from the sidebar

The sidebar's export button is a third control (`export`), for the same reason the tasks and settings
controls exist outside `RichChannel`: exporting reads a conversation, it is not part of the assistant
loop, and there is no core capability behind it for `ChannelUI` to degrade. It could not be a plain
`GET /export/{id}` route, because `build_app` constructs a fresh `Assistant` per WebSocket connection
(see above), so an HTTP handler outside that connection would have no live session store to read. The
control instead resolves the conversation through `Assistant.resolve_conversation` (a thin public
accessor over `ConversationBook.resolve`, added alongside `list_conversations` and the rest rather than
reaching into `_book` from `frontends/web.py`), renders it with `transcript_export.render_markdown`, and
writes the result under `config.downloads_path`. The reply reuses the **existing**
`/download/{name}` route (already serving generated PDFs and other artifacts) rather than adding a
second one: `WebChannel.send_download` sends a `{"type": "download", "name", "url"}` frame, and the page
turns it into a save by clicking a synthetic anchor carrying the `download` attribute, never by
navigating with `location.href`. Today the two would behave the same: the route answers with
`FileResponse(path, filename=name)`, which Starlette gives a `Content-Disposition: attachment`
header, and Chromium treats that as a download rather than a navigation either way, so the live
WebSocket and any running turn survive `location.href` too as things stand. The anchor's `download`
attribute is what keeps that true independent of the server: if the route ever answered without that
header, `location.href` would load the response in place and drop both, where the anchor still would
not. The exported filename is built from today's date, a title slug kept to
`[a-z0-9]` runs, and a short id fragment; the slug is an allowlist rather than an escape, which is what
keeps a title holding a `/` or a `..` segment from ever producing a name the download route's own
`name != Path(name).name` guard would refuse to serve.

**Stopping a run in flight.** A firing is a turn like any other, so stopping one is cancelling a task --
but not the *scheduler's* task. `Scheduler.cancel` would reach the job running the firing and would also
unregister it, which turns "stop this run" into "silently disarm this schedule". So `_run_unattended` runs
the turn in a child task and registers that in the `TurnTracker` under the firing's conversation, carrying
the task id; `Assistant.stop_task_runs` cancels every entry for a task, and the scheduler job goes on to
re-arm as though the run had finished. Invariant 7 in `core/turns.py` covers the rest, including how a
stop is told apart from a shutdown (both arrive as a cancellation) and why a firing is never stopped from
inside itself. Three surfaces reach it and all three go through `TaskService.stop`: the panel's Stop
button, the `stop_scheduled_task` tool, and -- because the firing is now tracked like any other turn --
`/stop` on a channel with no conversation list, where a firing runs in the conversation being viewed.

`task_action` is the seam that keeps the panel honest. Every task mutation pairs a write to the task's
`[scheduling.task.<name>]` table with the scheduler (un)arming that must accompany it, and both the
agent's tools and the panel go through the one `scheduling.TaskService` on `LiveState`, so the two cannot
drift: a front end that edited config.toml's task table directly would leave the in-memory scheduler
firing a task the table calls disabled. What they do *not* share is wording: the service returns records
and raises `TaskError`, and
`task_action` returns nothing, because the panel answers with a refreshed task list rather than a
sentence. The action name arrives from the browser, so it is looked up in a table and raises for anything
else rather than being dispatched on.

Grouping a task's conversations under it happens **on the page**, not in the core. `new_session` stamps
`metadata["task_id"]` on any conversation a firing mints, with the task's name -- its identity -- so a
rename has to move that stamp too: `update_scheduled_task`'s `new_name` path calls
`ConversationBook.retag_task` to re-point every conversation the old name owned, right after the table
itself is renamed, which is what lets the sidebar keep a task's history nested under it across a rename.
`ConversationBook.list()` projects `task_id`, but nothing is filtered there -- the agent's read-only
conversation tools walk `sessions()` and still see every conversation. The page nests a conversation under a task when its `task_id` matches a task *currently in
the list*. Requiring the task to be present is what makes
deleting a task return its conversations to the chat list instead of hiding them: keying on `task_id`
alone would leave an orphan unreachable from the sidebar. Because the nesting is client-side, a firing's
new conversation appears under its task with no task re-fetch -- `TurnRunner` pushes the conversation list
when a run starts and again when it ends. The reverse is not true: a task the model schedules or cancels
mid-chat does not push a `tasks` frame, which is what the section's refresh button is for.

That same client-side nesting is how the panel knows a task is running at all. `Assistant.list_conversations`
decorates each row with `running` from the `TurnTracker` (decorated there rather than in
`ConversationBook.list`, which has no view of turn bookkeeping and needs none), and the page offers Stop on
a task when any conversation nested under it is running. Nothing about
running state reaches the `tasks` frame, so the core still never sends one -- which matters because the
task's table does not change when a run starts or stops, and a `tasks` frame is only ever a reply to
something the page asked for.

**Retention.** Every firing mints its own conversation, and a task's `max_conversations` decides how
many survive: `1` replaces the previous run, `0` keeps them all, and a record with no cap of its own
inherits `[scheduling] max_task_conversations` (3), resolved by `TaskService` at fire time so a settings
change reaches the next firing. The prune itself lives in `TurnRunner`, beside the run it follows: it
asks `ConversationBook.sessions_for_task` for the task's conversations oldest-first (by `created_at`,
so a late turn touching an older run cannot make it look newest), reorders them so runs holding no
report come first, and deletes past the cap. It runs after every firing, successful or not, and outside
the turn's gate hold, since the delete takes a `gate.turn` of its own (invariant 1) and the firing's own
conversation is a prune candidate, so run inside the hold it would wait on the per-conversation lock the
same task already owns. It swallows failures the way the run itself does (invariant 6): a user who deleted
a run by hand must not stop the task firing.

That eviction order is what lets the prune run on the failure path at all. Pruning used to be
success-only, so a task failing on every firing was never pruned and accumulated conversations without
bound, past a cap that was supposed to cover exactly that. Simply running it on both paths is not enough
either: at a cap of `1`, an oldest-first eviction would drop the last good report in favour of the
failure that followed it. So `_holds_no_report` marks a conversation whose turn recorded a failure, or
which has no messages at all, and those go first. Both halves are needed, because the reason is keyed to
a turn's user message: a firing that raised before its user turn reached the transcript -- an agent that
would not build -- has no turn to key one to, and only its empty transcript says so. That the cap is enforced
at a firing rather than at an edit is what makes lowering one non-destructive and leaves a disabled task
whole. This is also why the record needs no `session_id`: nothing reuses a conversation, so a firing
writes nothing back to the task's table.

A `tool` frame carries what the call returned as well as what it was asked to do (`response`, added to
AIMU's base frame), because AIMU yields `TOOL_CALLING` only once the call has been dispatched. An error,
an argument-binding failure, and a denied approval are results too and arrive on that same key, so the
page needs no separate failure path and no second frame updating a card in place: the card is complete
when it appears. `renderTool` puts the result in a nested foldable of its own below the arguments, filled
on first expand and clamped to `OUTPUT_CLAMP` characters with a button for the rest, as plain text --
never markdown, since a tool result is untrusted input. Output travels whole and only the DOM clamps, so
a `history` frame grows by every tool result in the conversation and is re-sent on every conversation
switch; if that becomes a problem the fix is a server-side cap in `replay_items`, the path that re-sends.

Replay reaches the same card by a different route. A stored transcript splits a call from its result
across an assistant message's `tool_calls` and a later `role: "tool"` message, joined by
`tool_call_id`; `_tool_results_by_call_id` builds that map so each replayed call carries its own output.
The join has to be by id rather than by position, since concurrent dispatch appends results in completion
order. A call with no matching result -- a transcript stored before results were replayed, a turn cut
short mid-dispatch -- gets `response: None` and renders the card exactly as it always did.

The flattener behind all of this, `replay_items` (with its `_tool_results_by_call_id` and
`image_refs_of` helpers), lives in `core/transcripts.py` rather than in this channel: `send_history`
below is one caller, and a Markdown export of a conversation is the other, so the module both share is
where it has to sit. `WebChannel` imports it back (locally, inside `send_history`, to avoid a cycle
through `core/__init__` -> `assistant.py` -> this module) and renders its items as display frames; the
export renders the same items as prose instead.

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

A loop marker's meaning narrowed with AIMU 0.28.0's `CONTINUING` chunk: it now names an injected round
specifically, one the loop inserted on its own (a continuation nudge after an empty turn, or the forced
wrap-up at the round cap), and `reason` on the frame (`"continuation"` or `"final_answer"`) says which.
Before it, `stream_activity` guessed the boundary from a rise in `StreamChunk.iteration`, which labelled
the cap with the nudge's own wording and could draw a marker after an ordinary tool round too, since the
counter rises there as well and no reload ever reproduced it. A marker now means exactly one thing, live
and on reload alike, and `SubagentReporter.chunk` reads the identical chunk to draw the same boundary
inside a sub-agent card.

Whitespace alone does not open an answer bubble. A server that separates reasoning from the answer
itself (mlx-lm, llama-server, vLLM) sends the newlines that followed the reasoning as the answer
segment's first tokens, so on a turn that reasons and then calls a tool the whole segment can be
whitespace: an empty stamped bubble between the reasoning block and the tool card, reading as a section
whose content failed to arrive. `isBlank` in `app.js` guards the three places a bubble is created from
text (a live `token` frame, and a replayed `partial` or `message` item), so such a segment renders as
nothing at all. Only the *opening* is guarded: once a bubble is open the same whitespace is spacing
between words the reader can see. The frames themselves are unchanged, so nothing about the transport
or the transcript depends on the page's rule here.

Muting a turn is not losing it. Every turn frame is also folded into a per-conversation
`_CatchUpRecord`, which models the page's own append rules (consecutive thinking text collects into one
foldable; answer text collects into an open `partial` bubble that any other block closes, so prose keeps
the place it arrived in and the tokens after that block open a new bubble below it) and is
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

**A failed firing is persisted too.** `_run_unattended` used to reach `_persist` only where the run
returned normally, so a firing that raised left the conversation it minted exactly as minted: zero
messages, `updated_at` still equal to `created_at`, indistinguishable from a conversation that never ran.
Everything the run had done up to the failure lived only on the in-memory agent and went with the next
registry eviction. It now holds the error, snapshots the transcript, and re-raises so `proactive` still
logs the traceback and tells the user. The floor of what survives is the prompt, because a model client
appends the user turn before it sends the request. The reason goes into `metadata["failure"]`, keyed by
user-message index like `model` and `trace`, and *not* into `session.messages`: the messages are what this
conversation's agent rebuilds its context from, so a synthesized assistant turn saying "this failed" would
come back to the model as its own prior words. `replay_items` replays it as a `notice` item at
the *end* of its turn -- held until the next user message or the end of the transcript, so a conversation
the user carried on in keeps the notice inside the turn it describes. An unattended run needs this most,
since `_report`'s status line goes to whichever conversation the user was viewing at the time, leaving the
run's own conversation with no account of why it holds only half a turn.

Planning's reviewer verdicts and a turn's spawned sub-agents share one `subagent` frame type and one
persisted map (`metadata["subagent"]`); `task` on the create event is what tells the two apart.
`replay_items` replays that map on reload, interleaved right after its user bubble; a
verbose-traced turn suppresses the reviewer verdict cards (their content is already in the raw trace)
but still replays its own spawn cards, since a sub-agent it spawned is not part of that trace -- kept by
the create event's id, not by event shape, since a spawn whose text streamed closes with a status-only
event indistinguishable from a reviewer's verdict. The
page's `renderSubagent` builds or updates one foldable card per id, filling its body as `append` frames
arrive; a nested reasoning chunk and a nested tool call are reported the same way a top-level one is,
with nothing in the core deciding whether they are worth sending.

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
and is rendered as markdown
when the spawn hits its terminal status, so a replayed card (which also ends with a terminal status)
lands on the same DOM. This is the one render the page still defers to a terminal status: the
assistant's own reply renders as it streams (see [Streaming the answer](#streaming-the-answer)), and a
card would need the same treatment per answer block to match.

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
