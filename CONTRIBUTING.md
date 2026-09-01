# Contributing to Kokua

Thanks for your interest. Kokua is a small, hackable personal assistant built on
[AIMU](https://saxman.info/aimu/), and it exists so people can learn how agentic systems work by
reading, running, and extending a real one. That goal is what most of the conventions below are
protecting, so it is worth reading
[why Kokua exists](https://saxman.info/kokua/explanation/design-principles/#why-kokua-exists) before your first change:
a contribution that makes the system harder to follow costs more than the capability it adds.

## Setup

```bash
uv sync --all-extras --no-sources     # AIMU from PyPI; enough to work on Kokua alone
```

Kokua and AIMU are developed together, so `[tool.uv.sources]` points AIMU at a sibling `../aimu`
checkout installed editable. Drop `--no-sources` if you have that checkout and want your AIMU edits
picked up live:

```bash
git clone https://github.com/saxman/aimu    # sibling of kokua/
uv sync --all-extras                        # installs ../aimu editable
```

A path source is installed without being checked against the `aimu>=0.20.0` specifier, so a sibling on
an older branch is the failure mode to expect. Startup's preflight (`kokua.aimu_compat`) catches it and
names the fix rather than failing on an import.

Without `uv`:

```bash
pip install -e '.[web,dev]'
```

## Checks

Run these before opening a PR:

```bash
uv run ruff check .
uv run ruff format --check .      # use `ruff format .` to apply
uv run pytest -q
```

Line length is 120 (`ruff` is configured in `pyproject.toml`). Tests are mock-only and require no model,
network, or API keys.

## Design principles

Read [docs/explanation/design-principles.md](https://saxman.info/kokua/explanation/design-principles/) first. It opens
with why Kokua exists, then the six principles that serve it: 1 and 2 keep Kokua readable, 3 and 4 keep
it observable, 5 keeps it runnable by anyone who clones it, and 6 keeps its capability yours to bound. A change that serves none of them is
probably a plugin rather than a core change, and principle 2 exists to make that an easy answer.

## Where does it go?

`src/kokua/` is grouped by subsystem, and `tests/` mirrors it exactly -- a test goes where its module
does.

| Subpackage | Holds |
|---|---|
| `core/` | the transport-agnostic runtime: the assistant, conversations, turns, human decisions, runtime settings, agent building |
| `config/` | the settings schema, the TOML file, the writers, the write policy, and the runtime-settings table |
| `workflows/` | the workflow protocol and its two tiers, the shared reviewer, and `planning/`, the `/plan` pipeline |
| `mcp/` | remote MCP servers and their OAuth |
| `scheduling/` | recurrence math and the durable task lifecycle over `config.toml` |
| `channels/` | `ChannelUI` plus the concrete channels |
| `frontends/` | `cli` and `web`, registered as plugins exactly as a third party's would be |
| `toolsets/` | the registry, Kokua's six core toolsets, the AIMU wrappers, and the three plugin toolsets |

**Agent tools live under `toolsets/`, and only there.** A module defining an `@aimu.tool` is a toolset
module and nothing else is, so `grep -rl '@tool' src/kokua/` should only ever find files in that one
directory.

`cli.py`, `plugins.py`, `images.py`, `logging_setup.py`, `config.example.toml` and `web_static/` stay
at the package root: entry points and package-data paths point at them.

The stable public import surface is `kokua.plugins`, `kokua.config`, `kokua.core`,
`kokua.channels.web`, and `kokua.images`. Everything else is internal and may move.

## Conventions

- Plain Python: dataclasses, functions, type hints. Keep the core small; push capability into plugins.
- Add a **front end** (a new transport) or a **toolset** (a named capability an agent can declare) as a
  plugin, in its own package or under `src/kokua/frontends` / `src/kokua/toolsets`, registered via the
  `kokua.frontends` / `kokua.toolsets` entry-point groups. See
  [`src/kokua/toolsets/image.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/image.py) for the template: one tool, and a
  `build` that returns nothing when its prerequisite is missing. Neither reaches an agent until an
  `[agents.*]` table in `config.toml` names it.
- A new **runtime setting** for a *toolset* is one `kokua.toolsets.Setting` on the toolset and nothing
  else. For one of Kokua's own it is one `CORE_RUNTIME_SETTINGS` entry in `config/table.py` and one
  `AssistantConfig` field. If either takes more edits than that, fix the table rather than working
  around it. `tests/config/test_table.py` enforces that a core entry is a real config field and is
  documented in `config.example.toml` under its own section.
- Anything that writes **state** derives its path from `AssistantConfig`, never from a new function in
  `config/paths.py`.
- Anything touching **turn concurrency** updates the invariants block at the top of `core/turns.py`, in
  the same commit, naming the failure it prevents.
- Docstrings explain *why*, and stand on their own: no bare task or phase numbers ("Task 6",
  "Phase B"), which point at a design history that isn't in this repository.
- Use English punctuation (no em dashes); inclusive terminology (allowlist/blocklist, primary/replica, main
  branch).
- Update `CHANGELOG.md` and the README when you change behavior or the public surface. Include tests.

## Pull requests

One concern per PR. Make sure `ruff check`, `ruff format --check`, and `pytest` pass.
