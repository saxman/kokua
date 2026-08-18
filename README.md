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

Kokua **extends itself**: it writes and runs new skills to take on capabilities it didn't ship with, and grows its reach by connecting to remote MCP services on its own. Where it can't extend itself, you extend it: front ends and toolsets are **plugins** you add by installing a package, never by editing the core.

```bash
kokua                              # chat in the terminal
kokua --frontend web               # or a browser UI at http://127.0.0.1:8000
kokua --list-toolsets              # see what capability is installed
kokua config init                  # scaffold ~/.kokua/config.toml, every key documented
```

It runs as a single user in a single process, and can run code and connect to remote services with your privileges (see [Security](#security)).

## Why Kokua

Kokua is small on purpose. Six principles decide what belongs in the core: a **transport-agnostic core** that knows a channel rather than a terminal or a socket, **growth by plugin** rather than core change, **`config.toml` as the single source of settings** that the app itself writes, **all state under one directory you own**, **a single user and one process with concurrency rules written down**, and **verifiability without a model**. These sit on top of, and inherit, [AIMU's six library-level principles](https://saxman.info/aimu/explanation/design-principles/). The reasoning behind each of Kokua's, and the patterns each one excludes, is on the [design principles](docs/explanation/design-principles.md) page; the shape that falls out of them is in [architecture](docs/explanation/architecture.md).

The practical consequence: the assistant core is a few hundred lines that knows nothing about terminals or browsers, and adding a transport or a capability means shipping a package, not patching Kokua.

## Install

Kokua needs Python 3.11+ and [AIMU](https://github.com/saxman/aimu) 0.16.0 or newer.

```bash
uv sync --all-extras --no-sources        # AIMU from PyPI; what you want to just run Kokua
```

The `web` extra (included in `--all-extras`, or `pip install '.[web]'`) adds the browser front end; without it the CLI works alone.

> **Why `--no-sources`?** Kokua and AIMU are developed together, so `[tool.uv.sources]` points AIMU at a sibling `../aimu` checkout, installed editable, which lets architectural changes move across the boundary without a release round-trip. `--no-sources` ignores that table and resolves AIMU from PyPI instead. If you *are* developing both, clone AIMU as a sibling of this repo and drop the flag:
>
> ```bash
> git clone https://github.com/saxman/aimu    # sibling of kokua/
> uv sync --all-extras                        # installs ../aimu editable; picks up your edits live
> ```
>
> The `aimu>=0.16.0` requirement governs the PyPI path only: uv installs a path source without checking it against the specifier, so a sibling checkout is not constrained by it. If yours falls behind, startup says so and names the fix rather than failing on an import.

## Quick start

```bash
kokua --model ollama:qwen3:8b
```

The model string is AIMU's [`provider:model_id`](https://saxman.info/aimu/how-to/switch-providers/) form, so any provider AIMU supports works here. Omit `--model` and AIMU [resolves a default](https://saxman.info/aimu/how-to/switch-providers/#use-whatever-model-is-already-available-locally): [`$AIMU_LANGUAGE_MODEL`](https://saxman.info/aimu/reference/env-vars/) if set, else the first already-running local model found (Ollama, then a local OpenAI-compatible server). A cloud model is never auto-selected, and startup fails with an actionable message if nothing resolves. Chat at the prompt; Ctrl-D exits.

Useful flags: `--list-toolsets` (every capability name this install accepts, grouped by what provides it), `--mcp <url>` (repeatable), `--no-plugins`, `--system` (overrides the entry agent's system message for this run only; a worker's own declared message is untouched), `--config <path>`, `--host` / `--port` (web). What each agent can *do* is not a flag: it is that agent's `tools` list in `config.toml` (see [Configuration](#configuration)).

Two commands worth knowing: **`/stop`** cancels a reply that's still streaming and keeps the partial turn, so the conversation continues (the web UI has a Stop button for the same). **`/diag`** reports the in-flight turn, the gate state, and a stuck turn's async stack; it never takes the turn gate, so it answers even when a hung turn is holding it. Rotating logs go to `$KOKUA_HOME/data/logs/kokua.log`, and `kill -USR1 <pid>` dumps all thread stacks there.

## Key features

### Conversation

- **[Multiple conversations](docs/explanation/architecture.md#the-core).** The web sidebar lists them, titled automatically from the first message, with new/delete and a collapsible, resizable rail whose state is remembered per browser. Memory is shared across all of them; the CLI stays single-conversation.
- **The assistant can read across conversations.** `list_conversations`, `read_conversation`, and `search_conversations` give it read-only sight of every saved conversation, so it can answer "what did we decide about this last week?" and carry context out of a past thread into a sub-agent's task. A transcript comes back as bounded plain text (what was said, no reasoning or tool calls), oldest messages dropped first behind a count when it does not fit. Reads come from the last saved snapshot, so a conversation with a reply still streaming is flagged as having unsaved messages, and the conversation you are in is flagged too -- its current turn is not saved yet.
- **Concurrent, non-blocking turns.** Switching away from a streaming reply does not cancel it. Only the conversation you are viewing streams live; a turn that finishes elsewhere posts a dismissible notification. Switch back into one that's still running and it catches you up -- the reasoning, tool calls, and answer so far, with a pulsing "working" indicator and Stop in the composer -- then keeps streaming where it left off.
- **Tool calls show their results.** A tool card carries what the call returned as well as its arguments, in a nested foldable labeled with the result's size. It fills on first open and clamps long output behind a "show all" button, as plain text rather than markdown, since a tool result is untrusted. Results show on live cards, on a background turn's catch-up, inside a sub-agent's card, and on reload -- a stored call is rejoined to its result by `tool_call_id`, so a replayed card shows what the live one did.
- **Replayable transcripts.** Reloading replays the prior conversation, including reasoning and tool calls when `show_thinking` / `show_tools` are on. Auxiliary blocks (thinking, tool calls, phases, sub-agent cards, drafted plans) render collapsed behind a one-line header carrying the call, its condensed arguments, and a result size; messages and prompts stay open. Every row has a localized datetime caption, revealed on hover, that survives reloads.
- **Safe rendering.** Replies render as GitHub-flavored markdown once a turn completes, via vendored `marked` + `DOMPurify` (no CDN), so model or tool output cannot inject scripts. LaTeX (`$...$`, `$$...$$`) is typeset with vendored KaTeX *after* sanitization, with `trust` disabled.
- **[Runtime settings panel](src/kokua/config.example.toml).** The gear button changes the model and the display prefs mid-session. Changes apply on the next turn and are written back to `config.toml`, so they survive restarts. Sampling parameters are not here: AIMU layers a model card's tuned profile under anything a caller sets, so a Kokua-side override only shadowed the card. Also holds an auto/light/dark theme selector, applied before first paint.

### Capability

- **[Self-authored skills](https://saxman.info/aimu/how-to/use-skills/).** Built on AIMU's `SkillAgent`: the assistant writes `SKILL.md` skills (the same format Claude Code uses), and bundles runnable Python/shell scripts it can author and execute in the same turn, so it takes on capabilities it did not ship with.
- **[Remote MCP services](https://saxman.info/aimu/how-to/use-mcp-tools/).** Kokua wires AIMU's `MCPClient` to config and to the agent. Connect a server with `--mcp <url>` or `[[mcp.server]]`, or let the assistant connect one itself with its `add_mcp_server` tool. OAuth is handled by posting the authorization link into the chat and persisting the tokens; bearer-token servers read their secret from an env var named by `token_env`, never from the config file.
- **[Every agent is declared, none is baked in](docs/how-to/set-up-toolsets.md).** One `[agents.<name>]` table per agent in `config.toml`, each with a `description`, a `system_message`, a `tools` list of toolset names, and a `delegates_to` list. `[assistant].agent` says which one you talk to. `kokua config init` writes `assistant`, `researcher`, `coder`, and `generalist`, and you edit, delete, or add to them freely. At least one agent is required, so a config with none is refused at startup rather than run as an assistant that can do nothing. The shipped tables make the agent you talk to a **lean supervisor** -- the cross-cutting toolsets it needs to manage itself, no domain tools, and everything specialized delegated -- which keeps the always-on agent's tool context small. That is what the config declares, not a rule in the code: give it `"compute"` and it runs Python itself.
- **[Sub-agents](https://saxman.info/aimu/how-to/spawn-subagents/).** A non-empty `delegates_to` is itself the declaration: that agent gets AIMU's `spawn_subagent(agent_type, task)` offering exactly the agents it names, each cloning the active model with the tools its own table declares. Delegation nests, since an agent you delegate to that delegates in turn gets its own menu of its own targets; the graph has to be acyclic and a cycle is a startup error that prints the path. A worker's approval-gated call is routed to you rather than run unattended. Independent spawns in one turn run concurrently. In the web UI each spawn gets its own foldable card in the conversation that spawned it, holding the sub-agent's thinking, tool calls, and live-streamed answer as the same collapsible blocks the assistant's own turn uses (nested reasoning follows `show_thinking`, nested tool calls follow `show_tools`), and the card replays on reload.
- **One namespace, and nothing is implicit.** Every capability an agent can hold is a named toolset in a single namespace: AIMU's built-in tool groups, Kokua's own memory / documents / skills / config / MCP admin / scheduling / conversation capabilities, each installed plugin toolset, and each configured MCP server by its `name`. `kokua --list-toolsets` prints the lot, grouped by what provides it. **A capability is declared, never defaulted:** no code path adds a tool an agent did not name, not even the clock, so an installed toolset or a connected server reaches nothing until some agent's `tools` list names it. An unknown name is a startup error listing the valid ones, and two providers claiming one name is one too.
- **Persistent memory.** AIMU's [semantic store](https://saxman.info/aimu/how-to/use-semantic-memory/) for facts and [document store](https://saxman.info/aimu/how-to/use-document-memory/) for text, shared across every agent that declares the `memory` and `documents` toolsets and surviving restarts. Both live under [`data/`](#state); drop those names from an agent's `tools` and it has neither, and drop them from every agent and no store is opened at all.

### Proactive work

- **[Scheduled tasks](src/kokua/scheduling/).** Ask for something on a schedule ("every weekday at 9am, summarize my calendar") and the assistant persists it to `data/scheduled_tasks.json` via its own `schedule_task` / `list_scheduled_tasks` / `cancel_scheduled_task` tools; it survives restarts. Schedules are one-shot, interval, daily, or weekly.
- **Where a task runs, and how much it keeps.** Every firing opens its own chat, nested under the task in the sidebar. Ask for a number and that task keeps only its newest N runs, deleting the older ones as it goes; 1 means each run replaces the last, 0 keeps everything. A task that names no number follows `[scheduling] max_task_conversations` (3 by default). Nothing is deleted until a firing succeeds, so a failed run never costs you the last good one.
- **Pause, resume, dry-run.** `disable_scheduled_task` stops a task firing while keeping it; `enable_scheduled_task` resumes it; `run_scheduled_task` fires it now without touching its schedule, reproducing exactly what the scheduled run would do. Scheduled runs auto-deny the approval-gated tools, since nobody is present to approve them.
- **Seeing them.** The web sidebar carries a tasks section listing each task's schedule and next firing, with pause/resume, run-now, and delete on each row, and that task's own conversations nested under it (so the chat list above holds only conversations you started). It hides when you have no tasks and collapses when you do. Writing and editing tasks stays in chat.
- **Edit a task in place.** `get_scheduled_task` shows one task in full, prompt included; `update_scheduled_task` revises any subset of its fields, keeping its id, its past runs, and everything you leave out. Ask to change a task's wording or its time and it edits that task rather than rewriting it from memory.

### Deep planning

- **[Plan before doing](src/kokua/workflows/planning/).** When you ask for it, the assistant drafts an explicit plan first -- which tools, skills, and MCP services it will use, what it will search for, where it needs to build a skill or connect a server -- then carries it out. Planning is per request, not a global mode: use the **Plan** toggle beside the message box, or send `/plan <task>` (which also works in the CLI).
- **Human review.** Enable *Review the plan before executing* to pause a planned turn for your Approve / Edit / Reject; otherwise the plan runs automatically.
- **Adversarial review.** An independent reviewer agent with no conversation context can critique the plan (Kokua re-plans on rejection) and/or the final answer before you see it (Kokua revises, up to `[planning].review_rounds`). Both are off by default and combine with human review, whose prompt shows you the critique. Reviewing the result means the answer cannot stream live: the agentic loop still streams, but the answer appears only once it passes.
- **Reviewers check their claims.** Each reviewer is a tool-using agent that runs a bounded assessment over a curated verification toolset (current date/time, web lookup, arithmetic) before returning a verdict, so it can confirm recency and numeric claims instead of rejecting what it cannot verify from the request alone. The toolset excludes your memory, documents, skills, and MCP servers, keeping the reviewer an independent critic with no access to your state, and excludes `execute_python` because a reviewer cannot be approval-gated (see [Security](#security)).
- **Show all reasoning.** Turn it on for the full trace: every LLM call in a planned turn (planner, each reviewer, executor, each revision) streams live under a labeled phase header, and every intermediate version is shown. This overrides result review's hide-until-vetted gate; only the final answer is saved. The raw trace is recorded per turn, so reloading replays exactly what you saw rather than a summary.

### Files and media

- **[Images in and out](src/kokua/images.py).** Attach an image and the assistant reads it (needs a [vision-capable model](https://saxman.info/aimu/reference/model-matrix/)): the composer's paperclip or a paste in the web UI, `/attach <path>` in the CLI. It can also [generate images](https://saxman.info/aimu/how-to/generate-images/) when [`$AIMU_IMAGE_MODEL`](https://saxman.info/aimu/reference/env-vars/) is set (e.g. `gemini:nano-banana`, or a HuggingFace diffusers `hf:<repo>`); without it, no generation tool is offered at all. Images live in `data/images/` and are served at `/images/<name>`; a conversation stores only a short reference, so `sessions.json` stays compact.
- **[PDFs](skills/markdown-to-pdf/).** The `markdown-to-pdf` skill renders Markdown to a PDF in `data/downloads/`, handing back the `/download/<name>` link the web UI serves. Install it with `kokua skills install markdown-to-pdf` and name it in an agent's `tools`. It ships as a skill rather than a toolset because its script declares `fpdf2` and `markdown` inline (PEP 723) and `uv` resolves them per run, so neither is a Kokua dependency. Give it to an agent that also has `fs` and `compute`, which is what runs the script.
- **[AIMU agents](src/kokua/toolsets/aimu_agents.py).** The `aimu_agents` toolset mounts AIMU's prebuilt orchestrators -- `code_review`, `research_report`, `create_content` -- and is the worked example of wiring an agent built with AIMU into Kokua: any `Runner` exposes `.run(task) -> str`, so a toolset is the whole bridge and the core learns nothing new. Nothing is mounted until you ask for it: name it in an agent's `tools` in `config.toml`. They are synchronous, so a nested run gets no sub-agent card, no `/stop`, and no approval gate on its workers; and an agent declaring `fs` + `compute` is a stronger reviewer than the tool-less `CodeReviewAgent`. Copy the shape, not necessarily the agents.
- **[Email](skills/email-report/).** The `email-report` skill mails information to you -- digests, summaries, reports -- written in Markdown and delivered as formatted HTML with a plain-text fallback, optionally attaching files already in `data/downloads/` or `data/images/`. It can only email **you**: the address comes from the host's configuration and the script has no recipient flag at all. Configure `[email]` (`host`, `port`, `from`, `to`, `use_ssl`) and put the password in `$KOKUA_EMAIL_PASSWORD`, never in the config file (for Gmail, an App Password). Kokua passes those settings to the script's environment, so the script never re-derives your config. Without host, `to`, and the password it sends nothing and says so. Install with `kokua skills install email-report`, name it in an agent's `tools`, and give that agent `fs` and `compute` so it can run the script.

## Configuration

Settings come from a TOML file, so you don't repeat flags. Precedence, highest first: **command-line flag > config file > built-in default**. The file is read from `--config <path>`, else `$KOKUA_CONFIG`, else `$KOKUA_HOME/config.toml` (default `~/.kokua/config.toml`).

**The file is required.** Kokua will not start without one, and will tell you to run `kokua config init`. The `[agents.*]` tables exist nowhere else and the assistant cannot work without at least one agent, so there is no useful "no config" state to fall back to. Every individual *key* still has a built-in default, so you set only what you want to change.

```bash
kokua config init           # writes the documented example; --force to overwrite
```

The scaffold comments every key at its default, so changing a default in a later release still reaches keys you left commented, and it ships the four `[agents.*]` tables live rather than commented, since an agent is the one thing Kokua cannot default. See [`config.example.toml`](src/kokua/config.example.toml) for the full set, and [set up a toolset](docs/how-to/set-up-toolsets.md) for the walkthrough of declaring an agent.

An agent table is four keys:

```toml
[assistant]
agent = "assistant"          # which table below is the agent you talk to

[agents.assistant]
description = "The assistant the user talks to."
system_message = "You are a personal assistant running on the user's own machine. Be concise and helpful."
tools = ["memory", "documents", "skills", "config", "mcp-admin", "scheduling", "conversations", "planning", "time"]
delegates_to = ["researcher", "coder", "generalist"]
```

`config.toml` is also **app-written**: the settings panel, the assistant's own `update_config` tool, and a runtime `add_mcp_server` all write back to it, with your comments preserved. There is no second settings store. Four things in it are hand-edit only and refused by `update_config`: `[security] confirm_tools`, `[email] to`, `[paths] data_dir`, and the whole `[agents.*]` section, so which capability an agent holds stays your decision.

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
- **Toolsets** (`kokua.toolsets` group): one named capability an agent can declare. A toolset is a `kokua.plugins.Toolset` whose `build(ctx)` returns [`@aimu.tool`](https://saxman.info/aimu/how-to/add-custom-tool/) callables, plus an optional `guidance` string appended to the prompt of any agent holding it. Installing it puts the name in the namespace; an agent still has to declare it.

The built-in `cli` / `web` front ends and Kokua's two built-in toolsets are registered exactly this way in Kokua's own `pyproject.toml` -- if the built-in path and the plugin path ever diverge, the plugin path is the broken one. To add your own from another package:

```toml
# in your package's pyproject.toml
[project.entry-points."kokua.toolsets"]
weather = "my_weather_pack:TOOLSET"
```

`pip install` it, and `kokua --list-toolsets` shows it. Its tools do **not** appear automatically: name it in an agent's `tools` list in `config.toml`, since a capability is declared and never defaulted. See [`toolsets/image.py`](src/kokua/toolsets/image.py) for the template (one tool, and a `build` that returns nothing when its prerequisite is missing), and [`toolsets/aimu_agents.py`](src/kokua/toolsets/aimu_agents.py) for the same shape carrying a whole AIMU agent rather than a plain function. For something simpler than a toolset, [add a skill](docs/how-to/add-skills.md) instead: a directory with a script needs no packaging at all. [Set up a toolset](docs/how-to/set-up-toolsets.md) is the full walkthrough: what is in the namespace, how an agent declares its `tools` and `delegates_to`, and exactly which mistakes fail at startup.

## Security

Kokua can author and run Python/shell scripts as **real subprocesses with your user privileges (no sandbox)**, and connect to remote MCP servers and run whatever tools they expose. Real capability is the point of a personal assistant, but it means a prompt-injected or mistaken model can run arbitrary code on your machine and call arbitrary remote tools. Only run Kokua with a model, inputs, and MCP servers you trust. The CLI prints a notice on startup.

**Tool approval.** The riskiest tools require confirmation before each call -- a `y/N` prompt in the terminal, Allow/Deny buttons in the web UI. By default this gates `add_skill_script`, `add_mcp_server`, `execute_python`, and `update_config`. Adjust with `[security] confirm_tools` or `--confirm-tools name1,name2` (empty disables it). Gating is by tool name, so it applies to a sub-agent's call as much as the assistant's own: a worker's gated call is routed to you. Proactive and backgrounded turns auto-deny these regardless, so the assistant never runs a full-access tool unattended, and an approval prompt only ever appears for the conversation you are currently viewing.

**Capability is granted by hand.** `[agents.*]` decides what every agent can call, and it is the one section the assistant can never write: `update_config` refuses it outright, and the settings panel does not touch it. So the assistant can connect an MCP server, but it cannot give itself or a worker the server's tools: that takes an edit you make. Note the flip side, since it is easy to misread: there is no privilege tier among agents. An agent whose table declares `config` really does get `update_config`, and one declaring `compute` really does get `execute_python`. Your hand-edit is the consent, and `confirm_tools` is the gate at call time.

**Injection crosses conversations.** The assistant can read every saved conversation, and a transcript is untrusted text (a worker may have pasted web content into it), so an injection that lands in one conversation can influence what the assistant does in another. The three read tools are ungated by default, since they only read and gating them would make an unattended scheduled run that reads history fail silently; add `read_conversation` and `search_conversations` to `[security] confirm_tools` if you would rather approve each one.

**The reviewer needs no gate.** With adversarial review on, the reviewer is a tool-using agent, and an autonomous critic cannot pause to ask you mid-review. Rather than exempt it from the approval gate, its verification toolset holds nothing the gate exists to cover: web lookup, `calculate`, and the clock, with no `execute_python`, no memory or document access, and no MCP mutation. A test pins this against the shipped `confirm_tools` default, so adding a name there fails the suite until the reviewer's toolset is re-checked.

## Development

```bash
uv sync --all-extras                                  # installs the editable ../aimu + all extras
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                                      # mock-only: no model, network, or keys
uv run pytest -m e2e                                  # opt-in browser tests (playwright install chromium)
uv run --with build --with twine python -m build && uv run --with twine twine check --strict dist/*
```

That last line is what CI's `package` job runs before installing the wheel into a clean environment. An editable install imports straight from `src/`, so it cannot catch a resource missing from `[tool.setuptools.package-data]` or a console script pointing at a moved function; only a built wheel can.

`src/kokua/` is grouped by subsystem, and `tests/` mirrors it exactly:

```
core/         the transport-agnostic runtime: assistant, conversations, turns, interaction,
              settings_runtime, diagnostics, build, agent_registry, turn_gate, messages, errors
config/       the settings schema, the TOML file, the writers, the runtime-settings table
workflows/    the workflow protocol (the two tiers) and planning/, the /plan pipeline it carries
mcp/          remote MCP servers and their OAuth
scheduling/   recurrence math, the durable task registry, the agent-facing tools
channels/     ChannelUI plus the concrete channels
frontends/    cli, web        -- registered as plugins, exactly like a third party's would be
toolsets/     aimu_agents, image
```

Outside `src/`, the repository also carries `skills/`: Agent Skills Kokua ships as content rather than as
Python, so they are not in the wheel. `kokua skills install` copies them into your skills folder.

The stable public import surface is `kokua.plugins`, `kokua.config`, `kokua.core`, `kokua.channels.web`, and `kokua.images`. Everything else is internal and may move.

## Resources

### Kokua

- 📘 [How-to guides](docs/how-to/index.md): [set up a toolset](docs/how-to/set-up-toolsets.md) (the namespace, declaring an agent, writing a toolset) · [add a skill](docs/how-to/add-skills.md) · [add an MCP service](docs/how-to/add-mcp-services.md).
- 💡 [Design principles](docs/explanation/design-principles.md): the six that decide what belongs in the core, each with the code that backs it and the patterns it excludes.
- 🏗️ [Architecture](docs/explanation/architecture.md): module layout, control flow, and the concurrency model.
- ⚙️ [`config.example.toml`](src/kokua/config.example.toml): every setting, documented at its default.
- 🧩 [`toolsets/image.py`](src/kokua/toolsets/image.py): the toolset template.
- 🧩 [`skills/dice-roller/`](skills/dice-roller/): the skill template, for capability that needs no packaging.
- 📋 [CHANGELOG](CHANGELOG.md) · [TODO](TODO.md): what changed, and what's known but not yet scheduled.

### AIMU

Kokua is a thin application over [AIMU](https://saxman.info/aimu/), so most capability questions are really AIMU questions. The primitives Kokua wires together:

- 🔌 [Switch providers](https://saxman.info/aimu/how-to/switch-providers/): the `provider:model_id` string, default-model resolution, timeouts, and failover -- what `--model` accepts and how it resolves when you omit it.
- 🤖 [Personal-assistant primitives](https://saxman.info/aimu/how-to/build-personal-assistant/): the `Channel` transport, the `Scheduler`, and runtime skill authoring. The three pieces Kokua is built from.
- 🛠️ [Built-in tools](https://saxman.info/aimu/reference/api/tools/) · [custom tools](https://saxman.info/aimu/how-to/add-custom-tool/) · [MCP](https://saxman.info/aimu/how-to/use-mcp-tools/): what an agent's `tools` list and `--mcp` draw on.
- 🧠 [Semantic memory](https://saxman.info/aimu/how-to/use-semantic-memory/) · [documents](https://saxman.info/aimu/how-to/use-document-memory/) · [skills](https://saxman.info/aimu/how-to/use-skills/) · [sub-agents](https://saxman.info/aimu/how-to/spawn-subagents/): the capability layer.
- 📊 [Model matrix](https://saxman.info/aimu/reference/model-matrix/) · [environment variables](https://saxman.info/aimu/reference/env-vars/): which models support vision, tools, and reasoning, and the AIMU env vars Kokua inherits.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, where a new module goes, and PR conventions.

## License

Apache 2.0. See [LICENSE](LICENSE).
