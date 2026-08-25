# Changelog

## 0.1.0 (unreleased)

First release. Kokua starts from AIMU's `examples/personal-assistant/` and restructures it into an
installable, modular application: a small transport-agnostic core with capability pushed into plugins.
Because there is no earlier release, this section describes what 0.1.0 *is* rather than what changed.
The pre-release development history is in the git log.

Requires Python 3.11+ and [AIMU](https://github.com/saxman/aimu) 0.24.0 or newer. Apache-2.0.

### Package and entry points

- `src`-layout `kokua` package with two console scripts: `kokua` runs the selected front end (default
  `cli`), `kokua-web` is a convenience for `kokua --frontend web`. Both route through `kokua.cli`, so
  both run the AIMU preflight described under [Diagnostics](#diagnostics-and-error-reporting).
- **Plugin system** (`kokua.plugins`). Front ends and toolsets are discovered through the
  `kokua.frontends` and `kokua.toolsets` entry-point groups. The built-in `cli` / `web` front ends and
  the built-in toolsets register exactly as a third party's package would, so if the built-in path
  and the plugin path ever diverge, the plugin path is the broken one. Inspect with `--list-frontends`
  and `--list-toolsets`. Discovery is unconditional: installing a distribution that registers an
  entry point is the consent, and there is no switch to withhold it. Nor is any registration treated
  as untrusted: a toolset that fails to import, or whose `build` raises, fails startup naming itself,
  whichever route it arrived by. Kokua ships no third-party code, so it carries no special handling
  for code it does not ship.
- **`toolsets/` holds toolsets and nothing else.** The machinery a toolset is built against (the
  `Toolset` and `Setting` types, name resolution, and the `LiveState` / `ToolsetContext` a `build`
  draws on) is `kokua.registry`; assembling every provider into one namespace, validating an agent,
  and assembling its prompt is `kokua.core.agents`. So a reader looking for a capability finds a file
  with its name, and nothing else to sort past.
- **No startup warning for a registered name no agent declares.** Telling a name the user provisioned
  from one that merely ships meant a provenance rule over the whole namespace, and a toolset nobody
  declares costs nothing to leave unnamed. A configured MCP server does cost something (a handshake,
  and a held credential), and it is the case that lost a signal here; see
  [Add MCP services](docs/how-to/add-mcp-services.md). The distinct warning for a `config.toml`
  section whose owning toolset no agent declares is unaffected.
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
  message; `/think <level>` sets the reasoning effort for the messages that follow (`off`, `low`,
  `medium`, `high`, or `default`), and a bare `/think` reports the current one.
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
  - **A Think picker beside the Plan toggle** sets the reasoning effort for the messages you send after
    it. Sticky like Plan and, like Plan, per request rather than a setting: it rides the message as an
    `input` frame field, writes nothing to `config.toml`, and resets to the configured default on reload.
    It goes inert while Plan is on, because a planned turn runs its own agents at their declared efforts.
  - **Replies render as GitHub-flavored markdown while they stream**, via vendored `marked` +
    `DOMPurify` (bundled, served locally, no CDN). The rendered HTML is sanitized, so model and tool
    output cannot inject scripts or markup, and links open with `rel="noopener"`. The page reparses the
    whole accumulated answer on a 50ms timer rather than committing each finished block as it lands,
    because a newline is not a block boundary in CommonMark: a lone newline continues the paragraph,
    and a newline inside a fence, list, or table sits inside an open block, so a committed prefix could
    be wrong in a way no later token could repair. Reparsing converges on the final render by
    construction. LaTeX math is typeset with vendored KaTeX after sanitization, with `trust:false` and
    a `maxExpand` cap (a malformed expression is left as source text rather than breaking the row), and
    only once the turn completes: KaTeX is the expensive half of the render and half-typed TeX is noise,
    so mid-stream the expression stands as the source the model wrote.
  - **A turn renders in the order it was produced.** An answer the model wrote before calling a tool
    stays above the card for that call: the block closes the answer bubble, and the tokens that follow
    open a new one below it. A turn that says something, calls a tool, and then says more therefore
    reads top to bottom in all three renderings -- live streaming, a background turn's catch-up replay,
    and a reloaded transcript, where a stored assistant message's prose replays ahead of the
    `tool_calls` it carries rather than behind them.
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
    their top-level counterparts. The child's text streams in as plain text and renders as markdown when
    the spawn ends, which is the one place the page still waits for a terminal status to render (the
    assistant's own reply now renders as it streams). The parent's own duplicate `spawn_subagent` tool block is suppressed. Recording and
    display are independent, so a background turn's spawns are recorded even though its frames are
    muted, and switching in later shows the work.
  - **Rows carry a localized datetime caption**, revealed on hover (full precision in its tooltip) so
    the transcript is not dated line by line. On a foldable it rides the always-visible header, so it
    shows collapsed or expanded. Ephemeral chrome (notices, approval prompts, banners) is not stamped.
    Messages persisted before timestamps existed render without a caption.
  - **Agent-loop continuations are distinguished from user input.** The loop injects its own turns as
    `user`-role messages; these render as a dim `continuation` row showing the injected prompt rather
    than as the user's own turn. A proactive message keeps its uppercase label.
  - **No settings window.** The page's one settings control is a theme button in the header, cycling
    auto / light / dark; it is a per-browser `localStorage` preference applied before first paint and
    never reaches the server. Everything else runtime-mutable -- the planning toggles, and whatever else
    a toolset declares hot -- is changed by asking the assistant, whose `update_config` hot-applies the change
    for the next turn and persists it to `config.toml`. That keeps one path for every hot setting,
    including a third party toolset's, instead of a panel that could only show the ones its markup
    named. (The `settings` / `get_settings` control frames remain part of the web transport's contract
    for a front end that wants to render them.) The model and the reasoning effort are not
    runtime-mutable at all: both are read at startup, from `[assistant].model` / `[assistant].thinking`
    or an agent's own `[agents.<name>]` keys. Change them in `config.toml` and restart.
  - **Sampling parameters are AIMU's, not a Kokua setting.** There is no `[generation]` section and no
    generation fields anywhere in Kokua's settings. AIMU 0.16.0 resolves generation kwargs through one precedence chain
    (the client's fallbacks, then the model card's tuned profile, then `client.default_generate_kwargs`,
    then the per-call dict), and Kokua writing that third tier from config duplicated the chain while
    shadowing the card: a model whose card specifies a tuned `temperature` / `top_p` / `top_k` / `min_p`
    lost it to whatever that section set. `[generation]` gets no special handling on the way out: it is now
    an unrecognized section, so a stale one left in a `config.toml` fails to load the way any unknown key
    does, and deleting it is the fix.
  - Reloading the page replays the prior conversation, reasoning and tool calls included. What was
    streamed live is what is replayed: nothing in the core decides which frames are worth sending, so a
    front end that wants to fold or hide a block does that with the block in hand.
  - **A whitespace-only answer segment renders as nothing.** A server that separates reasoning from the
    answer itself sends the newlines that followed the reasoning as the answer segment's first tokens, so
    on a turn that reasons and then calls a tool the whole segment can be whitespace. That used to open an
    empty stamped bubble between the reasoning block and the tool card, reading as a section whose content
    failed to arrive. Live and on reload, whitespace alone no longer opens a bubble; inside an open one it
    is still the spacing between words.

### Agents and tools

- **An individual skill is a name in the same namespace as everything else.** A `[agents.*]` table lists
  `"citation-check"` beside `"web"` and `"stocks"` without saying which kind each is, and `--list-toolsets`
  shows every skill on disk under a `skill:` group. Two paths deliver one declaration, split on who
  authors: the **entry agent** stays an AIMU `SkillAgent`, and its catalogue is scoped with
  `SkillManager(include=...)` to the skills it declares; a **worker**, which is a plain agent with no skill
  machinery, gets a declared skill's script tools and `activate_skill` through the registry like any other
  toolset, and the skill's description reaches its prompt as that toolset's guidance. **Declaring the
  `skills` authoring toolset opts an agent out of scoping**, because narrowing an author's catalogue would
  hide the skill it just wrote and make `add_skill_script`'s "callable in the same turn" promise false. A
  skill whose name collides with a toolset or an MCP server is a startup error, like any other collision,
  and a skill on disk that no table names is simply available to an authoring entry agent through its
  catalogue anyway.
- **Every agent is declared whole in `config.toml`.** One `[agents.<name>]` table per agent, carrying a
  `description` (the label a delegator sees), a `system_message`, a `tools` list of toolset names, and a
  `delegates_to` list. `[assistant].agent` names the **entry agent**, the one you talk to and the root of
  the delegation graph. `assistant`, `researcher`, and `coder` ship as live tables in
  `config.example.toml`, which is what `kokua config init` writes, and are edited, deleted, or added to
  like any other setting. Nothing about an agent is defaulted in code, so **Kokua refuses to start
  without a config file, with one that defines no agents, or with one whose `[assistant].agent` names no
  table**, naming a remedy that works from that state -- a config file that exists but declares no
  agents is told to add an `[agents.<name>]` table by hand or overwrite it with `kokua config init
  --force`, since plain `kokua config init` refuses a file that is already there -- rather than running
  something that looks alive and cannot work.
- **One namespace of named toolsets.** A *toolset* is one named capability an agent can declare: a name,
  a description, a `build(ctx)` returning tool callables, and optional `guidance`. Every provider lands
  in one namespace -- AIMU's built-in tool groups plus its two stores and skills, Kokua's own `capabilities` /
  `config` / `conversations` / `mcp` / `scheduling`, each installed plugin toolset, and each configured
  MCP server by its (now required) `name` -- so a `tools` list says `"stocks"`, never `"mcp:stocks"`, and a
  capability can change provider without touching an agent. The cost is paid at startup: two providers
  claiming one name is a `ConfigError` naming both. `kokua --list-toolsets` prints the whole registry
  grouped by provider, and is the discovery command for the namespace.
- **A capability is declared, never defaulted.** There is no code path that adds a tool an agent did not
  name, and no flag that can disagree with a declaration. Not even the clock: `time` is a toolset that
  every agent wanting one declares. The state a toolset draws on is a lazy property on one `LiveState`, so
  two agents declaring one toolset share one store rather than opening two, and the memory and document
  stores are opened because some agent declared them and not otherwise.
  An unknown name raises rather than being dropped, since a dropped name is a declaration silently
  overruled. The one exception is a sub-agent composed by `compose_subagent` (below), and it is entered
  by declaration too: only an agent whose own table names `capabilities` can compose one, and what it
  composes is one task's sub-agent rather than an agent the config describes.
- **`spawn_subagent(agent_type, task)`** is typed, and a non-empty `delegates_to` is the whole switch
  for it: that agent gets a delegate offering exactly the agents it names, each running on its own
  declared model (or the default) with the tools its own table declares. Delegation nesting is Kokua's rather than AIMU's `max_depth`,
  so an agent you delegate to that delegates in turn gets its own menu of its own targets; the graph must
  be acyclic and a cycle fails startup printing the cycle as a path. Independent spawns in one turn run
  concurrently (`[assistant] concurrent_tools`, default on). A worker's gated-tool call is routed to the
  user for approval and is never run unattended.
- **Capability discovery** (`capabilities` toolset). An agent can read the toolset registry at runtime
  with `list_capabilities` and, when no declared sub-agent role fits a task, call `compose_subagent` to
  build a sub-agent holding exactly the capabilities that task needs. The composed sub-agent is
  constructed per call, runs one task, and is discarded; its tools still route through
  `[security].confirm_tools`. `[capabilities].max_depth` (default 3, `0` off) bounds how far composition
  nests: at 3 a chain reaches three sub-agents, and the last of them holds neither tool, since a
  `compose_subagent` with no way to look up capability names is useless to whatever holds it. A
  declared role is ranked above composing one in the prompt guidance, since its instructions were
  written for its job where a composed one's are written in the moment.
- **Guidance travels with the capability.** Each toolset carries the prompt text that makes the model use
  it, appended to any agent holding it, so installing a toolset brings its instructions and removing one
  takes them away. An agent's system message is its own opener (falling back to
  `[assistant].system_message`, then the built-in default), then its toolsets' guidance in declared
  order, then the delegation instructions when it delegates, and finally a "you are a lean supervisor,
  you MUST delegate" clause only when every toolset it declared is marked `cross_cutting` -- something an
  agent holds to manage itself rather than to do domain work. `cross_cutting` decides that one sentence
  and nothing else: it is **not** an authorization boundary, and an agent declaring `config` really does
  get `update_config`. The boundaries are that `[agents.*]` is locked by default and that `confirm_tools`
  gates by tool name whoever calls it.
- **The prompt names what cannot be answered from memory.** A model that believes it knows an answer
  never reaches for a tool, so both halves of the prompt state the trigger as a property of the question
  (could the answer have moved since training, could the user check it against a source: current events,
  prices, releases, published figures, who holds a role, what a page says today) rather than as the
  model's own confidence, which is the signal it is worst at reporting. A delegating agent is told to
  delegate those "even when you think you know"; the `web` toolset, the one AIMU group carrying guidance
  of its own, tells whoever holds the tools to look it up rather than recall it and to say where the
  answer came from. The two reach different agents: the shipped `[agents.assistant]` holds no web tools
  and gets the first, `[agents.researcher]` does not delegate and gets the second.
- **`--system` overrides the entry agent's opener for that run**, winning outright over its declared
  `system_message` (and over the `[assistant].system_message` fallback), since the message a person is
  talking to is a prompt, not the capability `[agents.*]` is the single source of. A worker's own
  declared opener is untouched: the flag only ever reaches the agent the user is talking to.
- **The shipped config, not the code, is what makes the assistant lean.** `[agents.assistant]` declares
  the cross-cutting toolsets and no domain tools, delegating web, file, and compute work, which keeps the
  always-on agent's tool context small; give it `"compute"` and it runs Python itself. The one structural
  restriction is `skills`, marked entry-point-only because a spawned worker is a plain AIMU `Agent`
  rather than a `SkillAgent`, so declaring it elsewhere fails startup instead of producing a worker whose
  skill tools quietly do nothing.
- **Startup validates the whole graph before touching anything.** An unknown toolset name (with the
  valid names listed), a duplicate provider name, a missing or absent entry agent, no agents at all, an
  unknown delegation target, a delegation cycle, and `skills` on a non-entry agent each fail before the
  session store is opened or any MCP server connected, so a bad config leaves nothing written and nothing
  connected. One agent shape backs this: `wire_agent` builds any agent from its declaration, selecting
  its toolsets once and feeding the same list to both its tools and its prompt.
- **The assistant can look across your other conversations.** The `conversations` toolset, three
  read-only tools:
  `list_conversations` (ids, last-active times, message counts, titles), `read_conversation` (one
  transcript as plain text), and `search_conversations` (case-insensitive text across every saved
  conversation, with snippets). So "what did we decide about the deployment last week?" is answerable,
  and context from a past thread can be carried into a `spawn_subagent` task. The shipped config gives it
  to the entry agent and to no worker, and a test pins that: a worker shares no history and has no
  conversation identity, so the capability would widen a spawn's blast radius for nothing.

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
- **Agent tools are findable, and are only presentation.** Every module defining an `@aimu.tool` is a
  toolset module, so `grep -rl '@tool' src/kokua/` finds only files under `toolsets/`. Kokua's own five
  (`capabilities.py`, `config.py`, `conversations.py`, `mcp.py`, `scheduling.py`) each wrap one
  subsystem (`capabilities.py` wraps the registry itself) and export the `TOOLSET` for it, indexed in
  `toolsets/core.py`. The subsystem underneath (`core/transcripts.py`, `config/store.py`,
  `mcp/servers.py`, `scheduling/tasks.py`) holds only what agents and front ends both need: it returns
  records and raises typed errors, and formats no sentence. Because about half the 30 tools the shipped
  entry agent holds come from AIMU and cannot be grepped here at all,
  [docs/explanation/architecture.md](docs/explanation/architecture.md#how-an-agents-tools-resolve) carries
  the full inventory with the factory that builds each and the toolset it is declared as, and
  `tests/core/test_build.py` pins it as an **exact set**: adding a tool to the entry agent fails the suite
  until the table is updated in the same commit, and a plugin toolset leaking onto it fails too, naming the
  offender.
- **Skills**: AIMU's skill authoring plus runnable skill scripts, as the `skills` toolset.
- **Memory**: a `SemanticMemoryStore` for facts (`memory`) and a `DocumentStore` for documents
  (`documents`), shared by every agent that declares them. Concurrent-turn safety lives inside AIMU's
  stores (a re-entrant per-store lock) rather than a Kokua-side wrapper.

### Bundled skills

Kokua ships three Agent Skills in a repository-level `skills/` directory, deliberately outside the
package and therefore outside the wheel: these are content, not Python, and keeping them in `src/` would
put them back in the source tree. `kokua skills list` shows what is bundled with each declared
description; `kokua skills install [name...]` copies them into the skills folder your config resolves,
honouring `[paths].data_dir` and `$KOKUA_HOME`, and leaves an already-installed skill of the same name
alone unless you pass `--force`, so a local edit survives a reinstall. Both commands run before the AIMU
preflight, since copying files needs no AIMU surface. An installed-from-PyPI Kokua has no copy and the
command says where to get one.

- **`markdown-to-pdf`** renders Markdown to a PDF in `data/downloads/`, which the web front end serves
  at `GET /download/<name>`, and reports that link. **This was the `pdf` toolset.** Its script declares
  `fpdf2` and `markdown` inline with [PEP 723](https://peps.python.org/pep-0723/) and `uv` resolves them
  per run, so **both left Kokua's dependencies entirely**. Path traversal in the output name is stripped
  to a bare filename, and fpdf2's Latin-1 core fonts mean smart quotes, dashes and arrows fold to ASCII
  (lossy, and expected).
- **`email-report`** mails a Markdown body to you as HTML with a plain-text fallback, optionally
  attaching a file already in `data/downloads/` or `data/images/`. **This was the `email` toolset**, and
  it keeps both properties that one had by construction: there is no recipient flag at all, so it can
  only ever mail the configured address; and every attachment resolves before connecting, so a bad name
  sends nothing rather than an email the caller believes carried the file. On failure it reports only the
  exception type, because SMTP errors can echo credentials.
- **`dice-roller`** rolls standard dice notation and prints JSON. **This was the `example` toolset**, and
  it stays the worked example: no dependencies, so no `uv run` needed, and nothing host-specific to copy
  around.

A script cannot discover where Kokua serves downloads from or which address it may mail, so
`LiveState.script_env()` passes the downloads and images folders and the `[email]` settings into each
script's environment (AIMU 0.14.1 threads them to the subprocess). Deriving them in a script would mean
re-implementing `config/paths.py` and drifting from it. `KOKUA_EMAIL_PASSWORD` is deliberately not
passed: a subprocess already inherits it, so copying it would duplicate a secret for nothing.
That map travels by two routes, because a skill script reaches an agent by two. A spawned worker takes
its skill tools from the registry, where `LiveState.skill_tools` passes the env to
`build_skills_server`. The entry agent is a `SkillAgent`, which builds its own skills server and so
cannot be reached that way; `wire_agent` hands it the same map as `SkillAgent(script_env=...)`, which
needs `aimu>=0.20.0`. A route that misses it raises nothing: the script runs with the settings simply
absent and reports itself unconfigured, which is why `tests/core/test_build.py` pins the entry agent's
copy directly.

A skill carrying scripts belongs on an agent that also declares `fs` and `compute`, since running the
script is how the skill does its work.

### Toolsets

**All 21 toolsets Kokua ships are one file each under `src/kokua/toolsets/`, named for the toolset, and
registered in `pyproject.toml`'s `kokua.toolsets` entry-point table** -- the same table a third party's
package writes into. There is no second route, no index in code, and no directory scan: that table is the
index. Adding a toolset is a new file and one line, and `tests/toolsets/test_registration.py` fails until
both exist and agree, in both directions and on all three names (file stem, entry-point key,
`TOOLSET.name`). That last one matters because `register` keys on `TOOLSET.name` while the entry key feeds
only the provenance label, so a mismatch would otherwise register a toolset under a name nobody wrote.

The directory holds toolsets and nothing else. The machinery is `kokua.registry`, namespace assembly is
`kokua.core.agents`, and `toolsets/__init__.py` exports nothing (Python needs it for the package to be
collected into the wheel and importable by name).

**`mcp-admin` is now `mcp`**, since a toolset's file name is its name. A `tools` list saying `"mcp-admin"`
fails startup on the unknown name.

**The `compute` toolset now carries a shell tool, `run_command`, alongside `calculate` and
`execute_python`.** It runs a command line through `/bin/sh -c` and returns the exit code with stdout
and stderr labelled separately; a nonzero exit comes back as output rather than an error, since `pytest`
exits 1 with the answer on stdout. The timeout is per call, defaulting to 30 seconds and clamped to 600.
The shipped `[agents.coder]` is the only agent declaring `compute`, and its `system_message` already told
it to run "Python, shell, or calculations," which is true for the first time; the toolset module's
identical docstring claim was equally aspirational and is now accurate. **It arrives gated**:
`config.example.toml` adds `run_command` next to `execute_python` in `[security].confirm_tools`, so a
command reaches you for approval before it runs, including one a sub-agent asked for, since a worker's
gated call routes to the parent's gate rather than running unattended. A `config.toml` scaffolded before
this release still gates `execute_python` alone; add `run_command` to it by hand. **The reviewer does not
get it.** `workflows/critics.py` names `builtin.calculate` individually rather than taking the whole
`compute` group, precisely so a future widening of that group cannot reach an agent with nobody to ask
for approval, and `tests/workflows/planning/test_reviewers.py` now asserts the absence by name rather
than relying on a set intersection staying empty. Needs `aimu>=0.24.0`, which is what moved the floor
there: `builtin.make_command_tool` and `run_command`'s membership in `builtin.compute` are both new in
that release, and the startup preflight's capability probe moved to `make_command_tool`, a plain name
lookup, since the capability and its handle are the same object.

**`[compute] command_env_passthrough`** names, comma separated, which environment variables a
`run_command` child may see on top of a fixed allowlist (`PATH`, `HOME`, the locale and temp variables,
plus `SHELL`, `TERM`, `TZ`, `USER`, `LOGNAME`). Nothing else reaches the child, API keys included, which
is what stops `run_command("env")` lifting a credential into the model's context on a machine whose
launcher sources a `.env`; the same default makes `gh`, `ssh`, and `git push` over ssh fail, which is the
policy working as intended, and naming the variable here is how you grant it back. It is a string rather
than a list because a toolset setting is a string, an int, or a bool, and it is cold rather than hot
because the value is baked into the tool's closure when an agent is built; a hot flag doing nothing until
a restart would be worse than a cold one that says so up front. The key is also locked by default in
`[security].locked_config_keys`, added beside `email.to` rather than matched by an existing pattern,
since without it the assistant could name one of its own credentials there and read it back out of a
command's environment. **Neither execution tool is a sandbox, and `run_command` is one step less of one
than `execute_python`:** a shell string reaches a credential sitting in a file with no code for anyone
to read first, and process signalling is unconfined either way. `run_command` also carries no memory
cap, unlike `execute_python`'s 512 MB address-space limit, since that cap would break compilers and
test suites and a shell child's own cap would need `preexec_fn`, which is neither portable nor
thread-safe. The approval gate is the control.
`tests/toolsets/test_compute.py`, `tests/core/test_build.py`, and `tests/workflows/planning/test_reviewers.py`
cover it.

The four below are Kokua's own standalone capabilities, needing nothing but `AssistantConfig`. They are
the shortest worked examples of the shape: one file, one `TOOLSET`, one entry-point line.

- **`image`**: `generate_image`, offered only when `AIMU_IMAGE_MODEL` is set (e.g. `gemini:nano-banana`
  or an `hf:<repo>` diffusers model).
- **`aimu_agents`**: mounts AIMU's prebuilt `CodeReviewAgent`, `ResearchReportAgent`, and
  `ContentCreationAgent` as the tools `code_review`, `research_report`, and `create_content`. It exists
  mainly as the worked example of wiring an AIMU-built agent into Kokua: every `Runner` exposes
  `.run(task) -> str`, so a toolset is the entire bridge and the core gains no new surface. Each call
  builds a fresh agent on `[assistant].model`, the same default an agent that declares no model of its
  own runs on. The
  caveats are documented in the module: the prebuilts are synchronous, so a nested run gets no sub-agent
  card, no `/stop`, and no approval gate on its workers.
- **`benchmark`**: one tool, `benchmark_model`, measures the model the agent holding it runs on and
  reports time to first token and output speed in tokens per second, as a median and a range over three
  timed runs. The two metrics are separated because they have different causes (prompt processing and
  queueing against decoding), and a discarded warmup run is reported on its own line because a first
  request can carry a cold model load worth many seconds, which would otherwise read as a slow model. It
  takes no arguments: the model, the sampling parameters, and the reasoning effort all come from
  `config.toml`, so there is nothing to redirect and nothing a per-call approval would protect. Token
  counts come from the provider's own usage report where there is one (Ollama, Anthropic, and the
  OpenAI-compatible providers all report it even on a streamed call) and from counting stream chunks
  where there is not, and the report says which, since a chunk is usually a token but nothing guarantees
  it. It builds its own client rather than borrowing the agent's, which is what keeps it from racing the
  turn it runs inside for `last_usage`, and it carries an empty system message so the figure describes
  the model rather than Kokua's own multi-thousand-token prompt. Any agent may hold it, and what it measures is its own holder: the
  model comes from `ToolsetContext.agent_name`, so a worker running on a model of its own is timed on
  that model, and the report names the agent whose model it timed rather than leaving a reader to assume.
  Resolution goes through `config.model_for` rather than the live agent because nothing on a model client
  retains the string it was built from. An in-process model (`hf:`, `llamacpp:`) cannot be benchmarked and the tool says so: a second
  client over one model would load its weights twice, so AIMU refuses to build one.
- **`github_backup`**: one tool, `backup_kokua_state`, mirrors `config.toml`, `sessions.json`, the
  memory store, saved documents, and authored skills into a private GitHub repository as a git commit,
  pushing from a working tree under `data/backup`. It takes no arguments, which is what lets it run
  ungated and therefore from a scheduled task; the repository and branch come from `[github_backup]` and
  the token from `$GITHUB_BACKUP_TOKEN`, which never reaches a command line or `.git/config`. A public
  repository is refused, an unchanged state makes no commit, and a diverged remote is reported rather
  than force-pushed. Three things keep the reported outcome honest across runs: repointing `repo` at a
  second repository is refused while `data/backup` still tracks the first (Kokua will not verify one
  repository and push to another), a commit an earlier push failed to deliver is pushed rather than
  reported as an existing backup, and one lock serialises the working tree so two concurrent turns
  cannot push a half-copied memory store as a success. The toolset offers no tool until `repo` is set.
  Logs, downloads, and images are excluded, and an in-tree `.gitignore` excludes anything further.
  Restore is manual: see
  [Back up to GitHub](docs/how-to/back-up-to-github.md).

Nothing a toolset contributes reaches an agent until that agent's `tools` list names it, and nothing
warns you about a name no agent named: a toolset that ships and is never declared costs nothing to leave
alone. The case that does cost something is a configured MCP server, which connects regardless; see
[Add MCP services](docs/how-to/add-mcp-services.md).

### Proactive work: scheduled tasks

- Durable, agent-managed tasks that fire an unprompted turn when due, declared as
  `[scheduling.task.<name>]` tables in `config.toml` and re-armed at startup. A task's **name** is its
  identity: it is required, unique, the table key, and the handle every scheduling tool and the sidebar
  use in place of an internal id. A table can be hand-written or hand-edited, comments and all, the same
  way an `[[mcp.server]]` entry can: the comment lines directly above a task's header are that task's, so
  they stay put when it is renamed and go with it when it is cancelled, never sliding onto its neighbour.
  Cancelling and renaming also cover the shapes a hand edit can write and the app never does: a task
  written as an inline table, and one task whose keys are split across several fragments, both move or
  go whole rather than in part.
  A malformed table is rejected at startup, naming the table rather than
  becoming a task that silently never fires. Schedules are one-shot, interval, daily, or weekly (no cron
  dependency).
- Managed by the assistant through `schedule_task`, `list_scheduled_tasks`, `get_scheduled_task`,
  `update_scheduled_task`, `cancel_scheduled_task`, `disable_scheduled_task` / `enable_scheduled_task`
  (pause without losing the task), `run_scheduled_task` (run one now, reproducing a real firing, so
  a task can be dry-run before it is due), and `stop_scheduled_task` (end a run that is happening now).
  `[scheduling.task.*]` is refused by `update_config`, even though the assistant writes it: a task
  write has to be paired with the scheduler (un)arming that accompanies it, which a bare config write
  would skip, so the scheduling tools are the only path in.
- **A fired one-shot is retired in place, not deleted.** Its table stays, with `enabled = false` and a
  `fired_at` stamp, so the run stays on the record and re-running it is a one-character edit.
  `cancel_scheduled_task` still removes the table outright.
- **A run in flight can be stopped**, from the task row in the web sidebar, by asking in chat, or with
  `/stop` on a channel where a firing runs in the conversation being viewed. Stopping ends that run
  only: the task stays on its schedule and fires again as usual, so a task that should not come back is
  still a `disable`. The stopped run keeps whatever it produced in its own conversation, with the stop
  recorded against that turn the way a failure is (and treated like one by retention, so a stopped run
  is evicted before a report). A firing is never stopped from inside itself, which would cut its own turn
  off mid-tool-call. This is also why a firing runs in a child task: cancelling the scheduler's job
  would have disarmed the schedule as a side effect.
- `update_scheduled_task` edits any subset of a task's fields in place, keeping its `created_at` and
  retention cap, and re-deriving the schedule fields you omit, so changing a weekly task's time keeps
  its day. It re-arms only when the schedule actually changes, so editing a prompt never restarts an
  interval countdown, and it rejects an invalid schedule, a past one-shot, or a name another task
  holds without writing anything. `new_name` renames a task: the rename moves its table in
  `config.toml` and re-points the conversations it has already run, so its history follows it under
  the new name.
- **Every firing runs in its own conversation**, stamped with the task's name, and a per-task
  `max_conversations` says how many of them survive: `1` means each run replaces the one before it,
  `0` keeps every run forever, and a task that names no cap follows `[scheduling]
  max_task_conversations` (default 3), read at fire time so a change reaches the next firing without a
  restart. Pruning happens after every firing, successful or not, evicting runs that hold no report
  before ones that do, so a task that keeps failing cannot grow past its cap and a cap of `1` still
  keeps the last good report rather than the failure that followed it. Lowering a cap deletes nothing
  until the task next fires, so an edit is not destructive and a disabled task keeps everything. A run
  the user already deleted is not an error. A channel with no conversation list (the CLI) has nowhere to
  put a minted conversation, so there a firing runs in the one being viewed and prunes nothing.
- A failing firing is reported and swallowed rather than propagating into the scheduler, which has no
  handler of its own. **Its conversation is still persisted as far as the run got** -- at minimum the
  prompt, since the model client appends the user turn before it sends the request -- with the reason it
  stopped recorded against that turn and replayed as a notice at the end of it. Without that, a firing
  that failed left a conversation with nothing in it at all, and the status line explaining why went to
  whichever conversation the user happened to be viewing.
- **A tasks section in the web sidebar**, below the conversation list, showing each task's name,
  schedule, and next firing, with disable/enable, run-now, and delete per row. A task with a run in
  flight pulses, keeps its buttons visible rather than waiting to be hovered, and offers Stop in place of
  run-now; the run's own conversation appears under it as it starts, marked running, rather than only once
  it has finished. It hides itself entirely
  when there are no tasks, collapses (remembered per browser), and scrolls independently of the
  conversation list so neither can crowd the other out. Creating and editing tasks stays in chat, where
  the model turns a natural-language schedule into a validated one.
- The panel's actions and the agent's tools share one implementation (`scheduling.TaskService`), so a
  table write and the scheduler (un)arming that must accompany it can never come apart. They do not
  share wording: the service returns records, and the panel and the model each phrase their own. The
  action name arrives from the browser and is allowlisted rather than dispatched on.
- **Each task's conversations are nested under it** in that section and left out of the chat list, so
  the chat list holds only conversations you started. A conversation records the task that minted it
  (`task_id` in its metadata, set to the task's name -- its identity -- so a rename re-points every
  conversation the old name owned, right after the table itself is renamed). Grouping happens on the
  page, not in the core: nothing is filtered out of `ConversationBook.list()`, so the agent's
  conversation tools still see every conversation. A conversation whose task has been deleted falls back
  into the chat list rather than becoming unreachable. Each nested row carries its own delete, like a
  chat row: a task that mints a conversation per firing accumulates them here, and the run being viewed
  is deletable too. Deleting the conversation a `target="task"` record remembers is safe -- its next
  firing mints a fresh one rather than failing.
- **Breaking, and with no migration.** Scheduled tasks used to persist as JSON records in
  `data/scheduled_tasks.json`, keyed by an internal uuid; they now live in `config.toml` as
  `[scheduling.task.<name>]` tables, keyed by name. An existing `data/scheduled_tasks.json` is ignored
  and left on disk rather than imported, and a conversation stamped with an old uuid `task_id` stops
  grouping under its task, since nothing in `config.toml` matches that id any more. Re-create any tasks
  you want to keep, or copy them into `config.toml` by hand.

### Workflows: a pluggable turn strategy

- **A `Toolset` can carry a `Workflow`, a named turn strategy, instead of (or alongside) tools.**
  Declaring that toolset in an agent's `tools` is what gives the agent the workflow's `/`-command,
  resolved from the same toolset registry every other capability is -- a turn strategy is granted the
  same way a tool is, and no code path grants one a config did not declare. Deep planning, described
  below, is the first workflow and is no longer a hardcoded core feature.
- **`[agents.*].tools` must name `planning` for `/plan` to exist**, on the web Plan toggle as much as
  the CLI. An existing `config.toml` that predates this release and does not list it gets no startup
  warning, since nothing warns about an undeclared toolset -- but typing `/plan` still gets an actionable
  reply rather than
  the model answering the literal `"/plan <task>"` text: the serve loop recognizes any command an
  installed workflow-bearing toolset offers, even one the entry agent didn't declare, and says which
  toolset to add. The fix is one line: add `"planning"` to the entry agent's `tools`.
- **A third party ships a workflow exactly the way it ships a toolset**, through the `kokua.toolsets`
  entry-point group -- no separate mechanism for a turn strategy versus a tool.
- **Two tiers.** A workflow builds an `aimu.aio.AsyncRunner`, so any of AIMU's own
  `aimu.aio.workflows` runners (`Chain`, `Parallel`, `Router`, `EvaluatorOptimizer`,
  `PlanExecuteEvaluator`) works as a base-tier workflow with no adapter: Kokua streams its `run()`
  into the reply. **A base-tier turn is not persisted**: the runner appends nothing to the agent's own
  transcript, so there is no message to anchor the turn to, and the exchange is gone after a reload.
  That is documented behavior a base-tier workflow author needs to know going in, not a bug to file.
  The rich tier (a runner that also implements `run_turn()`) gets the channel, the human-decision
  slot, and control of the transcript instead, which is what lets deep planning persist a planned turn
  as a plain user/assistant pair.
- **A toolset can own a whole `config.toml` section.** Declaring `settings=(Setting(key, kind,
  default, hot=...), ...)` on a `Toolset` gives it a `[<name>]` section -- always named after the
  toolset itself, so the namespace's existing duplicate-name check also keeps two toolsets from
  claiming one section, and a name colliding with a section Kokua's own core already parses is refused
  at startup. `hot=True` is what lets `update_config` change a setting live; a cold one is a
  startup-only key like any other. Deep planning is the first user: `toolsets/planning.py`
  declares `[planning]`'s five keys (`plan_review`, `plan_review_agent`, `result_review`,
  `show_reasoning` hot; `review_rounds` cold), and a workflow reads its own section as attributes
  through `WorkflowContext.settings`.
- **Breaking for anything importing the old fields.** `AssistantConfig.plan_review`,
  `plan_review_agent`, `result_review`, `review_rounds`, and `show_reasoning` are gone, replaced by
  `config.toolset_settings["planning"][...]`. `config.toml` itself is unchanged -- the `[planning]`
  section still parses and still means the same thing, so no user action is needed -- but code that
  read those attributes directly has to change.
- **A contributed setting's wire key is namespaced** (`planning.plan_review`) to keep two toolsets from
  colliding in the one flat settings payload; Kokua's own core settings stay bare.
- **Startup warns about a configured-but-undeclared toolset.** A `config.toml` section belonging to a
  toolset no agent's `tools` names is now its own warning, distinct from the provisioned-toolset
  warning below: the section still parses and seeds its declared defaults, but nothing reads them and
  any command the toolset offers does not exist.

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
  and documents, skill authoring, MCP mutation, and both execution tools, `execute_python` and
  `run_command` -- a reviewer cannot be approval-gated, since an autonomous critic has nobody to ask
  mid-review, so it is given nothing that would need a gate. Both prompts warn that the reviewer's own
  knowledge may be stale, that disagreement with memory is not evidence of fabrication, and that a
  suspected inaccuracy must be verified with
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
  sits at its built-in default with a line of description. The long form, with what each key accepts,
  which keys apply live, and who may write each one, is
  [docs/reference/configuration.md](docs/reference/configuration.md). The example file stays short on
  purpose: `read_config` hands the assistant the scaffolded file, so its comments occupy the model's
  context on every configuration question.
- **Strict parsing**: an unknown key or non-table section fails fast with a `ConfigError` naming the
  key, so typos and removed keys surface immediately instead of being silently ignored.
- **`[agents.*]` describes every agent, and replaces the per-role vocabulary this line of development
  went through.** Nothing has been released, so there is nothing to migrate from, but a config or a
  script written against an earlier commit of this development line hits the following, and a config key
  in the left column fails with a message naming its replacement rather than a generic unknown-key error:

  | Was | Now |
  |---|---|
  | the whole `[tools]` section (`groups`, and `--tools` overriding it) | each agent lists what it holds in `[agents.<name>].tools` |
  | `[subagents.roles.<name>]` | `[agents.<name>]` |
  | a role's `groups` / `tool_packs` / `mcp_servers` | one flat `tools` list over the single namespace |
  | `[subagents].concurrent` | `[assistant].concurrent_tools` |
  | `[assistant].memory` (and `--memory` / `--no-memory`) | declare the `memory` and `documents` toolsets on an agent |
  | an implicit `time` group on every agent | `"time"` in the `tools` list of each agent that wants a clock |
  | an optional `[[mcp.server]].name` | required: it is how the server enters the namespace |

  `[assistant].agent` is the new key naming the entry agent. The `--tools`, `--memory` and `--no-memory`
  flags are gone rather than deprecated, since a flag that could contradict a declaration is exactly what
  the declarative model rules out; argparse rejects them as unrecognized. `--list-tool-packs` became
  `--list-toolsets` and widened: it prints the entire registry grouped by provider, not just the installed
  plugins, and reads the config file first because the registry depends on it. The plugin contract renamed
  with the concept: `ToolPack` is `Toolset` (with `build(ctx)` in place of `build(config)`), the
  entry-point group `kokua.tools` is `kokua.toolsets`, and `src/kokua/toolpacks/` is `src/kokua/toolsets/`.
- **One runtime-settings table.** `config/table.py`'s `SettingsTable` -- built at startup from
  `CORE_RUNTIME_SETTINGS` plus every installed toolset's own hot `Setting`s -- is the single
  declaration of what can change without a restart, driving the TOML schema, the incoming-payload
  sanitizer, the hot-apply set, the live-apply loop, and the persist path at once. Adding one of
  Kokua's own is one `CORE_RUNTIME_SETTINGS` entry; adding a toolset's is one `Setting` on the toolset
  and nothing else. Both are enforced by tests. `CORE_RUNTIME_SETTINGS` ships **empty**: every runtime
  setting Kokua has turned out to belong to a capability rather than to the core, so every one of them
  is that capability's own declaration.
- **The assistant can inspect and repair its own configuration**: `read_config` / `update_config`.
  `update_config` validates and coerces the value, applies hot-appliable keys immediately and persists
  only after a successful apply (so a flag that cannot be applied is not saved), and reports "restart
  required" for everything else. Which of the two happened is in every result, because most of what the
  assistant is asked to change -- the model and the reasoning effort among them -- is startup-only, and
  a saved-but-not-yet-applied change reported as done is a change the user thinks they have. A rejected
  key says where to put it instead: an unknown `[section].key` names the section that *does* declare
  that key (`[assistant.generation].thinking` is answered with `[assistant].thinking`) and lists what
  the section it was given accepts, so the assistant can correct itself from the error alone. An
  `[agents.<name>]` table gets both hints as well, read off the one set of entries that serves every
  agent, and a hint pointing *into* an agent's table says `[agents.<name>]` rather than the wildcard the
  schema is keyed by, because a model follows a hint literally and the wildcard written back would
  create an agent named `*`. The tool refuses that section outright as well, along with every other
  agent name a section header could not carry as written (an empty one, a spaced one): those are quoted
  on the way into the file, so the table written would not be the section the tool just reported. The
  rule is TOML's bare-key character set, so `report-writer` and `stock_trader` still write. A
  `config.toml` that is not valid TOML is answered the same way, as a
  refusal naming the file and the syntax fault: both parsers the tool sits over (tomlkit for the write
  itself, `tomllib` for the re-read an agent write is checked against) raise a `ConfigError` on a syntax
  error, so a stray bracket from a hand-edit made while Kokua runs is something the assistant can report
  rather than an exception out of the tool call. A model
  string gets a stronger check than the schema's `str`: `[assistant].model` is startup-only, so nothing
  applies it live and a name that does not resolve would be saved and surface as a Kokua that will not
  start. It is refused at write time by building a throwaway client -- the same call startup makes, so
  it also catches a provider whose extra is not installed. A
  list of locked patterns, `[security].locked_config_keys`, ships covering `[security] confirm_tools`,
  `[email] to`, `[paths] data_dir`, and the whole `[agents.*]` section, matched by section prefix since
  agent names cannot be enumerated ahead of time. Each is refused by the tool by default, and changeable
  only by hand-editing the list itself: `update_config` is a tool the assistant holds, so a writable
  agent table would let it widen its own reach. `update_config` is also in the default `confirm_tools`
  list, so each write it *is* allowed goes through the approval prompt.
- The `update_config` write policy is now yours to set. `[security].locked_config_keys` holds the
  patterns the assistant may not write, defaulting to what was previously hardcoded. The key is itself
  always locked, so the assistant cannot unlock itself in one call. A pattern that could never match
  anything fails startup rather than reading as a lock you do not have. Two checks decide that: the
  shape (no dot, whitespace at either end, an empty segment, a `*` sharing a segment with other
  characters, or a `*` anywhere but the last one), and then the vocabulary, read off the sections and
  keys this install really has, which is what refuses `agnets.*`, `Agents.*`, and
  `security.confirm_tool`. A name only you can invent is deliberately not checked: `agents.<name>` and
  `scheduling.task.<name>` are sections you create, so locking one before it exists stays legal.
  Removing `agents.*` genuinely
  unlocks agent tables: `update_config` can now resolve an agent's keys, and dry-runs `validate_agents`
  before saving so a write that would break the next startup is refused. That dry run reads the file
  rather than the running session, the agent tables, the `[assistant].agent` naming the entry agent, and
  the `[[mcp.server]]` entries alike, so two writes that are each fine alone cannot combine into a config
  the next startup rejects, and a write naming a server `add_mcp_server` connected this session is
  accepted rather than refused as an unknown toolset: the entry is already on file, so the next startup
  registers the name even though the running registry never learned it.
  `read_config` opens with the policy in force, and a refusal names the pattern that matched.
- **A default model, with per-agent overrides.** `[assistant].model` is the model every agent runs on;
  an agent that names its own `[agents.<name>].model` runs on that instead. Resolution is per agent and
  never inherited down the delegation graph, so a delegator that pins a model does not drag its workers
  onto it -- a worker declaring nothing runs on the default like any other undeclared agent. A declared
  model that AIMU cannot resolve fails startup naming the table it came from, rather than surfacing
  later as a failed spawn. Both are read once, at startup: no live client is ever rebound to another
  model, which is why the model is not a runtime setting. `[assistant].thinking` follows the same
  shape for reasoning effort (below).
  `[assistant].model` is optional, and leaving it out is supported rather than merely tolerated: AIMU
  resolves a default from `$AIMU_LANGUAGE_MODEL`, or a probe of the local servers when that is unset,
  which is what lets one `config.toml` be shared across machines serving different models.
  `AssistantConfig.default_model` performs that resolution once per process and `model_for` falls back
  to it, so `model_for` is **total**: it answers with a string whatever the file says, and nothing
  downstream has to reconstruct the default from somewhere lossier. See the endpoint entry below for
  what "somewhere lossier" cost.
- **A default reasoning effort, with per-agent overrides.** `[assistant].thinking` is the effort every
  agent runs at; an agent that names its own `[agents.<name>].thinking` runs at that instead. The values
  are AIMU's own four: unset sends nothing (each model keeps its own behavior), `false` asks the model
  not to reason, `true` reasons at the model's default effort, and `"low"` / `"medium"` / `"high"` request
  a level. Startup-only and not a runtime setting, for the same reason the model is. An unrecognized level
  fails startup naming the table, since AIMU would otherwise raise on it only once a request was built,
  and `"xhigh"` is a plausible typo (it is Qwen's own effort ceiling).
  Resolution tests "is it declared" rather than "is it truthy", which the model can afford and this
  cannot: `thinking = false` is a declaration that has to be able to override a `"high"` default. And
  unlike the model, the *resolved* value is written into every sub-agent spec rather than only a declared
  one -- AIMU falls a missing spec `model` back to the spawn tool's own, but a missing spec `thinking` is
  simply `None`, so an undeclared worker would skip the default rather than inherit it. The `/plan`
  workflow's independent reviewers run at the `[assistant]` effort too, being no agent's table.
  Two caveats it is worth knowing: a level is advisory on a model whose card declares no effort-level
  support (AIMU warns once and reasoning is still on), and `false` additionally selects a card's
  instruct-mode sampling profile where it declares one -- only the Qwen 3.5/3.6/3.8 cards today. A
  generation parameter set below still applies over that profile; it is only a parameter nobody set that
  the profile switch decides outright.
  Needs `aimu>=0.17.0`, which added the `agent_types` spec key the per-worker half rides on. An AIMU that
  predates it ignores an unknown spec key silently, so a per-worker effort would simply not apply with
  nothing raised; that release also closes a spec's keys to a published `SUBAGENT_SPEC_KEYS`, which is
  what the startup preflight now probes, since the key itself is a dict entry no probe can see.
  A turn may also ask for its own effort, which beats both tiers for that one turn: the web composer's
  Think picker and the CLI's `/think`. The request rides `ChannelMessage.metadata`, so it is a property of
  the message rather than a setting anything stores, and it reaches the entry agent's own run alone: a
  spawned worker keeps its table's effort, and a workflow turn ignores the request rather than applying it
  to agents the user did not choose for. What the turn actually ran at is what `metadata.thinking` records.
- **A default tier of generation parameters, with per-key overrides.** `[assistant.generation]` sets
  `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `repetition_penalty`, `max_tokens`, and
  `context_length` for every agent; an agent's own `[agents.<name>.generation]` overrides it **per key**,
  so naming only `temperature` still inherits the default's `context_length` rather than replacing the
  whole table. `AssistantConfig.generation_for(name)` is the single resolution, per agent and never
  inherited down the delegation graph, and it is empty by default -- the normal case, since a key this
  never sets stays absent from the request and a model card's own tuned sampling profile is what answers
  instead. The result reaches three places: the entry agent's client (`build_model_client`), each
  spawned worker's spec (the *resolved* value, like `thinking`, so an undeclared worker still inherits
  the default rather than skipping it), and the two planning reviewers, which get the `[assistant]` tier
  only, being no agent's own table. Startup-only and not a runtime setting, like the model and the effort, and
  `[assistant.generation]` is the first dotted sub-table `config/file.py`'s section loader handles, which
  is what lets a schema entry per parameter serve it instead of the flat-key loop reading it as one
  `[assistant]` key holding a table.
  Two things worth knowing about the parameters themselves: `max_tokens` caps *generated* tokens while
  `context_length` sizes the whole window prompt and output share, so a 32768 window with a 4096 cap
  leaves roughly 28k for the system prompt, the tool block, and history -- and AIMU's own weakest tier
  sets `max_tokens = 1024` on Anthropic, the OpenAI-compatible family, and llama.cpp, which is low for a
  turn carrying tool results. A parameter a backend cannot take is dropped with a warning naming the
  remedy: Ollama's SDK has no `min_p`, the Anthropic API has no penalties, and only Ollama's native API
  sizes the context window per request. That warning is written to the rotating file log
  (`data/logs/kokua.log` under your Kokua home) and nowhere else -- not the chat, not the terminal, and
  not `/diag`, which reports what you declared rather than what the backend accepted -- so the log is
  where to look when a parameter you set has no effect.
  Needs `aimu>=0.18.0` for the per-agent half, which added the `generate_kwargs` key to the `agent_types`
  spec; an AIMU that predates it ignores the unknown key silently, so 0.18.0 replaces the 0.17.0 floor
  above rather than sitting alongside it. This is a breaking change.
  **That AIMU bump also changes how four Ollama models sample, whether or not you set anything here.**
  It renames the portable `repetition_penalty` into Ollama's own `repeat_penalty`, so the
  `repetition_penalty = 1.0` that the `qwen3.8:27b`, `qwen3.6:35b`, `qwen3.6:27b`, and `qwen3.5:9b` cards
  have always carried now reaches the wire instead of being discarded by the SDK, and Ollama's own server
  default of 1.1 no longer applies: the repetition penalty is effectively off for those four. It is what
  each card asks for, but it is a real change in their output. The example config's `ollama:qwen3:8b` is
  not one of them -- that card declares no sampling profile at all, so nothing about it changes.
- **`/diag` names the models, the reasoning effort, and the generation parameters in play**: the entry
  agent's first, then each agent that declares its own. None is a runtime setting and all are read only
  at startup, so this is where a running session says what they are. With nothing declared, the model line
  reports what AIMU resolved onto the live client, and the thinking and generation lines are omitted
  rather than reading "unset" on every `/diag`; an agent that overrides the default generation tier shows
  only the keys it declares, not the merged result, since what a table declares is what a reader checks
  against the file.
- **A stored conversation says which model produced its output, and how hard it thought.** Each turn
  records the model that answered it under `metadata.model.<user_index>` and its reasoning effort under
  `metadata.thinking.<user_index>`, and each sub-agent card carries its own worker's pair, so a
  conversation that spans a config edit -- or a turn whose workers ran on different models at different
  efforts -- stays readable from the JSON alone. An effort of `None` is left out (it is the common case);
  `false` is recorded, being a declaration rather than the absence of one. It is metadata, never a message key: AIMU strips only its own
  inert keys before a request, and Ollama and OpenAI-compatible providers forward anything else
  verbatim.
- **Remote and custom model endpoints.** `[assistant] model` accepts AIMU's extended
  `provider:model_id[@base_url][;flags]` form, so Kokua can target a remote OpenAI-compatible server or
  a model id not in AIMU's catalog, e.g.
  `model = "llamaserver:qwen3-8b.gguf@http://gpu-box:8080/v1"`. A worker's `[agents.<name>].model` takes
  the same suffixes, which is worth stating because the two are checked by different code and had drifted:
  a worker's model was parsed with a resolver reading only `provider:model_id`, so pinning a worker to the
  endpoint the assistant itself runs on failed at startup.
  Needs `aimu>=0.20.0`, which is what moved the floor there: earlier AIMU resolved a *spawned* agent's
  model string with that same narrow resolver, so a documented endpoint on `[assistant] model` killed
  every delegation (`Provider 'ollama' has no model id 'qwen3.8:27b@http://gpu-box:11434'`) while the
  entry agent ran on it happily, and a fix would still have dropped the endpoint had it only widened the
  parse: no resolved model enum can carry one, so the sub-agent would have gone on talking to the provider
  default.
- **The endpoint reaches every sub-agent, including when the default comes from the environment.** The
  paragraph above ends on the reason a widened parse would not have been enough, and Kokua had the other
  half of exactly that bug. With `[assistant].model` unset and the default arriving through
  `$AIMU_LANGUAGE_MODEL` (`ollama:qwen3.8:27b@http://gpu-box:11434`), five places recovered the default by
  reading it back off an already-built client, as `config.model or agent.model_client.model`. A client
  reports a resolved `Model` enum, which names a catalogued id and carries nothing else, so the `@base_url`
  was gone before any of them saw it: `spawn_subagent`'s delegate, a nested delegate one level down,
  `compose_subagent`, the `aimu_agents` prebuilt orchestrators, and both `/plan` reviewers were each built
  against the *provider default* while the entry agent talked to the override. Where a local server happened
  to be running it produced answers from the wrong model with nothing reported anywhere; where none was, it
  surfaced as `All connection attempts failed` from a tool call inside a turn that was otherwise working.
  All five now ask `config.default_model`, and none of them may consult a live client. Needs `aimu>=0.21.0`,
  which exports `resolve_default_text_model`: AIMU's public default resolver was the enum-returning twin,
  which by its own docstring cannot represent an endpoint, and the string resolver its documentation told
  callers to use was importable only from `aimu.models._internal`.
  Two things fell out of the fix. `build.model_label` no longer takes a client, so `/diag` and the stored
  per-turn record show `ollama:qwen3.8:27b@http://gpu-box:11434` rather than `OllamaModel.QWEN_3_8_27B` --
  the string you wrote, not the enum it became. And the test suite pins a default of its own
  (`tests/conftest.py`'s `pin_default_model`), because resolving through AIMU reads a `.env` found by
  walking up from the working directory and otherwise probes a local server over HTTP; a developer's real
  `~/devel/.env` did leak into a run before that fixture existed.
- **All state under one directory you own**: `$KOKUA_HOME`, default `~/.kokua`, replacing the example's
  reliance on `aimu.paths.output`. `data/` holds content only -- conversations, memory, documents,
  `images/`, `downloads/`, `logs/`. Images and downloads live in their own folders so the binary files
  never disturb the `DocumentStore`, which scans the documents folder as text. Scheduled tasks are the
  one declared exception: they live in `config.toml` itself, as `[scheduling.task.<name>]` tables, not
  under `data/`.

### MCP

- Remote MCP servers from a startup `--mcp <url>` (repeatable) or `[[mcp.server]]` tables in
  `config.toml`, plus `add_mcp_server` / `remove_mcp_server` at runtime. Servers connect before the
  first agent is built so config-declared servers reach the agents that name them, and a runtime add or
  remove rebuilds each live agent's `spawn_subagent` so its workers pick the server up or drop it
  immediately.
- **Each configured server is a toolset, named by its `name`.** `name` is required: it is how the server
  enters the one namespace an agent declares against, so a server nothing can name reaches nothing. A
  runtime `add_mcp_server`, and the startup `--mcp <url>` flag, both derive a name from the server's host
  and disambiguate it (shared logic) against every other name already claimed -- on file for
  `add_mcp_server`, across the flag's own URLs for `--mcp` -- so neither can produce a config, or a set of
  servers in one run, the registry's collision check would reject. That derived name still reaches no
  agent until a human puts it in an `[agents.*]` table, since that section is locked by default: neither
  can grant itself the capability.
- **Per-server bearer tokens via environment variables.** Each `[[mcp.server]]` takes a required `url`, a
  required `name`, and an optional `token_env` naming the variable holding that server's token, read
  at startup so the secret stays out of `config.toml`. A `token_env` whose variable is unset logs a
  warning and connects tokenless rather than aborting startup.
- **OAuth**: `add_mcp_server` starts the OAuth flow for a server that signals its auth requirement with
  a non-standard response (a 400 plus "missing Authorization header", not only a 401/403). When the flow
  cannot complete because the server lacks dynamic client registration, it returns an actionable "provide
  a bearer token and add the server again" message instead of a raw `OAuthRegistrationError`, and its
  docstring tells the assistant to relay that, ask for a token, and retry with `bearer_token`.
- **The OAuth callback is placeable** through `[mcp].oauth_callback_host` / `oauth_callback_port`.
  FastMCP's defaults (loopback, a fresh random port per process) assume the browser runs on the machine
  the client does. When it does not, the provider redirects the approved browser to *its own* loopback,
  Kokua's listener never hears it, and the connection fails only when the flow times out minutes later,
  with nothing anywhere naming the cause. Pinning the port makes an SSH forward possible while keeping
  the loopback redirect URI that OAuth providers accept; setting the host suits a provider that takes a
  non-loopback one. Pinning also fixes a single-machine case, since the client registration is cached
  across restarts while a random port is not, so a re-authorization in a later process could present a
  redirect URI the provider had on file under the old port. The authorization message now names the
  callback address it will use, because a redirect landing on the wrong machine is otherwise invisible.
- **Boot logs the tools each server contributed, by name.** This is the only record of a remote server's
  tool list: `config.toml` does not know it, `--list-toolsets` runs before any connection so it can only
  name the server, and `add_mcp_server` reports tool names on a runtime add but a boot reconnect reported
  nothing. Without it, an agent that answered from its own knowledge rather than calling a server's tool
  gave you no way to tell whether the tool it needed was absent or merely unused. A server that connects
  and contributes nothing is named as such, since it otherwise looks identical to a healthy one.
- **The auth-challenge log line says what it is.** Every non-bearer server rediscovers its auth mode
  through one rejected unauthenticated request on every boot, because no auth mode is persisted (which
  keeps a stored mode from going stale against a server that changed its mind). That step used to log
  "requires authorization; starting OAuth flow", which reads like the user is about to be prompted, when
  a cached token makes it silent -- misleading enough to send a reader hunting a token-persistence bug
  that was not there. It now reports a rejected unauthenticated request and an attempt with stored
  credentials. An authorization link still reaches the conversation only when no usable token exists, so
  the absence of one is the signal that token reuse is working, and a test pins that the challenge path
  itself posts nothing.
- **No startup warning for a registered name no agent declares.** There was one, covering a configured
  MCP server as well as an installed third-party toolset, and it was dropped: telling a name the user
  provisioned from one that merely ships needed a provenance rule spanning the whole namespace, and a
  toolset nobody declares costs nothing to leave unnamed. The MCP case is the one that lost something
  real, since a configured server connects, spends its token on the handshake, and is then reachable by
  nobody; `docs/how-to/add-mcp-services.md` says so plainly and says what to check instead. The distinct
  warning for a `config.toml` section whose owning toolset no agent declares is unaffected.

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

- **Security policy.** `SECURITY.md` states the reporting channel (GitHub private vulnerability
  reporting), which barriers a report is about (the approval gate, `[security] locked_config_keys`,
  agent capability boundaries, the web front end's download and image routes, the `[email].to`
  recipient lock, and secret disclosure), and which behavior is the product working as documented (a
  model you configured running code you approved).
- **Tool approval.** Configured risky tools require confirmation before each call -- terminal `y/N`, web
  Allow/Deny -- built on AIMU's `ToolApproval` gate. The default set is `add_skill_script`,
  `add_mcp_server`, `execute_python`, `run_command`, and `update_config`; adjust with `[security] confirm_tools` or
  `--confirm-tools` (empty disables). Proactive and backgrounded turns auto-deny gated tools regardless,
  so a full-access tool is never run unattended, and a prompt only ever appears for the conversation you
  are currently viewing. The reply is routed through the single channel reader, so it is safe alongside
  `/stop`. Approval and plan review share one lock-guarded pending slot, so two concurrent requests
  cannot overwrite each other.
- **A gate that names nothing fails startup.** `[security] confirm_tools` matches a tool by name, so an
  entry no configured agent provides gates nothing, and the symptom is a prompt that never comes, which
  nobody notices. Every unmatched entry is now named at startup with its near misses. The vocabulary is
  every tool the config builds, not just the entry agent's: `execute_python` comes from `[agents.coder]`,
  and `spawn_subagent` and a skill's script tools count too. A tool that arrives later (from a runtime
  `add_mcp_server`, or built only by `compose_subagent` out of a toolset no agent names) cannot be listed
  ahead of time, and the error says so.
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

- **An AIMU too old to run Kokua fails with an instruction, not a traceback.** The `aimu>=0.24.0`
  requirement covers a normal install, but a development checkout installs the sibling `../aimu`
  editable and that checkout can sit on an older commit. `kokua.aimu_compat` preflights both the version
  floor and one capability probe -- the version string of an editable install says what its branch
  claims, not what its code contains -- and names both fixes: update the sibling, or
  `uv sync --no-sources` to take AIMU from PyPI instead. The probe tracks the newest surface Kokua leans
  on and takes that surface's shape, which is why it has moved several times and has been a
  set-membership check, a plain name lookup (the `resolve_default_text_model` export, the shape to hope
  for: the capability *is* the exported name), and a signature check -- `SkillManager(include=...)`, then
  `SkillAgent(script_env=...)`, then `aio.WebChannel(stream_thinking=...)`, the rename that carried
  the default flip Kokua relies on for reasoning and tool calls to reach a front end at all. Today it is
  a plain name lookup again, on `aimu.tools.builtin.make_command_tool`, the factory behind `[compute]
  command_env_passthrough`: the capability and its handle are the same object, the shape to hope for.
  It covers one surface at a time by design; every earlier release's capabilities are the floor's job.
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
  tearing down the WebSocket. Any failure between taking the web front end's single-connection guard and
  finishing with it releases that guard, so a later connection is not wrongly refused as "busy in another
  tab" -- a diagnosis that would name the one thing that was not wrong.
- **An `[agents.*]` table that no longer resolves stops the web front end at startup.** The web front
  end builds its assistant per connection, so an unknown toolset name (a toolset renamed, or moved out
  to a skill that is not installed) used to surface only as a WebSocket that closed: the server came up,
  served the page, and refused every connection, leaving the browser able to say nothing but
  "Disconnected." `build_app` now validates the agent tables while building the app, so `kokua
  --frontend web` and `kokua-web` both print the offending name and exit 2, which is what the CLI front
  end already did. A `ConfigError` reaching a connection anyway -- the registry is rebuilt per
  connection, so a skill deleted from disk under a running server is one -- is shown in the chat, like a
  model-client failure.
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

- `src/kokua/` is grouped by subsystem -- `core/`, `config/`, `workflows/`, `mcp/`, `scheduling/`,
  `channels/`, `frontends/`, `toolsets/` -- and `tests/` mirrors it exactly.
- `Assistant` is a composition root and the serve loop; it delegates to `ConversationBook` (store +
  agent cache + active pointer), `TurnRunner`, `HumanGate`, `SettingsApplier`, and `ChannelUI`.
  `ChannelUI` is the one adapter that probes each optional channel capability once and gives every rich
  frame a documented fallback, so there is no `isinstance(channel, WebChannel)` in `core/` or
  `workflows/`. `channels/protocol.py` declares the rich surface for documentation and typing.
- **Why Kokua exists**, and the six principles that serve it, are in
  [docs/explanation/design-principles.md](docs/explanation/design-principles.md): the project's purpose
  is that people can learn how agentic systems work by reading, running, and extending a real one, and
  each principle carries the code that backs it. The architecture narrative is in
  [docs/explanation/architecture.md](docs/explanation/architecture.md).
- Task-oriented guides for the three ways to add capability are in
  [docs/how-to/](docs/how-to/index.md): [set up a toolset](docs/how-to/set-up-toolsets.md) (the namespace,
  declaring an agent, writing a toolset), [add a skill](docs/how-to/add-skills.md), and
  [add an MCP service](docs/how-to/add-mcp-services.md). All three converge on the same rule: a capability
  is declared, never defaulted, and nothing reaches an agent until an `[agents.*]` table names it,
  composing a worker for one task included, since that takes declaring `capabilities`.
- **A mock-only default test suite.** No model, no network, no keys. This is why the model client is
  injectable and the builders are free functions. Client-side page JS is
  covered by an opt-in Playwright suite (`pytest -m e2e`) driving the real `index.html` in headless
  Chromium; it skips rather than errors when the browser or the `web` extra is absent, and it does not
  gate the default suite.
- CI lints, runs the suite on Python 3.11-3.13, and separately builds the sdist and wheel, checks their
  metadata, and installs the wheel into a clean environment to run both console scripts -- an editable
  install imports straight from `src/`, so it cannot catch a missing package-data entry or a console
  script pointing at a moved function.
