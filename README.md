# Mopai

**My own personal ai.** A hackable, modular personal-assistant application (OpenClaw / Hermes Agent
style) built on the [AIMU](https://github.com/saxman/aimu) library. Mopai runs an always-on assistant
that chats with you, takes proactive actions on a schedule, authors and runs its own skills, connects to
remote tool services, and remembers facts and documents across conversations. Front ends and tool-packs
are **plugins**: you extend Mopai by installing modules, not by editing the core.

Mopai began as AIMU's `examples/personal-assistant/` and grows it into a real application.

## Status

Alpha. Single user, single process. The assistant can run code and connect to remote services with your
privileges (see [Security](#security)).

## Install

Mopai depends on AIMU. It currently uses AIMU features that are on AIMU's `main` branch but not yet in a
published release, so it installs AIMU **from source as an editable dependency**, configured via
`[tool.uv.sources]` in `pyproject.toml`. Clone AIMU as a sibling of this repo, then sync:

```bash
git clone https://github.com/saxman/aimu        # sibling of mopai/, if you don't have it
uv sync --all-extras                             # installs the local editable ../aimu automatically
```

`[tool.uv.sources]` pins `aimu = { path = "../aimu", editable = true }`, so `uv sync` always installs the
local checkout (and picks up your edits live) rather than the PyPI build, which carries the same version
string but lacks the features Mopai needs. This requires `../aimu` to exist; for CI or a clone without it,
swap that source for a git one (see the comment in `pyproject.toml`):

```toml
aimu = { git = "https://github.com/saxman/aimu", branch = "main" }
```

(Once AIMU publishes a release with the needed features, the source override goes away and this becomes a
normal `uv add mopai` / `pip install mopai`.)

## Run

```bash
mopai --model anthropic:claude-sonnet-4-6 --reminder-seconds 30
```

Omit `--model` to use `AIMU_LANGUAGE_MODEL` or a locally available model. Chat at the prompt; Ctrl-D exits.
After ~30s a proactive message appears.

Run the **web** front end instead (needs the `web` extra):

```bash
mopai --frontend web              # or: mopai-web
# then open http://127.0.0.1:8000
```

List what's installed:

```bash
mopai --list-frontends     # cli, web, + any installed plugins
mopai --list-tool-packs    # example, + any installed plugins
```

Useful flags: `--tools web,fs,compute,misc` (AIMU built-in tool groups), `--mcp <url>` (repeatable, connect
a remote MCP server; `--mcp-bearer` for auth), `--no-memory`, `--no-plugins`, `--system`, `--config <path>`,
`--host` / `--port` (web).

### Configuration file

Settings can also come from a TOML config file, so you don't have to repeat flags. Resolution order,
highest precedence first: **command-line flag > config file > built-in default**. The file is read from
`--config <path>`, else `$MOPAI_CONFIG`, else `$MOPAI_HOME/config.toml` (default `~/.mopai/config.toml`); a
missing default-location file is fine. Every setting has a built-in default, so the file is entirely
optional and you only set what you want to change. See [`config.example.toml`](config.example.toml) for the
full set of keys with their defaults.

## Modules (plugins)

Mopai discovers two kinds of plugin at runtime via Python entry points, so a third party adds capability by
publishing a package, with no change to Mopai's core:

- **Front ends** (`mopai.frontends` group): how the assistant runs (terminal, web, future Telegram/Slack).
  A front end is a `mopai.plugins.FrontEnd` whose `run(config, args)` drives the assistant.
- **Tool-packs** (`mopai.tools` group): extra agent tools. A tool-pack is a `mopai.plugins.ToolPack` whose
  `build(config)` returns `@aimu.tool` callables, merged into the agent automatically.

The built-in `cli` / `web` front ends and the `example` tool-pack (a dice roller) are registered exactly
this way in Mopai's own `pyproject.toml`. To add your own from another package:

```toml
# in your package's pyproject.toml
[project.entry-points."mopai.tools"]
weather = "my_weather_pack:TOOL_PACK"
```

`pip install` it and `mopai --list-tool-packs` shows it; its tools appear on the agent next run. See
`src/mopai/toolpacks/example.py` for the template.

## State

All state lives under `~/.mopai` (override the root with the `MOPAI_HOME` environment variable). The root
holds an optional `config.toml` and a single `data/` directory for all transient and user-provided content:

```
~/.mopai/
  config.toml          # optional (see Configuration file)
  data/
    skills/            # authored skills
    history.json       # conversation
    memory/            # semantic facts
    documents/         # saved documents
```

Point `data/` elsewhere with `[paths] data_dir` in the config file. Nothing is written to your working
directory. On first run after upgrading from a pre-`data/` layout, existing top-level `history.json` /
`memory/` / `documents/` / `skills/` are moved into `data/` automatically.

## Security

Mopai can author and run Python/shell scripts as **real subprocesses with your user privileges (no
sandbox)**, and connect to remote MCP servers and run whatever tools they expose. Real capability is the
point of a personal assistant, but it means a prompt-injected or mistaken model can run arbitrary code on
your machine and call arbitrary remote tools. Only run Mopai with a model, inputs, and MCP servers you
trust. The CLI prints a notice on startup.

## Development

```bash
uv sync --all-extras            # installs the editable ../aimu + all extras (see Install)
ruff check . && ruff format --check .
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
