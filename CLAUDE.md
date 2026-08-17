# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --all-extras                     # install; pulls the editable sibling ../aimu (see below)
uv run pytest -q                         # full test suite (mock-only: no model, network, or keys; e2e deselected)
uv run pytest tests/frontends/test_web.py -q             # one module (tests/ mirrors src/kokua/)
uv run pytest -m e2e                      # opt-in browser UI tests (needs `playwright install chromium`)
uv run ruff check . && uv run ruff format --check .      # lint (format with `ruff format .`)
uv run kokua --frontend web              # run the web UI (or `kokua-web`); `kokua` alone is the CLI
uv run kokua config init                 # scaffold $KOKUA_HOME/config.toml from the documented example
uv run kokua skills list                 # skills bundled in ./skills (outside the package, not in the wheel)
uv run kokua skills install [name...]    # copy them into $KOKUA_HOME/data/skills
uv run --with build python -m build      # build sdist + wheel (what CI's `package` job verifies)
```

Line length is 120 (configured in `pyproject.toml`). Run lint + tests before committing; update
`CHANGELOG.md` and `README.md` when you change behavior or the public surface.

## AIMU dependency (important)

Kokua is built on the [AIMU](https://github.com/saxman/aimu) library and requires `aimu>=0.14.2`. That
floor is the requirement that ships in the wheel. Separately, `[tool.uv.sources]` points AIMU at
`{ path = "../aimu", editable = true }`, so `uv sync` here installs the sibling checkout live: the two
projects are developed together and architectural changes move code across the boundary.

Consequences for working in this repo:

- **The version floor does not constrain your sibling checkout.** uv installs a path source without
  checking it against the specifier (a declared `aimu>=0.99.0` installs a 0.13.1 sibling and locks it
  without complaint), so `>=0.14.2` governs an installed Kokua and nothing about your working copy.
  Do not read the pin as a guarantee about the AIMU you are running.
- **So a sibling on an older branch is the failure mode to expect, and the startup preflight is what
  catches it.** `kokua.aimu_compat` checks the version floor plus one capability probe, and prints the
  fix. Both halves earn their place: the probe catches a checkout whose declared version already reads
  new enough while the code behind it does not (an editable install's version says what its branch
  claims), and the floor catches the capabilities that are not importable symbols -- AIMU 0.13.1 added
  the tool result to its web `tool` frame, which no `getattr` can detect and which would otherwise
  degrade silently to tool cards with no output. The probe is currently a name lookup for
  `aimu.agents.TruncatedTurnError`, the newest surface Kokua depends on: without it the agent loop
  answers a turn the model had no room to finish by nudging it, which is how a scheduled task produced
  rounds of continuation prompts and no work. It takes whatever shape the surface has, and a *signature*
  check when that surface is a keyword argument no `getattr` would notice (as `SkillManager(include=...)`
  was, before this). If you add a Kokua feature needing a newer AIMU, raise `MINIMUM_AIMU` and the
  `pyproject.toml` floor in the same commit, and move the probe to whatever the new surface is.
- **Without `../aimu`** (CI, a fresh clone, or just running Kokua), `uv sync --no-sources` resolves AIMU
  from PyPI. Nothing in `pyproject.toml` needs editing for that any more.
- **Both console scripts route through `kokua.cli`** so they share that preflight. `kokua-web` is
  `cli:main_web`, not `frontends.web:main`, because importing the web front end pulls in the AIMU surface
  the preflight checks.

## Design principles

Six principles decide what belongs in this repository. Check a proposed change against them; a change
that serves none is probably a plugin, not a core change. Full rationale, with the code that backs
each claim, is in [docs/explanation/design-principles.md](docs/explanation/design-principles.md).

1. **A small, transport-agnostic core.** The assistant knows a `Channel`, not a terminal or a socket.
   Every optional rich frame degrades once, in `ChannelUI`, to a documented fallback. No
   `isinstance(channel, WebChannel)` in `core/` or `planning/`.
2. **Grow by plugin, not by core change.** Capability arrives as a `FrontEnd` or a `Toolset`. A third
   party's arrives through the `kokua.frontends` / `kokua.toolsets` entry-point groups, and Kokua's own
   front ends and its two plugin toolsets (`aimu_agents`, `image`) register there identically. Kokua's *core* capabilities
   (`config`, `conversations`, `mcp-admin`, `scheduling`, and the AIMU wrappers `memory` / `documents` /
   `skills`) are the same kind of object, resolved through the same registry and named in the same
   namespace -- but they do not arrive by the same route: `build_registry` adds them as their own
   provider sources and never calls `discover_toolsets()`, so do not go looking for them in
   `pyproject.toml`'s entry points.
   Its corollary: **a capability is declared, never defaulted.** An agent holds exactly the toolsets its
   `[agents.<name>].tools` names, plus the delegate a non-empty `delegates_to` earns it; no code path
   grants a capability the table did not declare, and no flag can disagree with one. (One exception worth
   knowing: the entry agent is an `aio.SkillAgent`, so AIMU gives it the skill catalogue,
   `activate_skill`, and each `{skill}__{stem}` script tool whether or not it declares the `skills`
   toolset, which only adds `author_skill` / `add_skill_script`. A spawned worker is a plain `aio.Agent`
   and gets none of it.)
3. **`config.toml` is the single source of settings, and the app writes it.** No parallel store. A
   runtime-mutable setting is one entry in `config/table.py`'s `RUNTIME_SETTINGS`, which drives the
   schema, the sanitizer, the hot-apply set, the live-apply loop, and the persist path at once.
   `[agents.*]` is hand-edit only (locked by section prefix in `config/store.py`'s `is_locked`), because
   `update_config` is a tool the assistant holds and a writable agent table would let it widen its own
   reach.
4. **All state under one directory the user owns.** `$KOKUA_HOME`, default `~/.kokua`. Every leaf
   below `data/` is a derived `AssistantConfig` property, never a new function in `config/paths.py`.
5. **A single user, one process, with concurrency rules written down.** The five turn invariants live
   at the top of `core/turns.py`, each naming the bug it prevents. Update them in the same commit as
   any change to turn concurrency.
6. **Verifiable without a model.** The default suite is mock-only. This is why the model client is
   injectable and the builders are free functions -- not just a testing habit.

Kokua inherits AIMU's six library-level principles on top of these.

## Architecture

Kokua wraps AIMU primitives into a single-user, always-on personal assistant: a small core, with
capability pushed into plugins.

The full narrative -- module-by-module layout, control flow, config layering, images, MCP, planning,
the web front end -- lives in
[docs/explanation/architecture.md](docs/explanation/architecture.md). Keep it current there; this
section is a map, not a second copy.

```
src/kokua/
  cli.py  plugins.py  images.py  logging_setup.py  config.example.toml  web_static/
  core/         assistant (composition root + serve loop), conversations, turns, interaction,
                settings_runtime, diagnostics, build, agent_registry, turn_gate, turn_registry,
                messages, errors, transcripts
  config/       schema, paths, file, store (writes + write policy), table
  planning/     runner (the /plan pipeline), reviewers
  mcp/          servers (connect, attach, add, remove), auth
  scheduling/   recurrence (pure math), registry (the JSON file), tasks (TaskService)
  channels/     ui (ChannelUI), protocol (RichChannel), cli, web
  frontends/    cli, web           -- registered as plugins, exactly like a third party's
  toolsets/     registry (Toolset, select, build_tools), context (LiveState, ToolsetContext),
                agents (build_registry, validate_agents, prompt assembly, delegation),
                builtin (AIMU groups/stores/skills), core (an index over Kokua's four),
                config, conversations, mcp_admin, scheduling -- Kokua's own four,
                example, aimu_agents, image -- plugins, like a third party's
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

Use English punctuation (no em dashes) and inclusive terminology (allowlist/blocklist, primary/replica,
main branch). Keep the core small; prefer a plugin over a core change.

**Agent tools live under `toolsets/`, and only there.** A module that defines an `@aimu.tool` is a
toolset module; nothing else contains one, so `grep -rl '@tool' src/kokua/` should only ever find files
in that one directory. Each of Kokua's own four (`toolsets/config.py`, `conversations.py`,
`mcp_admin.py`, `scheduling.py`) also exports the `TOOLSET` wrapping its factory, indexed in
`toolsets/core.py`, so the capability is declared next to the tools it wraps rather than in a second
list that could drift.

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
reaches `toolsets/` and a real import would close the cycle.

Note the convention is only half the answer: about half the tools the shipped entry agent holds come
from AIMU and are not in this repo at all, which is why
[docs/explanation/architecture.md](docs/explanation/architecture.md#how-an-agents-tools-resolve) carries
the full inventory and `tests/core/test_build.py` pins it as an exact set. That inventory is what
`config.example.toml`'s `[agents.assistant]` declares, not a fixed list in code; add a tool to any
toolset that table names and the test fails until the table in the doc is updated in the same commit.

Docstrings explain *why*, and must stand on their own: no bare task or phase numbers ("Task 6",
"Phase B"), which reference a design history that isn't in the repository. Name the behavior or link
a doc under `docs/`.
