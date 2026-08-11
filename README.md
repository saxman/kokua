<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/kokua-horizontal-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/kokua-horizontal-light.svg">
  <img alt="Kokua" src="docs/assets/kokua-horizontal-light.svg" width="420">
</picture>

**A personal AI assistant that extends itself.**

[![CI](https://github.com/saxman/kokua/actions/workflows/ci.yml/badge.svg)](https://github.com/saxman/kokua/actions/workflows/ci.yml) ![GitHub License](https://img.shields.io/github/license/saxman/kokua) ![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsaxman%2Fkokua%2Frefs%2Fheads%2Fmain%2Fpyproject.toml) [![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv) [![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

[Design principles](docs/explanation/design-principles.md) · [Architecture](docs/explanation/architecture.md) · [Configuration](src/kokua/config.example.toml) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

</div>

**Kokua** (Hawaiian: *help, assistance*) is a hackable, modular personal-assistant application (OpenClaw / Hermes Agent style) built on the [AIMU](https://saxman.info/aimu/) library. It runs an always-on assistant that chats with you, authors and runs its own skills, connects to remote tool services, delegates independent subtasks to isolated sub-agents, schedules its own proactive work, and remembers facts and documents across conversations.

Kokua **extends itself**: it writes and runs new skills to take on capabilities it didn't ship with, and grows its reach by connecting to remote MCP services on its own. Where it can't extend itself, you extend it: front ends and tool-packs are **plugins** you add by installing a package, never by editing the core.

```bash
kokua                              # chat in the terminal
kokua --frontend web               # or a browser UI at http://127.0.0.1:8000
kokua --list-tool-packs            # see what capability is installed
kokua config init                  # scaffold ~/.kokua/config.toml, every key documented
```

It runs as a single user in a single process, and can run code and connect to remote services with your privileges (see [Security](#security)).

## Why Kokua

Kokua is small on purpose. Six principles decide what belongs in the core: a **transport-agnostic core** that knows a channel rather than a terminal or a socket, **growth by plugin** rather than core change, **`config.toml` as the single source of settings** that the app itself writes, **all state under one directory you own**, **a single user and one process with concurrency rules written down**, and **verifiability without a model**. These sit on top of, and inherit, [AIMU's six library-level principles](https://saxman.info/aimu/explanation/design-principles/). The reasoning behind each of Kokua's, and the patterns each one excludes, is on the [design principles](docs/explanation/design-principles.md) page; the shape that falls out of them is in [architecture](docs/explanation/architecture.md).

The practical consequence: the assistant core is a few hundred lines that knows nothing about terminals or browsers, and adding a transport or a capability means shipping a package, not patching Kokua.

## Install

Kokua depends on AIMU, and currently uses AIMU features that are on AIMU's `main` branch but not yet in a published release. It therefore installs AIMU **from source as an editable dependency**. Clone AIMU as a sibling of this repo, then sync:

```bash
git clone https://github.com/saxman/aimu        # sibling of kokua/, if you don't have it
uv sync --all-extras                            # installs the local editable ../aimu automatically
```

`[tool.uv.sources]` pins `aimu = { path = "../aimu", editable = true }`, so `uv sync` always installs your local checkout (and picks up your edits live) rather than the PyPI build, which carries the same version string but lacks the features Kokua needs.

> **No sibling checkout?** For CI or a clone without `../aimu`, swap that source for the git one (see the comment in `pyproject.toml`):
>
> ```toml
> aimu = { git = "https://github.com/saxman/aimu", branch = "main" }
> ```
>
> Once AIMU publishes a release with the needed features, the override goes away and this becomes a normal `uv add kokua` / `pip install kokua`.

The `web` extra (`uv sync --all-extras`, or `pip install '.[web]'`) adds the browser front end; without it the CLI works alone.

## Quick start

```bash
kokua --model ollama:qwen3:8b
```

The model string is AIMU's [`provider:model_id`](https://saxman.info/aimu/how-to/switch-providers/) form, so any provider AIMU supports works here. Omit `--model` and AIMU [resolves a default](https://saxman.info/aimu/how-to/switch-providers/#use-whatever-model-is-already-available-locally): [`$AIMU_LANGUAGE_MODEL`](https://saxman.info/aimu/reference/env-vars/) if set, else the first already-running local model found (Ollama, then a local OpenAI-compatible server). A cloud model is never auto-selected, and startup fails with an actionable message if nothing resolves. Chat at the prompt; Ctrl-D exits.

Useful flags: `--tools web,fs,compute,time,misc` ([AIMU built-in tool groups](https://saxman.info/aimu/reference/api/tools/)), `--mcp <url>` (repeatable), `--no-memory`, `--no-plugins`, `--no-subagents`, `--system`, `--config <path>`, `--host` / `--port` (web).

Two commands worth knowing: **`/stop`** cancels a reply that's still streaming and keeps the partial turn, so the conversation continues (the web UI has a Stop button for the same). **`/diag`** reports the in-flight turn, the gate state, and a stuck turn's async stack; it never takes the turn gate, so it answers even when a hung turn is holding it. Rotating logs go to `$KOKUA_HOME/data/logs/kokua.log`, and `kill -USR1 <pid>` dumps all thread stacks there.

## Key features

### Conversation

- **[Multiple conversations](docs/explanation/architecture.md#the-core).** The web sidebar lists them, titled automatically from the first message, with new/delete and a collapsible, resizable rail whose state is remembered per browser. Memory is shared across all of them; the CLI stays single-conversation.
- **Concurrent, non-blocking turns.** Switching away from a streaming reply does not cancel it. Only the conversation you are viewing streams live; a turn that finishes elsewhere posts a dismissible notification, and switching into a conversation with work still running shows a "Working…" indicator.
- **Replayable transcripts.** Reloading replays the prior conversation, including reasoning and tool calls when `show_thinking` / `show_tools` are on. Auxiliary blocks (thinking, tool calls, phases, sub-agent cards, drafted plans) render collapsed behind a labeled header; messages and prompts stay open. Each bubble carries a localized datetime caption that survives reloads.
- **Safe rendering.** Replies render as GitHub-flavored markdown once a turn completes, via vendored `marked` + `DOMPurify` (no CDN), so model or tool output cannot inject scripts. LaTeX (`$...$`, `$$...$$`) is typeset with vendored KaTeX *after* sanitization, with `trust` disabled.
- **[Runtime settings panel](src/kokua/config.example.toml).** The gear button changes the model, the generation parameters (`temperature`, `max_tokens`, `top_p`, `top_k`, `presence_penalty`, `repetition_penalty`), and the display prefs mid-session. Changes apply on the next turn and are written back to `config.toml`, so they survive restarts. Leave a field blank for the provider default. Also holds an auto/light/dark theme selector, applied before first paint.

### Capability

- **[Self-authored skills](https://saxman.info/aimu/how-to/use-skills/).** Built on AIMU's `SkillAgent`: the assistant writes `SKILL.md` skills (the same format Claude Code uses), and bundles runnable Python/shell scripts it can author and execute in the same turn, so it takes on capabilities it did not ship with.
- **[Remote MCP services](https://saxman.info/aimu/how-to/use-mcp-tools/).** Kokua wires AIMU's `MCPClient` to config and to the agent. Connect a server with `--mcp <url>` or `[[mcp.server]]`, or let the assistant connect one itself with its `add_mcp_server` tool. OAuth is handled by posting the authorization link into the chat and persisting the tokens; bearer-token servers read their secret from an env var named by `token_env`, never from the config file.
- **[Sub-agents](https://saxman.info/aimu/how-to/spawn-subagents/).** AIMU's `spawn_subagent(agent_type, task)` delegates an independent subtask to a fresh, isolated agent. Roles are defined entirely in `config.toml` under `[subagents.roles.*]`, with none baked into the code: `kokua config init` writes `researcher`, `coder`, and `generalist`, and you edit, delete, or add to that list freely. Each role clones the active model with its own tool subset (its groups intersected with your enabled `[tools]` groups, plus the `time` group so no worker is left unable to tell the time; parent-only memory/skills/MCP tools are withheld), and can also name specific MCP servers and tool-packs. With no roles configured at all, `spawn_subagent(task)` falls back to a single untyped worker carrying every enabled tool. Independent spawns in one turn run concurrently. In the web UI each spawn gets its own foldable card in the conversation that spawned it, holding the sub-agent's thinking, tool calls, and live-streamed answer as the same collapsible blocks the assistant's own turn uses (nested reasoning follows `show_thinking`, nested tool calls follow `show_tools`), and the card replays on reload.
- **Lean supervisor mode** (on by default). Keeps the always-on agent's tool context small: it mounts only cross-cutting tools (memory, skills, MCP management, config, scheduling, and the `time` group) plus `spawn_subagent`, answers trivial requests itself, and delegates specialized work to the role-scoped workers above. Here `[tools] groups` defines the universe workers may draw from rather than the assistant's own toolset, so a supervisor has no calculator of its own and delegates arithmetic like anything else. Set `lean_supervisor = false` for a flat agent that carries every tool itself.
- **Persistent memory.** AIMU's [semantic store](https://saxman.info/aimu/how-to/use-semantic-memory/) for facts and [document store](https://saxman.info/aimu/how-to/use-document-memory/) for text, shared across all conversations and surviving restarts. Both live under [`data/`](#state); `--no-memory` turns them off.

### Proactive work

- **[Scheduled tasks](src/kokua/scheduling/).** Ask for something on a schedule ("every weekday at 9am, summarize my calendar") and the assistant persists it to `data/scheduled_tasks.json` via its own `schedule_task` / `list_scheduled_tasks` / `cancel_scheduled_task` tools; it survives restarts. Schedules are one-shot, interval, daily, or weekly.
- **Where a task runs.** By default a firing lands in whatever conversation you are viewing (shown with amber "proactive" styling). Ask for its own conversation and each firing opens a fresh chat; ask for an ongoing one and every firing writes to the single chat it created first, building on its own history.
- **Pause, resume, dry-run.** `disable_scheduled_task` stops a task firing while keeping it; `enable_scheduled_task` resumes it; `run_scheduled_task` fires it now without touching its schedule, reproducing exactly what the scheduled run would do. Scheduled runs auto-deny the approval-gated tools, since nobody is present to approve them.

### Deep planning

- **[Plan before doing](src/kokua/planning/).** When you ask for it, the assistant drafts an explicit plan first -- which tools, skills, and MCP services it will use, what it will search for, where it needs to build a skill or connect a server -- then carries it out. Planning is per request, not a global mode: use the **Plan** toggle beside the message box, or send `/plan <task>` (which also works in the CLI).
- **Human review.** Enable *Review the plan before executing* to pause a planned turn for your Approve / Edit / Reject; otherwise the plan runs automatically.
- **Adversarial review.** An independent reviewer agent with no conversation context can critique the plan (Kokua re-plans on rejection) and/or the final answer before you see it (Kokua revises, up to `review_rounds`). Both are off by default and combine with human review, whose prompt shows you the critique. Reviewing the result means the answer cannot stream live: the agentic loop still streams, but the answer appears only once it passes.
- **Reviewers check their claims.** Each reviewer is a tool-using agent that runs a bounded assessment over a curated verification toolset (current date/time, web lookup, computation) before returning a verdict, so it can confirm recency and numeric claims instead of rejecting what it cannot verify from the request alone. The toolset excludes your memory, documents, skills, and MCP servers, keeping the reviewer an independent critic with no access to your state. See [Security](#security) for a known limitation.
- **Show all reasoning.** Turn it on for the full trace: every LLM call in a planned turn (planner, each reviewer, executor, each revision) streams live under a labeled phase header, and every intermediate version is shown. This overrides result review's hide-until-vetted gate; only the final answer is saved. The raw trace is recorded per turn, so reloading replays exactly what you saw rather than a summary.

### Files and media

- **[Images in and out](src/kokua/images.py).** Attach an image and the assistant reads it (needs a [vision-capable model](https://saxman.info/aimu/reference/model-matrix/)): the composer's paperclip or a paste in the web UI, `/attach <path>` in the CLI. It can also [generate images](https://saxman.info/aimu/how-to/generate-images/) when [`$AIMU_IMAGE_MODEL`](https://saxman.info/aimu/reference/env-vars/) is set (e.g. `gemini:nano-banana`, or a HuggingFace diffusers `hf:<repo>`); without it, no generation tool is offered at all. Images live in `data/images/` and are served at `/images/<name>`; a conversation stores only a short reference, so `sessions.json` stays compact.
- **[PDFs](src/kokua/toolpacks/pdf.py).** The built-in `pdf` tool-pack adds `markdown_to_pdf`: ask for something as a PDF and it writes to `data/downloads/`, handing back a download link in the web UI or a path from the CLI.
- **[AIMU agents](src/kokua/toolpacks/aimu_agents.py).** The `aimu_agents` tool-pack mounts AIMU's prebuilt orchestrators -- `code_review`, `research_report`, `create_content` -- and is the worked example of wiring an agent built with AIMU into Kokua: any `Runner` exposes `.run(task) -> str`, so a tool-pack is the whole bridge and the core learns nothing new. Nothing is mounted until you ask for it: give a role `tool_packs = ["aimu_agents"]` in `config.toml`. They are synchronous, so a nested run gets no sub-agent card, no `/stop`, and no approval gate on its workers; and a `coder` role with `fs` + `compute` is a stronger reviewer than the tool-less `CodeReviewAgent`. Copy the shape, not necessarily the agents.
- **[Email](src/kokua/toolpacks/email.py).** The `email` tool-pack lets the assistant mail information to you -- digests, summaries, reports -- written in Markdown and delivered as formatted HTML with a plain-text fallback, optionally attaching files already in `data/downloads/` or `data/images/`. It can only email **you**: the recipient is fixed to `[email] to`, so the tool takes no recipient argument. Configure `[email]` (`host`, `port`, `from`, `to`, `use_ssl`) and put the password in `$KOKUA_EMAIL_PASSWORD`, never in the config file (for Gmail, an App Password). The tool appears only once host, `to`, and the password are all present. Sending is ungated, so a daily digest can send itself.

## Configuration

Settings come from a TOML file, so you don't repeat flags. Precedence, highest first: **command-line flag > config file > built-in default**. The file is read from `--config <path>`, else `$KOKUA_CONFIG`, else `$KOKUA_HOME/config.toml` (default `~/.kokua/config.toml`); a missing default-location file is fine. Every setting has a built-in default, so the file is optional and you set only what you want to change.

```bash
kokua config init           # writes the documented example; --force to overwrite
```

The scaffold comments every key at its default, so changing a default in a later release still reaches keys you left commented. See [`config.example.toml`](src/kokua/config.example.toml) for the full set.

`config.toml` is also **app-written**: the settings panel, the assistant's own `update_config` tool, and a runtime `add_mcp_server` all write back to it, with your comments preserved. There is no second settings store.

### State

All state lives under `~/.kokua` (override the root with `$KOKUA_HOME`). The root holds `config.toml`; a single `data/` directory holds all content:

```
~/.kokua/
  config.toml            # all settings; app-written as well as hand-edited
  data/
    sessions.json        # conversations
    skills/              # authored skills
    memory/              # semantic facts
    documents/           # saved documents
    downloads/           # generated files (e.g. PDFs), served at /download
    images/              # uploaded + generated images, served at /images
    scheduled_tasks.json # durable scheduled tasks
```

Point `data/` elsewhere with `[paths] data_dir`. Nothing is written to your working directory or inside the installed package.

## Extending Kokua

Kokua discovers two kinds of plugin at runtime through Python entry points, so a third party adds capability by publishing a package, with no change to Kokua's core:

- **Front ends** (`kokua.frontends` group): how the assistant runs -- terminal, web, a future Telegram or Slack. A front end is a `kokua.plugins.FrontEnd` whose `run(config, args)` drives the assistant.
- **Tool-packs** (`kokua.tools` group): extra agent tools. A tool-pack is a `kokua.plugins.ToolPack` whose `build(config)` returns [`@aimu.tool`](https://saxman.info/aimu/how-to/add-custom-tool/) callables, merged into the agent automatically.

The built-in `cli` / `web` front ends and the five tool-packs are registered exactly this way in Kokua's own `pyproject.toml` -- if the built-in path and the plugin path ever diverge, the plugin path is the broken one. To add your own from another package:

```toml
# in your package's pyproject.toml
[project.entry-points."kokua.tools"]
weather = "my_weather_pack:TOOL_PACK"
```

`pip install` it, and `kokua --list-tool-packs` shows it; its tools appear on the agent next run. See [`toolpacks/example.py`](src/kokua/toolpacks/example.py) for the template, and [`toolpacks/aimu_agents.py`](src/kokua/toolpacks/aimu_agents.py) for the same shape carrying a whole AIMU agent rather than a plain function.

## Security

Kokua can author and run Python/shell scripts as **real subprocesses with your user privileges (no sandbox)**, and connect to remote MCP servers and run whatever tools they expose. Real capability is the point of a personal assistant, but it means a prompt-injected or mistaken model can run arbitrary code on your machine and call arbitrary remote tools. Only run Kokua with a model, inputs, and MCP servers you trust. The CLI prints a notice on startup.

**Tool approval.** The riskiest tools require confirmation before each call -- a `y/N` prompt in the terminal, Allow/Deny buttons in the web UI. By default this gates `add_skill_script`, `add_mcp_server`, and `execute_python`. Adjust with `[security] confirm_tools` or `--confirm-tools name1,name2` (empty disables it). Proactive and backgrounded turns auto-deny these regardless, so the assistant never runs a full-access tool unattended, and an approval prompt only ever appears for the conversation you are currently viewing.

**Known limitation -- reviewer tools bypass the approval gate.** With adversarial review on, the reviewer is a tool-using agent whose verification toolset includes `execute_python` (so it can check numeric claims). Unlike the main agent it has **no** approval gate -- an autonomous critic cannot pause to ask you mid-review -- so it can run arbitrary Python unattended while reviewing. This is an intentional short-term tradeoff we intend to revisit (sandboxing the reviewer, or restricting it to `calculate`-only arithmetic). Until then, treat "review on" as granting the reviewer the same code-execution reach the main agent has.

## Development

```bash
uv sync --all-extras                                  # installs the editable ../aimu + all extras
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                                      # mock-only: no model, network, or keys
uv run pytest -m e2e                                  # opt-in browser tests (playwright install chromium)
```

`src/kokua/` is grouped by subsystem, and `tests/` mirrors it exactly:

```
core/         the transport-agnostic runtime: assistant, conversations, turns, interaction,
              settings_runtime, diagnostics, build, agent_registry, turn_gate, messages, errors
config/       the settings schema, the TOML file, the writers, the runtime-settings table
planning/     the /plan pipeline and the context-free reviewer agents
mcp/          remote MCP servers and their OAuth
scheduling/   recurrence math, the durable task registry, the agent-facing tools
channels/     ChannelUI plus the concrete channels
frontends/    cli, web        -- registered as plugins, exactly like a third party's would be
toolpacks/    example, aimu_agents, pdf, image, email
```

The stable public import surface is `kokua.plugins`, `kokua.config`, `kokua.core`, `kokua.channels.web`, and `kokua.images`. Everything else is internal and may move.

## Resources

### Kokua

- 💡 [Design principles](docs/explanation/design-principles.md): the six that decide what belongs in the core, each with the code that backs it and the patterns it excludes.
- 🏗️ [Architecture](docs/explanation/architecture.md): module layout, control flow, and the concurrency model.
- ⚙️ [`config.example.toml`](src/kokua/config.example.toml): every setting, documented at its default.
- 🧩 [`toolpacks/example.py`](src/kokua/toolpacks/example.py): the tool-pack template.
- 📋 [CHANGELOG](CHANGELOG.md) · [TODO](TODO.md): what changed, and what's known but not yet scheduled.

### AIMU

Kokua is a thin application over [AIMU](https://saxman.info/aimu/), so most capability questions are really AIMU questions. The primitives Kokua wires together:

- 🔌 [Switch providers](https://saxman.info/aimu/how-to/switch-providers/): the `provider:model_id` string, default-model resolution, timeouts, and failover -- what `--model` accepts and how it resolves when you omit it.
- 🤖 [Personal-assistant primitives](https://saxman.info/aimu/how-to/build-personal-assistant/): the `Channel` transport, the `Scheduler`, and runtime skill authoring. The three pieces Kokua is built from.
- 🛠️ [Built-in tools](https://saxman.info/aimu/reference/api/tools/) · [custom tools](https://saxman.info/aimu/how-to/add-custom-tool/) · [MCP](https://saxman.info/aimu/how-to/use-mcp-tools/): what `--tools`, a tool-pack, and `--mcp` draw on.
- 🧠 [Semantic memory](https://saxman.info/aimu/how-to/use-semantic-memory/) · [documents](https://saxman.info/aimu/how-to/use-document-memory/) · [skills](https://saxman.info/aimu/how-to/use-skills/) · [sub-agents](https://saxman.info/aimu/how-to/spawn-subagents/): the capability layer.
- 📊 [Model matrix](https://saxman.info/aimu/reference/model-matrix/) · [environment variables](https://saxman.info/aimu/reference/env-vars/): which models support vision, tools, and reasoning, and the AIMU env vars Kokua inherits.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, where a new module goes, and PR conventions.

## License

Apache 2.0. See [LICENSE](LICENSE).
