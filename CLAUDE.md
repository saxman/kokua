# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is for

**Kokua exists so people can learn how agentic systems work.** It is a real assistant rather than a
demo, because a toy cannot teach what real work costs, and it is designed to be understood: the
machinery is meant to be followed, not taken on faith. People learn from it three ways, and all three
are load-bearing. They **read it** (a core small enough to hold in your head, each module saying why it
is shaped as it is), they **run it** (reasoning, tool calls, results, sub-agent cards, and plan phases
on by default, so the loop is watched rather than inferred), and they **extend it** (capability arrives
through the same seam Kokua's own capabilities use, so what you read is what you would write). The full
statement, and the six principles that serve it, are in
[docs/explanation/design-principles.md](docs/explanation/design-principles.md).

Three consequences for work in this repository:

- **Docs are part of the change, not a follow-up.** A behavior change that lands without its
  explanation is unfinished. `README.md`, `CHANGELOG.md`, and the relevant page under `docs/` go in the
  same commit as the code. A new page under `docs/` needs a `nav` entry in `mkdocs.yml`, or the strict
  build fails; a link out of `docs/` naming a repository path or a published-site slug that does not
  exist is caught instead by `tests/test_docs.py`, which the build does not check.
- **Between two working designs, take the one a newcomer follows faster.** An abstraction that saves
  lines and costs a reader a jump is a bad trade here even when it is the tidier code.
- **Growth has to teach something.** A change that enlarges what a newcomer must hold in their head
  without demonstrating something new about agentic systems is a plugin, not a core change.

## Commands

```bash
uv sync --all-extras                     # install; pulls the editable sibling ../aimu (see below)
uv run pytest -q                         # full test suite (mock-only: no model, network, or keys; e2e deselected)
uv run pytest tests/frontends/test_web.py -q             # one module (tests/ mirrors src/kokua/)
uv run pytest -m e2e                      # opt-in browser UI tests (needs `playwright install chromium`)
uv run ruff check . && uv run ruff format --check .      # lint (format with `ruff format .`)
uv run mkdocs serve                       # preview the docs site (http://127.0.0.1:8000; same port as
                                          # `kokua --frontend web` below, so don't run both at once)
uv run mkdocs build --strict              # what CI builds; broken link or off-nav page fails
uv run kokua --frontend web              # run the web UI (or `kokua-web`); `kokua` alone is the CLI
uv run kokua config init                 # scaffold $KOKUA_HOME/config.toml from the documented example
uv run kokua skills list                 # skills bundled in ./skills (outside the package, not in the wheel)
uv run kokua skills install [name...]    # copy them into $KOKUA_HOME/data/skills
uv run kokua export [id-or-prefix]       # write a saved conversation to Markdown; --full, -o, -o -
uv run --with build python -m build      # build sdist + wheel (what CI's `package` job verifies)
```

Line length is 120 (configured in `pyproject.toml`). Run lint + tests before committing; update
`CHANGELOG.md` and `README.md` when you change behavior or the public surface.

## AIMU dependency (important)

Kokua is built on the [AIMU](https://github.com/saxman/aimu) library and requires `aimu>=0.28.0`. That
floor is the requirement that ships in the wheel. Separately, `[tool.uv.sources]` points AIMU at
`{ path = "../aimu", editable = true }`, so `uv sync` here installs the sibling checkout live: the two
projects are developed together and architectural changes move code across the boundary.

Consequences for working in this repo:

- **The version floor does not constrain your sibling checkout.** uv installs a path source without
  checking it against the specifier (a declared `aimu>=0.99.0` installs a 0.13.1 sibling and locks it
  without complaint), so `>=0.28.0` governs an installed Kokua and nothing about your working copy.
  Do not read the pin as a guarantee about the AIMU you are running.
- **So a sibling on an older branch is the failure mode to expect, and the startup preflight is what
  catches it.** `kokua.aimu_compat` checks the version floor plus one capability probe, and prints the
  fix. Both halves earn their place: the probe catches a checkout whose declared version already reads
  new enough while the code behind it does not (an editable install's version says what its branch
  claims), and the floor catches the capabilities that are not importable symbols -- AIMU 0.13.1 added
  the tool result to its web `tool` frame, which no `getattr` can detect and which would otherwise
  degrade silently to tool cards with no output. AIMU 0.16.0 is another: it made
  `client.default_generate_kwargs` an input starting empty on every provider, where Ollama used to
  report the model card's profile there, which is what let Kokua stop writing that tier itself. AIMU
  0.17.0 is a third, and the sharpest illustration: the capability Kokua depends on is a `"thinking"` key
  it writes into an `agent_types` spec, and an AIMU that predates it *ignores an unknown spec key in
  silence* -- per-worker reasoning effort would simply not apply, with nothing raised anywhere. A dict key
  is invisible to both a name lookup and a signature check, so the floor is the only half that could
  have covered it. What rescued the probe is the other half of that same release: closing a spec's keys
  to a known set, published as `aimu.tools.builtin.SUBAGENT_SPEC_KEYS`, which *is* a symbol and *is* the
  set the depended-on key belongs to. That is what the probe moved to, off `aio.ContextOverflowError`.
  AIMU 0.18.0 then moved it again without moving the symbol: the capability is `generate_kwargs`, a member
  of that same `SUBAGENT_SPEC_KEYS`, which shipped one release earlier for the `thinking` key, so the set's
  mere presence proves nothing and only its contents do -- the same gap a name lookup left for a dict key,
  one level down, inside a set instead of at module scope. AIMU 0.20.0 carries two capabilities Kokua depends on, and
  between them they show both outcomes of the search for a handle. The first is that a *sub-agent* spawned
  with a `provider:model@base_url` string reaches that endpoint: before it, the async spawn path resolved
  the string through a resolver reading only `provider:model_id`, so an endpoint
  [docs/reference/configuration.md](docs/reference/configuration.md#model) documents killed every
  delegation while the entry agent ran on it happily. That fix is two lines inside a private function: no
  symbol, no parameter, no set member, and the one thing that would detect it directly (which resolver the
  function reaches for) is the kind of internal a later honest refactor would change, turning the preflight
  into a wall in front of a *newer, working* AIMU. While it was the newest surface the probe gripped
  `aimu.models.model_client.endpoint_kwargs`, the mapping that fix routes through, and stated the limit out
  loud: the plumbing landed earlier *within* 0.20.0 than the spawn fix riding on it, so a sibling parked
  between those two commits passed and still dropped a sub-agent's endpoint. The second capability closes
  that window by arriving later in the same release with a handle of its own, and the probe has moved to
  it: `aio.SkillAgent(script_env=...)`, the parameter carrying the `[email]` settings and the downloads
  folder into a skill script the entry agent runs, without which those scripts run with the settings simply
  missing and report themselves unconfigured. It landed after both of the commits the endpoint window sat
  between, so passing it then implied the spawn fix too, which was luck rather than a rule. AIMU 0.21.0's
  surface was the easiest one this probe has had: `aimu.models.resolve_default_text_model`,
  the export `AssistantConfig.default_model` calls, where the capability *is* the exported name, so a plain
  name lookup asks exactly the question that matters. Note what is and is not new there. The function is old;
  only its reachability is new, which is precisely why both halves still earn their place: an AIMU predating
  the export has the behavior and no way to ask for it. The capability *behind* that export is the fourth
  kind of case, and worth remembering when you next go looking for a handle: nothing on a live client retains
  the string it was built from, so "which endpoint is this client talking to" cannot be asked at all, and the
  export is the route around that gap rather than a fix for it. AIMU 0.23.0 was the current floor until
  0.25.0, and it is the silent-degradation case in its purest form: AIMU renamed its channel flags `show_thinking` /
  `show_tools` to `stream_thinking` / `stream_tools` and flipped both defaults from `False` to `True`, and
  Kokua retired its own `[display]` settings in the same change and now constructs both channels bare. An
  older AIMU accepts that same bare construction and relays neither reasoning nor tool calls, in a front end
  whose whole claim is that the loop is watched rather than inferred -- and since Kokua no longer reads
  `self.show_thinking` anywhere, not even an `AttributeError` is left to notice. The probe is a signature
  check on `aio.WebChannel.__init__` for `stream_thinking`. What Kokua actually depends on there is the
  *default value*, not the parameter; the name stands in for it because the rename and the flip were one
  change, so a checkout carrying the new name carries the new default. The default is directly inspectable,
  unusually, and the parameter check is still preferred: it dates the checkout to the same release without
  teaching the probe a fourth shape for one case. AIMU 0.25.0 moved the probe
  onto `make_async_subagent_tool`'s new `events` parameter, the handle that lets a spawned sub-agent's
  model turns reach the sink the delegating turn opened; a fresh-client critic
  (`workflows.critics.reviewer_agent`) had the identical gap for the identical reason. (This capability
  was first published as 0.24.0, but that number collided with a different 0.24.0 AIMU's own `main`
  released first, carrying an unrelated `run_command` tool and none of this capability; the branch
  that added `events` rebased past it and renumbered to 0.25.0, so the real, released 0.24.0 fails that
  probe correctly rather than being a gap in it.) A name lookup on the module would not catch either
  absence, because `make_async_subagent_tool` itself predates 0.25.0 and imports fine either way; only
  its parameters changed, so a signature check was what the shape demanded, the fourth time this probe
  has taken it. Unlike `script_env` and `include`, which carried settings *to* an existing capability,
  `events` *is* the capability: nothing else has to be true of a checkout once that one argument exists.
  What it left uncovered is one level down from the parameter's own presence: a checkout carrying
  `events` but not the recursive passthrough (a spawned worker forwarding its own `events` on to a
  grandchild it delegates to in turn) still passes, so a worker spawning its own worker could go
  uncounted without the probe raising anything -- the floor's job now that the surface has moved on.
  **AIMU 0.27.0 was the current floor until 0.28.0, and it is the first one whose reason and whose probe
  are different capabilities from different releases.** Learn that split, because it is the shape of every future case
  where a bug fix rather than a feature moves the floor. The floor moved for *0.26.0*: AIMU's tool loop
  no longer strands an un-dispatched tool call before its forced wrap-up. Before that fix, exhausting
  `max_iterations` on a turn that had requested tools appended the wrap-up's *user* prompt straight on
  top of the unanswered calls, which Anthropic rejects with ``messages.N: `tool_use` ids were found
  without `tool_result` blocks immediately after``. Kokua felt it as sub-agents failing instead of
  answering, search-heavy ones worst, since a run that spends every round calling tools is the one still
  holding a pending call when the cap lands. That fix offers no handle a probe could honestly grip:
  `_settle_pending_tools` is a private method on a private class, exactly the internal a later refactor
  would rename, and gripping it would turn the preflight into a wall in front of a *newer, working* AIMU
  -- the 0.20.0 trap again. So the probe grips *0.27.0*'s `ModelRefusalError` instead, exported from
  `aimu.aio`, and that is a plain name lookup for only the second time (`resolve_default_text_model` was
  the first): the capability *is* the export. Anthropic returns a refusal as HTTP 200 with
  `stop_reason: "refusal"` and no content, so an AIMU that does not raise for it returns an empty string
  an agent loop cannot distinguish from a degenerate turn, and the continuation nudge then burns the
  run's iterations getting refused again; `core/turns.py` branches on the class at three sites so a
  declined request reads as declined rather than as a generic failure. 0.27.0's other half is the floor's
  job for 0.26.0's reason: every provider now reports how a turn ended, so `TruncatedTurnError` fires
  outside Ollama for the first time and `client.last_stop_reason` carries the provider's own word for it,
  but that is an attribute on a live client rather than a module symbol and Kokua reads it nowhere
  directly. The probe therefore covers exactly one surface at a time, in whatever shape that surface has,
  and it has taken three: a name lookup for a symbol (`resolve_default_text_model` first,
  `ModelRefusalError` second), a *signature* check for a keyword argument no `getattr` would notice
  (`SkillManager(include=...)` first, `script_env` second, `stream_thinking` third, `events` fourth), and
  a membership check for an entry in a published set, today's shape for the second time (`generate_kwargs`
  first, `max_iterations` now).

  **AIMU 0.28.0 is the current floor, and it is the case where a probe's honest claim is weaker than its
  predecessors' and has to say so.** The capability is the ``"max_iterations"`` entry in
  `SUBAGENT_SPEC_KEYS`, the spec key `core/agents.py` writes for an agent declaring its own tool-loop cap,
  which is what makes `[agents.<name>].max_iterations` expressible at all: AIMU's cap was one value per
  spawn tool, so per-agent could not be said. The probe is a membership check on that set, the second time
  after `generate_kwargs` and for the same reason (the set shipped in 0.17.0, so only its contents date a
  checkout). What is new is the honesty the shape demands. Every recent floor converted *silence* into a
  startup error: an older AIMU accepted `stream_thinking`'s absence, `script_env`'s absence, an unknown
  spec key in 0.17.0, and said nothing. This one cannot, because the key set is now closed and an AIMU
  predating 0.28.0 raises `ValueError` naming the key. The capability fails loudly either way, so what the
  probe buys is *timing*: a failure at the first delegation, which may be a long way into a session,
  becomes a startup message carrying the fix. That is a real gain and a smaller one, and writing it down as
  such is the point, because a probe that overclaims is worse than one that covers less. What it leaves
  uncovered is the other half of the same feature: the tool-level `max_iterations` argument on both spawn
  factories is old, so an AIMU with the set entry but broken `_build_agent` wiring would pass the probe and
  still cap every worker wrong. The floor's job, as always.

  What the current surface says nothing about, only the floor covers. `tests/test_aimu_compat.py` pins
  `MINIMUM_AIMU` against `pyproject.toml`'s specifier, since the two are halves of one decision and
  neither can detect the other drifting. If you add
  a Kokua feature needing a newer AIMU, raise `MINIMUM_AIMU` and the `pyproject.toml` floor in the same
  commit, and move the probe to whatever the new surface is. When a release genuinely offers no handle a
  probe can grip, leave the probe where it is and say so in `aimu_compat`'s docstring rather than moving it
  to something it can only pretend to check -- but look for a handle first, because 0.17.0 appeared to be
  that case and was not, and 0.20.0 shows the other outcome: no handle for the capability itself, so the
  probe takes the nearest one on its path and names what that leaves uncovered.
- **Without `../aimu`** (CI, a fresh clone, or just running Kokua), `uv sync --no-sources` resolves AIMU
  from PyPI. Nothing in `pyproject.toml` needs editing for that any more.
- **Both console scripts route through `kokua.cli`** so they share that preflight. `kokua-web` is
  `cli:main_web`, not `frontends.web:main`, because importing the web front end pulls in the AIMU surface
  the preflight checks.

## Design principles

Six principles decide what belongs in this repository, and each of them serves the goal above: 1 and 2
keep Kokua readable, 3 and 4 keep it observable, 5 keeps it runnable by anyone who clones it, and 6
keeps its capability yours to bound.
Check a proposed change against them; a change that serves none is probably a plugin, not a core
change. Full rationale, with the code that backs each claim, is in
[docs/explanation/design-principles.md](docs/explanation/design-principles.md).

1. **A small, transport-agnostic core.** The assistant knows a `Channel`, not a terminal or a socket.
   Every optional rich frame degrades once, in `ChannelUI`, to a documented fallback. No
   `isinstance(channel, WebChannel)` in `core/` or `workflows/`.
2. **Grow by plugin, not by core change.** Capability arrives as a `FrontEnd` or a `Toolset`. A third
   party's arrives through the `kokua.frontends` / `kokua.toolsets` entry-point groups, and **every one of
   the 21 toolsets Kokua ships arrives the same way**, listed in `pyproject.toml`'s
   `[project.entry-points."kokua.toolsets"]` table beside where a third party's entry would go. There is no
   second route and no index in code: that table *is* the index, and
   `tests/toolsets/test_registration.py` pins it against `src/kokua/toolsets/` in both directions so a
   module nobody registered, or an entry naming no module, fails the suite instead of producing a toolset
   that silently does not exist. Each entry key must equal its module name and its `TOOLSET.name`
   (`register` keys on the last of those, and the key feeds only the provenance label, so a mismatch is
   otherwise silent). A `Toolset` may also carry a `Workflow`, a named turn strategy: an agent gets the
   workflow's `/`-command exactly the way it gets the toolset's tools, by naming the toolset in
   `[agents.<name>].tools`. `planning` is the one toolset that does that and contributes no tools at all.
   Its corollary: **a capability is declared, never defaulted.** An agent holds exactly the toolsets its
   `[agents.<name>].tools` names, plus the delegate a non-empty `delegates_to` earns it; no code path
   grants a capability the table did not declare, and no flag can disagree with one. `compose_subagent` is
   the one code path that draws from the whole registry instead of a table, and it is still entered by
   declaration: only an agent whose own table names `capabilities` holds it at all. What the rule protects
   is a *persistent* agent's reach, and a sub-agent composed for one task is not an agent the config
   describes -- built per call, discarded with the call. (Another exception worth knowing: the entry
   agent is an `aio.SkillAgent`, so AIMU gives it the skill catalogue, `activate_skill`, and each
   `{skill}__{stem}` script tool whether or not it declares the `skills` toolset, which only adds
   `author_skill` / `add_skill_script`. A spawned worker is a plain `aio.Agent` and gets none of it.)
3. **`config.toml` is the single source of settings, and the app writes it.** No parallel store. Kokua's
   own runtime-mutable settings are one entry each in `config/table.py`'s `CORE_RUNTIME_SETTINGS`; a
   toolset's are one `Setting` on the toolset itself, in its own `[<name>]` section. `SettingsTable`,
   built at startup from both, is what still drives the schema, the sanitizer, the hot-apply set, the
   live-apply loop, and the persist path from one place. `CORE_RUNTIME_SETTINGS` holds exactly **one**
   entry, `[assistant].generate_titles`, and an addition has to answer both of the questions it did: does
   the *core* read it (everything a capability reads is that capability's declaration, which is where the
   `[planning]` flags live), and does a change apply without a restart? `generate_titles` passes because
   `Assistant._spawn_title` is its only reader and reads it per conversation; `[assistant].model` passes
   the first and fails the second, which is why it is startup-only. The entries before it were
   `[display]`'s `show_thinking` / `show_tools`, retired along with `RuntimeSetting.mirror_on_channel` and
   `ChannelUI.display_flag` once what reaches a channel stopped being a user setting: a channel now relays
   reasoning and tool calls unconditionally (AIMU's `stream_thinking` / `stream_tools`, both defaulting
   on), and what to *show* is the front end's call to make with the frame in hand. `[agents.*]` is locked by default (matched by
   `config/store.py`'s `locked_by(section, key, patterns)`, which answers with the pattern that refused
   the write), because `update_config` is a tool the assistant holds and a writable agent table would
   let it widen its own reach; granting it that table takes a hand-edit removing the pattern from
   `[security].locked_config_keys`. `[scheduling.task.*]` is locked by prefix too, but for routing
   rather than capability: the assistant may change any task, only through the scheduling tools, since a
   bare `update_config` write would skip the scheduler (un)arming a task write has to be paired with.
4. **All state under one directory the user owns.** `$KOKUA_HOME`, default `~/.kokua`. Every leaf
   below `data/` is a derived `AssistantConfig` property, never a new function in `config/paths.py`.
   Declared scheduled tasks are the one stated exception, living in `config.toml` rather than under
   `data/`, because a task is a declaration a user should be able to write and comment.
5. **A single user, one process, with concurrency rules written down.** The seven turn invariants live
   at the top of `core/turns.py`, each naming the bug it prevents. Update them in the same commit as
   any change to turn concurrency.
6. **Security is explicit and user controlled.** Capability stays real; a control is added beside it
   and the control is yours. Every security control is a value in `config.toml` (`[security]
   confirm_tools`, `[security] locked_config_keys`, an agent's own `tools` list), never a constant in
   the source. A control that would do nothing, a gate naming no real tool or a lock pattern matching
   no real key, is a hard startup error rather than a silent no-op, because nobody notices a prompt
   that never comes. You may loosen as well as tighten; only the key holding the lock list is
   unconditional.

Kokua inherits AIMU's six library-level principles on top of these.

## Architecture

Kokua wraps AIMU primitives into a single-user, always-on personal assistant: a small core, with
capability pushed into plugins.

The full narrative -- module-by-module layout, control flow, config layering, images, MCP, planning,
the web front end -- lives in
[docs/explanation/architecture.md](docs/explanation/architecture.md). Keep it current there; this
section is a map, not a second copy. Every `config.toml` key is documented in
[docs/reference/configuration.md](docs/reference/configuration.md); `config.example.toml` carries only a
line per key, because `read_config` hands the assistant the scaffolded file and its comments sit in the
model's context on every configuration question. A new or changed key goes in both.

```
src/kokua/
  cli.py  plugins.py  images.py  logging_setup.py  transcript_export.py  config.example.toml  web_static/
  core/         assistant (composition root + serve loop), conversations, turns, interaction,
                settings_runtime, diagnostics, build, agents (build_registry, validate_agents, prompt
                assembly, delegation), agent_registry, turn_gate, turn_registry, messages, titles,
                errors, transcripts, metrics (what a turn cost, accumulated from AIMU's run events)
  config/       schema, paths, file, store (writes + write policy), table, settings_sources (joins a
                toolset's declared settings into the table; the one module under config/ that imports
                upward, so the rest of the layer stays at the bottom)
  workflows/    protocol (Workflow, WorkflowContext, WorkflowResult, the two tiers), critics
                (the shared independent reviewer), planning/ (the /plan workflow)
  mcp/          servers (connect, attach, add, remove), auth
  scheduling/   recurrence (pure math), tasks (TaskService, over config.toml's [scheduling.task.*])
  channels/     ui (ChannelUI), protocol (RichChannel), cli, web
  frontends/    cli, web           -- registered as plugins, exactly like a third party's
  registry/     registry (Toolset, Setting, select, build_tools, register), context (LiveState,
                ToolsetContext) -- the machinery, and no toolsets
  toolsets/     audio, compute, documents, fs, memory, misc, skills, speech, time, transcription, web
                -- wrappers over AIMU's groups and stores,
                capabilities, config, conversations, mcp, scheduling -- one Kokua subsystem each,
                planning -- a Workflow and no tools, aimu_agents, benchmark, github_backup, image --
                Kokua's own, needing only the config. One file per toolset, named for the toolset, each
                listed in pyproject.toml's kokua.toolsets table, and nothing else in the directory
```

`tests/` mirrors this layout. Public import surface: `kokua.plugins`, `kokua.config`, `kokua.core`,
`kokua.channels.web`, `kokua.images`. Everything else is internal.

Points worth knowing before changing the core:

- **`Assistant` delegates.** It is the composition root and the serve loop; conversations go to
  `ConversationBook`, turns to `TurnRunner`, human decisions to `HumanGate`, settings to
  `SettingsApplier`, the channel to `ChannelUI`.
- **A reactive turn runs as a background `aio.RunHandle`,** so the channel keeps reading during it.
  That is what lets `/stop` cancel an in-flight reply and a web approval reply reach the waiting tool
  call.
- **Switching conversations does *not* cancel the running turn.** Each conversation owns its agent and
  client; a backgrounded turn persists to its own conversation, streams muted, and notifies on
  completion. Only `delete_conversation` cancels, and only its own turn.
- **Turn concurrency has written invariants** at the top of `core/turns.py`. Read them first.

## Testing notes

Tests are mock-only: no model, no network, no keys. `tests/helpers.py` provides
`MockAsyncModelClient`; `tests/channels.py` and `tests/fakes.py` hold the shared channel and
MCP/client doubles; `tests/conftest.py` redirects `KOKUA_HOME` to a temp dir. The mock **fakes
tool-call rounds** rather than running AIMU's real dispatch, so features that hook dispatch (the
tool-approval gate) are tested by calling `agent._prepare_run()` then
`agent.model_client._handle_tool_calls([...])` directly.

`tests/` mirrors `src/kokua/`; a test goes where its module does. Client-side page JS is covered by
an **opt-in** end-to-end suite (`tests/frontends/test_web_e2e.py`, marked `e2e`, deselected by
default) driving the real `index.html` in headless Chromium. Run it with `uv run pytest -m e2e`
(needs the `web` extra + `uv run playwright install chromium`); it skips rather than errors when
those are absent, and it does not gate the default suite. Behaviors it doesn't cover still warrant a
manual browser check.

## Conventions

**No em dashes.** Not in prose, docstrings, comments, commit messages, or user-facing strings. Recast
the sentence with a comma, a colon, a semicolon, or parentheses; do not substitute a `--` for the dash
you wanted. (Existing `--` in the docs predates this rule and is fine to leave in place; convert it
when you are editing that text anyway.) Use inclusive terminology as well (allowlist/blocklist,
primary/replica, main branch). Keep the core small; prefer a plugin over a core change.

**Agent tools live under `toolsets/`, and only there, one file per toolset named for the toolset.** A
module that defines an `@aimu.tool` is a toolset module; nothing else contains one, so
`grep -rl '@tool' src/kokua/` should only ever find files in that one directory. Each toolset module
exports a module-level `TOOLSET`, so a capability is declared next to the tools it wraps, and each has one
line in `pyproject.toml`'s `kokua.toolsets` table, which is the only index: adding a toolset means a new
file and that one line, and `tests/toolsets/test_registration.py` fails until both exist and agree.
Nothing else lives in that directory -- the registry machinery is `registry/`, and namespace assembly is
`core/agents.py`. `toolsets/planning.py` is the one toolset carrying a `Workflow` instead of tools.
`workflows/` itself defines no tools either -- not even `workflows/critics.py`, which *mounts* AIMU's tool
callables (`REVIEWER_TOOLS`) for its reviewer agent to call, without itself declaring an `@aimu.tool`.

**A subsystem holds logic, not presentation.** `core/`, `config/`, `mcp/`, and `scheduling/` contain
only what agents *and* front ends need. They return records and raise typed errors
(`TaskNotFound`, `ConnectFailed`, `SettingLocked`); they never format a sentence, never shape a
signature to fit a tool schema, and never import `aimu.tools`. The toolset module wrapping one owns its
tool schemas, its model-facing docstrings, and every string a reader sees. Where the two readers want
different words, each renders its own: a task with no countdown is a `status` from `TaskService`,
"disabled" to the model, and "disabled" in the sidebar because app.js says so, not because the service
did. The flat scalar arguments of `schedule_task` are the clearest case of a signature that belongs to
the tool surface rather than the domain, which is why `_build_schedule` lives in `toolsets/scheduling.py`.

Two dependency rules fall out of this and are worth stating: `config/` is the bottom layer and imports
nothing above it (which is why `name_from_url` lives in `config/store.py`, not `mcp/servers.py`), and a
`toolsets/` module annotating a `core/` type imports it under `TYPE_CHECKING`, since `core/build.py`
reaches `toolsets/` and a real import would close the cycle. `config/settings_sources.py` is the one
exception to the first rule: it reaches `toolsets.core` to collect what the installed toolsets declared,
and does so inside the function that needs it rather than at module scope, because `toolsets/config.py`
imports `settings_sources` at module level to build its cold-key schema -- hoisting the upward import
would close that loop and break `import kokua.toolsets.core` on a partially-initialized module.

Note the convention is only part of the answer: twelve of the 31 tools the shipped entry agent holds
come from AIMU and are not in this repo at all, which is why
[docs/explanation/architecture.md](docs/explanation/architecture.md#how-an-agents-tools-resolve) carries
the full inventory and `tests/core/test_build.py` pins it as an exact set. That inventory is what
`config.example.toml`'s `[agents.assistant]` declares, not a fixed list in code; add a tool to any
toolset that table names and the test fails until the table in the doc is updated in the same commit.

Docstrings explain *why*, and must stand on their own: no bare task or phase numbers ("Task 6",
"Phase B"), which reference a design history that isn't in the repository. Name the behavior or link
a doc under `docs/`.
