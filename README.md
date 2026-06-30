# Kokua

**Help, assistance** (Hawaiian). A hackable, modular personal-assistant application (OpenClaw / Hermes Agent
style) built on the [AIMU](https://github.com/saxman/aimu) library. Kokua runs an always-on assistant
that chats with you, takes proactive actions on a schedule, authors and runs its own skills, connects to
remote tool services, and remembers facts and documents across conversations. Front ends and tool-packs
are **plugins**: you extend Kokua by installing modules, not by editing the core.

Kokua began as AIMU's `examples/personal-assistant/` and grows it into a real application.

## Status

Alpha. Single user, single process. The assistant can run code and connect to remote services with your
privileges (see [Security](#security)).

## Install

Kokua depends on AIMU. It currently uses AIMU features that are on AIMU's `main` branch but not yet in a
published release, so it installs AIMU **from source as an editable dependency**, configured via
`[tool.uv.sources]` in `pyproject.toml`. Clone AIMU as a sibling of this repo, then sync:

```bash
git clone https://github.com/saxman/aimu        # sibling of kokua/, if you don't have it
uv sync --all-extras                             # installs the local editable ../aimu automatically
```

`[tool.uv.sources]` pins `aimu = { path = "../aimu", editable = true }`, so `uv sync` always installs the
local checkout (and picks up your edits live) rather than the PyPI build, which carries the same version
string but lacks the features Kokua needs. This requires `../aimu` to exist; for CI or a clone without it,
swap that source for a git one (see the comment in `pyproject.toml`):

```toml
aimu = { git = "https://github.com/saxman/aimu", branch = "main" }
```

(Once AIMU publishes a release with the needed features, the source override goes away and this becomes a
normal `uv add kokua` / `pip install kokua`.)

## Run

```bash
kokua --model anthropic:claude-sonnet-4-6 --reminder-seconds 30
```

Omit `--model` to use `AIMU_LANGUAGE_MODEL` or a locally available model. Chat at the prompt; Ctrl-D exits.
After ~30s a proactive message appears. Send `/stop` to cancel a reply that's still streaming (the partial
turn is kept, so the conversation continues); the web UI has a Stop button for the same.

Run the **web** front end instead (needs the `web` extra):

```bash
kokua --frontend web              # or: kokua-web
# then open http://127.0.0.1:8000
```

Reloading the page replays the prior conversation (the assistant already keeps its context across
reconnects; this makes it visible again), including reasoning and tool calls when `show_thinking` /
`show_tools` are on. Assistant replies are rendered as markdown (headings, lists, code, links, etc.)
once each turn completes; the source is HTML-escaped first, so model or tool output can't inject markup.
The page follows your OS light/dark preference, with a header toggle to override it (remembered across reloads).

The web UI holds multiple conversations: the left sidebar lists them (titled automatically from the first
message) and has a "+ New conversation" button; click any conversation to continue it. Memory (facts and
documents) is shared across all conversations. The CLI remains single-conversation for now.

List what's installed:

```bash
kokua --list-frontends     # cli, web, + any installed plugins
kokua --list-tool-packs    # example, + any installed plugins
```

Useful flags: `--tools web,fs,compute,misc` (AIMU built-in tool groups), `--mcp <url>` (repeatable, connect
a remote MCP server; `--mcp-bearer` for auth), `--no-memory`, `--no-plugins`, `--system`, `--config <path>`,
`--host` / `--port` (web).

### Configuration file

Settings can also come from a TOML config file, so you don't have to repeat flags. Resolution order,
highest precedence first: **command-line flag > config file > built-in default**. The file is read from
`--config <path>`, else `$KOKUA_CONFIG`, else `$KOKUA_HOME/config.toml` (default `~/.kokua/config.toml`); a
missing default-location file is fine. Every setting has a built-in default, so the file is entirely
optional and you only set what you want to change. See
[`config.example.toml`](src/kokua/config.example.toml) for the full set of keys with their defaults.

Scaffold a starter file at the default location with:

```bash
kokua config init           # writes $KOKUA_CONFIG or $KOKUA_HOME/config.toml; --force to overwrite
```

It writes the same documented example shown above (every key commented at its default), so changing a
built-in default in a later release still takes effect for keys you leave commented.

## Modules (plugins)

Kokua discovers two kinds of plugin at runtime via Python entry points, so a third party adds capability by
publishing a package, with no change to Kokua's core:

- **Front ends** (`kokua.frontends` group): how the assistant runs (terminal, web, future Telegram/Slack).
  A front end is a `kokua.plugins.FrontEnd` whose `run(config, args)` drives the assistant.
- **Tool-packs** (`kokua.tools` group): extra agent tools. A tool-pack is a `kokua.plugins.ToolPack` whose
  `build(config)` returns `@aimu.tool` callables, merged into the agent automatically.

The built-in `cli` / `web` front ends and the `example` tool-pack (a dice roller) are registered exactly
this way in Kokua's own `pyproject.toml`. To add your own from another package:

```toml
# in your package's pyproject.toml
[project.entry-points."kokua.tools"]
weather = "my_weather_pack:TOOL_PACK"
```

`pip install` it and `kokua --list-tool-packs` shows it; its tools appear on the agent next run. See
`src/kokua/toolpacks/example.py` for the template.

## State

All state lives under `~/.kokua` (override the root with the `KOKUA_HOME` environment variable). The root
holds an optional `config.toml` and a single `data/` directory for all transient and user-provided content:

```
~/.kokua/
  config.toml          # optional (see Configuration file)
  data/
    skills/            # authored skills
    sessions.json      # conversations (web UI can hold several)
    memory/            # semantic facts
    documents/         # saved documents
```

Point `data/` elsewhere with `[paths] data_dir` in the config file. Nothing is written to your working
directory. A `history.json` from an earlier single-conversation version, if present, is imported once into
`sessions.json` as your first conversation.

## Security

Kokua can author and run Python/shell scripts as **real subprocesses with your user privileges (no
sandbox)**, and connect to remote MCP servers and run whatever tools they expose. Real capability is the
point of a personal assistant, but it means a prompt-injected or mistaken model can run arbitrary code on
your machine and call arbitrary remote tools. Only run Kokua with a model, inputs, and MCP servers you
trust. The CLI prints a notice on startup.

**Tool approval.** The riskiest tools require your confirmation before each call: a `y/N` prompt in the
terminal, or Allow/Deny buttons in the web UI. By default this gates `add_skill_script`, `add_mcp_server`,
and `execute_python`. Adjust the set with `[security] confirm_tools` in the config file or `--confirm-tools
name1,name2` (an empty value disables it). Proactive (unprompted) turns auto-deny these regardless, so the
assistant never runs a full-access tool on its own schedule without you.

## Development

```bash
uv sync --all-extras            # installs the editable ../aimu + all extras (see Install)
ruff check . && ruff format --check .
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
