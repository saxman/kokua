# Design principles

Kokua is an *application*, not a library. It is built on [AIMU](https://saxman.info/aimu/) and
inherits [AIMU's six library-level principles](https://saxman.info/aimu/explanation/design-principles/)
wholesale: plain Python, plain data, uniform interfaces, progressive disclosure, direct paths, loud
failures. What follows are the six that are Kokua's own, the ones that decide what belongs in this
repository at all.

They are ordered from foundation to consequence: read top to bottom and the shape of the app falls
out.

## 1. A small, transport-agnostic core

The assistant knows a `Channel`, not a terminal and not a socket. Every transport-specific behaviour
lives on the channel or degrades, once and visibly, to plain text. A new front end implements AIMU's
`Channel` and works immediately; implementing more of the rich-frame surface makes it nicer, never
functional-versus-broken.

*How this cashes out:* `Assistant` takes a `Channel`, and [frontends/cli.py](../../src/kokua/frontends/cli.py)
and [frontends/web.py](../../src/kokua/frontends/web.py) share it unchanged.
[`ChannelUI`](../../src/kokua/channels/ui.py) probes each optional frame once at construction and
resolves it to exactly one documented fallback, so no caller asks `getattr(channel, "send_x", None)`
for itself. [`channels/protocol.py`](../../src/kokua/channels/protocol.py) declares the rich surface
for documentation and typing, and nothing does `isinstance` against it. There is no
`isinstance(channel, WebChannel)` anywhere in `core/` or `planning/`. Where a capability changes what
the core *does* rather than how it renders, it is a named boolean -- `supports_conversations`,
`supports_phases`, `supports_streamed_activity` -- not an inline `getattr`.

## 2. Grow by plugin, not by core change

Capability arrives as an entry-point-registered `FrontEnd` or `Toolset`, from any installed package.
Kokua's own front ends and toolsets register exactly the way a third party's would; if the built-in
path and the plugin path ever diverge, the plugin path is the broken one.

*How this cashes out:* `pyproject.toml`'s `kokua.frontends` and `kokua.toolsets` groups list
`kokua.frontends.web:FRONTEND` and `kokua.toolsets.image:TOOLSET` in the same table a third party's
entry would go in. [`plugins.py`](../../src/kokua/plugins.py) is the only loader. A plugin toolset that
raises in `build()` is logged and skipped, so one bad plugin cannot stop startup. `plugins` imports the
built-in front ends lazily, and `kokua/__init__.py` exposes `Assistant` through PEP 562, so
`import kokua` never pulls in `aimu.aio` or starlette for a caller that only wanted to list plugins.
[`toolsets/image.py`](../../src/kokua/toolsets/image.py) exists as the template.

This now reaches Kokua's *own* capabilities, not just third-party ones. `config`, `conversations`,
`mcp-admin`, and `scheduling` are each a `Toolset` declared in a `toolsets/` module that wraps one
subsystem's logic as agent tools, indexed in [`toolsets/core.py`](../../src/kokua/toolsets/core.py);
memory, documents, and
skills are toolsets wrapping AIMU's own factories in
[`toolsets/builtin.py`](../../src/kokua/toolsets/builtin.py). All of them land in the same registry
namespace a plugin does. `kokua --list-toolsets` prints that namespace grouped by provider, and the
grouping is the only way to tell a built-in from a plugin from the outside: an agent's `tools` list names
`"scheduling"`, `"image"` and a skill's own name identically, so a capability can change provider without
touching an agent.
Inside, one asymmetry remains and is deliberate: a plugin's `build` is wrapped so a raised exception is
logged and yields no tools, while a core or AIMU toolset failing to build is a bug in this repository and
must be loud.

### Corollary: a capability is declared, never defaulted

An agent's capability is exactly what its `[agents.<name>].tools` table declares. **No code path adds a
tool an agent did not name, and no flag can disagree with a declaration.**

*How this cashes out:* `[assistant].memory` and the `--memory` / `--tools` flags were removed rather
than kept alongside the `memory` and `documents` toolsets, because a second switch for one capability
means one of the two is lying. `time` is a toolset every agent that wants a clock declares, where it
used to be added to every agent in code. The shared state a toolset draws on is a lazy property on
[`LiveState`](../../src/kokua/toolsets/context.py), so the memory and document stores are opened because
some agent declared the toolset that needs them, and not otherwise. An unknown name raises rather than being
dropped ([`toolsets/registry.py`](../../src/kokua/toolsets/registry.py)'s `select`), since a dropped name
is a declaration the code silently overruled.

**`cross_cutting` is not an authorization boundary,** and reading it as one would be a false security
conclusion. It decides exactly one sentence of prompt guidance (whether an agent is told it is a lean
supervisor that must delegate) and nothing else. A worker whose table declares `tools = ["config"]`
really does get `update_config`; a worker declaring `compute` really does get `execute_python`. That is
intentional: filtering a declaration in code is precisely the behavior this design removes. The security
boundary is elsewhere, and it is two things: `[agents.*]` can only be changed by hand-editing
`config.toml`, so a human writing the table *is* the consent, and `[security] confirm_tools` gates the
dangerous calls at call time no matter which agent makes them (a worker's gated call is routed to the
user for approval, and an unattended turn auto-denies).

`update_config` refusing to touch `[agents.*]` is not the only thing standing between it and being
rewritten by the assistant itself: the entry agent's `add_skill_script` and a `compute` worker's
`execute_python` both have the machine access to overwrite `config.toml` directly, bypassing
`update_config`'s refusal entirely. `confirm_tools` gating both by default is what makes "hand-edit only"
hold in practice rather than only in `update_config`'s own code, and it is why `[security] confirm_tools`
is itself one of the hand-edit-only keys. `config.example.toml` documents `confirm_tools = []` as the way
to turn approval off, so this backstop is a default a user can remove, not a wall.

## 3. `config.toml` is the single source of settings, and the app writes it

One settings file, hand-authorable and app-writable, with its comments preserved across the app's own
writes. No parallel store, no process-only overrides, no settings that exist in memory and nowhere
else.

*How this cashes out:* [`config/store.py`](../../src/kokua/config/store.py) does comment-preserving
`tomlkit` writes; three writers (the web settings panel, `add_mcp_server`, and the assistant's own
`update_config` tool) land in that one file. A `runtime-settings.json` store used to exist and was
retired in favour of `[generation]`. Four things in the file are hand-edit only, refused by
`update_config` even behind the approval prompt: `[security] confirm_tools` (the gate itself),
`[email] to` (the locked recipient), `[paths] data_dir` (where all state lives), and the whole
`[agents.*]` section. That last one is locked by section prefix rather than by a key entry, since agent
names cannot be enumerated ahead of time, and it is locked for the obvious reason: `update_config` is a
tool the assistant holds, so a writable agent table would let the assistant widen its own reach. A
runtime-mutable setting is **one entry** in
[`config/table.py`](../../src/kokua/config/table.py)'s `RUNTIME_SETTINGS`, which drives the TOML
schema, the panel sanitizer, the hot-apply set, the live-apply loop, the channel mirroring, and the
persist path at once -- and `tests/config/test_table.py` fails if an entry is not also a real config
field and documented in `config.example.toml` under its own `[section]`.

## 4. All state under one directory the user owns

`$KOKUA_HOME` (default `~/.kokua`): `config.toml` at the root, content under `data/`. Nothing is
written inside the installed package, into a hidden cache, or split across XDG directories.

*How this cashes out:* [`config/paths.py`](../../src/kokua/config/paths.py) holds exactly three
locations -- the root, `data/`, and `config.toml` -- because those are the only ones that must resolve
*before* the settings file can be read. Every leaf below `data/` is a derived property on
`AssistantConfig` (`sessions_path`, `skills_dir`, `memory_path`, `images_path`, `logs_path`, ...), so
a single `[paths] data_dir` override moves all of them; adding a leaf function to `config/paths.py` would
silently bypass that, which is why the module docstring says not to. `tests/conftest.py` redirecting
`KOKUA_HOME` to a temp directory is sufficient isolation for the entire suite.

## 5. A single user, one process, with concurrency rules written down

One process serving one person is what makes a live scheduler, per-conversation agents, and
last-writer-wins config writes reasonable. Kokua is not multi-tenant and does not pretend to be. But
turns *are* concurrent, and every rule that makes that safe is named, next to the code, with the bug
it prevents.

*How this cashes out:* [`core/turns.py`](../../src/kokua/core/turns.py) opens with a
`## Concurrency invariants` block -- five rules, each stating what breaks without it, including a
deadlock that a regression test still guards. [`TurnGate`](../../src/kokua/core/turn_gate.py) is a
documented writer-preferring readers-writer gate: turns read, a settings change writes.
[`AgentRegistry`](../../src/kokua/core/agent_registry.py) gives each conversation its own agent and
model client, with LRU eviction and a pin held for the duration of any in-flight turn. A background
or scheduled turn auto-denies a gated tool because nobody is watching it. Every human decision is a
lock-guarded single slot ([`core/interaction.py`](../../src/kokua/core/interaction.py)), so concurrent
turns cannot clobber each other's prompt. The web front end refuses a second connection.
`config/store.py` states last-writer-wins rather than adding file locking.

## 6. Verifiable without a model

The default test suite is mock-only: no network, no API keys, no provider, and a temp `$KOKUA_HOME`.
Anything needing a browser or a live model is opt-in and never gates the default run. This is a
design constraint on the code, not just a habit in the tests -- it is *why* the model client is
injectable and why the builders are free functions rather than methods on the assistant.

*How this cashes out:* `Assistant.create(config, channel, client=..., client_factory=...)` exists so
a test can inject. [`core/build.py`](../../src/kokua/core/build.py) is free functions taking a config
and returning parts. `planning/reviewers.py`'s `_reviewer_agent` is factored out, in its own words,
"so tests can monkeypatch it". `pytest`'s `addopts` deselect `-m e2e`; the Playwright suite skips
rather than errors when the extra is absent. `tests/helpers.py` vendors `MockAsyncModelClient` so the
suite does not reach into the sibling AIMU checkout.

## What follows from these principles

Each one excludes things, and most of what is absent from Kokua is absent for a reason on this list.

**A small, transport-agnostic core** rules out transport branches in the core, and rules out a
`Channel` contract that a new front end must implement twelve methods to satisfy. **Grow by plugin**
rules out a hardcoded tool registry, an `if config.enable_x` switchboard, and importing a front end's
dependencies at core-import time. Its corollary, **declared and never defaulted**, additionally rules out
a tool an agent gets without asking, a flag that grants capability, and a code path that filters a
declaration it disagrees with. **`config.toml` as the single source** rules out a second settings
store and rules out environment variables as a settings mechanism -- the three that exist
(`KOKUA_HOME`, `KOKUA_CONFIG`, `KOKUA_EMAIL_PASSWORD`) are exactly the ones that *cannot* live in the
file: the file's own location, twice, and a secret. **One directory the user owns** rules out writing
inside the package, XDG-split state, and per-feature configurable paths. **Single user, one process**
rules out multi-tenant session keying, authentication on the web front end, file locking, and a job
queue. **Verifiable without a model** rules out any test that needs a model or a key, any fixture that
touches the real `~/.kokua`, and any code path reachable only with a live provider.

None of these are missing because they were hard. They are missing because they would make Kokua a
different kind of program.

## What this means for contributing

Before proposing a change, ask which principle it serves. A change that serves none of them is
probably a plugin, not a core change -- and principle 2 exists to make that an easy answer rather than
a rejection.

The harder question is which principle a change *violates*. Some concrete forms that question takes
here:

- A new transport is a `FrontEnd`. A new capability is a `Toolset`. Neither is a core change, and
  neither reaches an agent until an `[agents.*]` table names it.
- A new runtime setting is one `RUNTIME_SETTINGS` entry, one `AssistantConfig` field, and one input in
  the web panel. If it takes more edits than that, the table needs fixing, not working around.
- Anything that writes state derives its path from `AssistantConfig`, never from a new function in
  `config/paths.py`.
- Anything touching turn concurrency updates the invariants block in `core/turns.py`, in the same
  commit, with the failure it prevents.
- Anything a test cannot reach without a live model is not finished.

If you cannot tell which principle applies, the principles are not doing their job; open a discussion.

## Not (yet) a principle

**"Failures reach the user, not just the log."** This is a goal, not a description: today a bad model
string or a malformed config can still surface as a stack trace or a silent failure rather than a
message in the chat. It is [TODO.md](../../TODO.md) item 1. Six honest principles beat seven with one
aspirational; when that item lands, this becomes the seventh.

## See also

- [Architecture](architecture.md): the shape that falls out of these principles.
- [CONTRIBUTING.md](../../CONTRIBUTING.md): the mechanics.
- [AIMU's design principles](https://saxman.info/aimu/explanation/design-principles/): the
  library-level six that Kokua inherits rather than restates.
