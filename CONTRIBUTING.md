# Contributing to Kokua

Thanks for your interest. Kokua is a small, hackable application built on [AIMU](https://github.com/saxman/aimu).

## Setup

Kokua needs AIMU's latest features from a sibling `../aimu` checkout, which `[tool.uv.sources]`
pins as an editable install:

```bash
uv sync --all-extras        # installs Kokua plus the editable ../aimu
```

Without `uv`, install AIMU from source first (see the README), then Kokua:

```bash
pip install -e ../aimu      # a local AIMU checkout (sibling dir)
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

Read [docs/explanation/design-principles.md](docs/explanation/design-principles.md) first. Six
principles decide what belongs in this repository; a change that serves none of them is probably a
plugin rather than a core change, and principle 2 exists to make that an easy answer.

## Where does it go?

`src/kokua/` is grouped by subsystem, and `tests/` mirrors it exactly -- a test goes where its module
does.

| Subpackage | Holds |
|---|---|
| `core/` | the transport-agnostic runtime: the assistant, conversations, turns, human decisions, runtime settings, agent building |
| `config/` | the settings schema, the TOML file, the writers, and the runtime-settings table |
| `planning/` | the `/plan` pipeline and the context-free reviewer agents |
| `mcp/` | remote MCP servers and their OAuth |
| `scheduling/` | recurrence math, the durable task registry, and the agent-facing tools |
| `channels/` | `ChannelUI` plus the concrete channels |
| `frontends/`, `toolpacks/` | the built-in plugins |

`cli.py`, `plugins.py`, `images.py`, `logging_setup.py`, `config.example.toml` and `web_static/` stay
at the package root: entry points and package-data paths point at them.

The stable public import surface is `kokua.plugins`, `kokua.config`, `kokua.core`,
`kokua.channels.web`, and `kokua.images`. Everything else is internal and may move.

## Conventions

- Plain Python: dataclasses, functions, type hints. Keep the core small; push capability into plugins.
- Add a **front end** (a new transport) or a **tool-pack** (new tools) as a plugin, in its own package or
  under `src/kokua/frontends` / `src/kokua/toolpacks`, registered via the `kokua.frontends` / `kokua.tools`
  entry-point groups. See `src/kokua/toolpacks/example.py` for the template.
- A new **runtime setting** is one entry in `config/table.py`'s `RUNTIME_SETTINGS`, one
  `AssistantConfig` field, and one input in the web panel. If it takes more edits than that, fix the
  table rather than working around it. Tests enforce the first two.
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
