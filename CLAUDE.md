# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --all-extras                     # install; pulls the editable sibling ../aimu (see below)
uv run pytest -q                         # full test suite (mock-only: no model, network, or keys)
uv run pytest tests/test_web.py::test_ws_round_trip -q   # a single test
uv run ruff check . && uv run ruff format --check .      # lint (format with `ruff format .`)
uv run kokua --frontend web              # run the web UI (or `kokua-web`); `kokua` alone is the CLI
uv run kokua config init                 # scaffold $KOKUA_HOME/config.toml from the documented example
```

Line length is 120 (configured in `pyproject.toml`). Run lint + tests before committing; update
`CHANGELOG.md` and `README.md` when you change behavior or the public surface.

## AIMU dependency (important)

Kokua is built on the [AIMU](https://github.com/saxman/aimu) library and uses features on AIMU's `main`
that are not yet in a published release. `[tool.uv.sources]` in `pyproject.toml` pins
`aimu = { path = "../aimu", editable = true }`, so a sibling `../aimu` checkout must exist and `uv sync`
installs it live. The trap: the PyPI build of AIMU carries the same version string but lacks these
features, so a plain install silently gives you the wrong AIMU. For CI or a clone without `../aimu`, swap
that source for the git one noted in `pyproject.toml`.

## Architecture

Kokua wraps AIMU primitives into a single-user, always-on personal assistant. The design goal: keep a
small core and push capability into plugins.

**`assistant.py` is the transport-agnostic core.** `Assistant` takes a `Channel` (so the CLI and web
front ends share it unchanged) and wires: an AIMU `SkillAgent` (skill authoring + runnable skill
scripts), a `Scheduler` (proactive reminders), a `TinyDBSessionStore` (multiple conversations, each an
`aimu.sessions.Session`), shared memory (a `SemanticMemoryStore` for facts + a `DocumentStore` for
documents), remote MCP clients, and a tool-approval gate. Non-obvious control flow: the serve loop runs
each reactive turn as a background `aio.RunHandle` so the channel keeps reading during a turn — that is
what lets a `/stop` message cancel an in-flight reply and what lets a web approval reply be routed back
to the waiting tool call. Switching conversations cancels and awaits the current turn (so its partial
state persists to the *old* conversation) before restoring the new one.

**Plugins via entry points.** Two groups: `kokua.frontends` (a `FrontEnd` with `run(config, args)`) and
`kokua.tools` (a `ToolPack` with `build(config)`). The built-in `cli`/`web` front ends and the `example`
tool-pack are registered in Kokua's own `pyproject.toml` exactly as a third party would register theirs.
`plugins.py` discovers them at runtime. Add a transport or new tools as a plugin, not by editing the core;
see `src/kokua/toolpacks/example.py` for the template.

**Config layering.** Precedence is **CLI flag > TOML config file > built-in default**. `config.py` holds
`AssistantConfig` (plain dataclass; leaf paths derive from `data_dir`). `settings.py` finds and parses the
TOML file into schema-validated `AssistantConfig` overrides. `cli.py`'s `resolve_config` merges
`settings.load()` under `_cli_overrides()`. Flag defaults are the `None` sentinel so an unspecified flag
defers to the file/default. `config.example.toml` documents every key at its default.

**State lives under `~/.kokua`** (override with `KOKUA_HOME`); `paths.py` owns all locations. `data/`
holds `sessions.json` (conversations), `skills/`, `memory/`, `documents/`, and `mcp-servers.json`.

**MCP servers** come from config `[mcp]` at startup or the runtime `add_mcp_server` tool. `mcp_auth.py`
(`ChatOAuth`) handles OAuth by posting the authorization link into the chat and persisting tokens to
disk; `mcp_registry.py` records runtime-added servers (URL + auth mode, never a bearer secret) so they
reconnect across restarts.

**Web front end.** `frontends/web.py` is a Starlette + uvicorn WebSocket server (behind the `web` extra);
`channels/web.py`'s `WebChannel` subclasses AIMU's base `WebChannel`. The streaming transport
(`token`/`thinking`/`tool`/`done` frames and `send()`) lives in AIMU's base; Kokua's subclass adds the
`conversations`, `history`, and `approval` frames its richer page needs. The UI is a single
self-contained `web_static/index.html` served as package data, plus vendored `marked` + `DOMPurify`
(GitHub-flavored markdown, sanitized, rendered client-side on turn completion) and vendored KaTeX
(`katex.min.*` + `auto-render.min.js` + `fonts/*.woff2`) for LaTeX math, typeset after sanitization
with `trust:false`. The web server allowlists these assets: JS/CSS by name, the woff2 fonts under
`/fonts/`.

## Testing notes

Tests are mock-only. `tests/helpers.py` provides `MockAsyncModelClient`; `tests/conftest.py` redirects
`KOKUA_HOME` to a temp dir so tests never touch real state. The mock **fakes tool-call rounds** rather
than running AIMU's real dispatch, so features that hook dispatch (e.g. the tool-approval gate) are
tested by calling `agent._prepare_run()` then `agent.model_client._handle_tool_calls([...])` directly.
Client-side page JS (markdown rendering, theme, sidebar) has no pytest coverage — verify it with a
headless browser.

## Conventions

Use English punctuation (no em dashes) and inclusive terminology (allowlist/blocklist, primary/replica,
main branch). Keep the core small; prefer a plugin over a core change.
