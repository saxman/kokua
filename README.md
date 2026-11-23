<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/saxman/kokua/main/docs/assets/kokua-horizontal-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/saxman/kokua/main/docs/assets/kokua-horizontal-light.svg">
  <img alt="Kokua: understandable agents" src="https://raw.githubusercontent.com/saxman/kokua/main/docs/assets/kokua-horizontal-light.svg" width="275">
</picture>

**A personal AI assistant built to be understood and extended.**

[![PyPI](https://img.shields.io/pypi/v/kokua)](https://pypi.org/project/kokua/) [![CI](https://github.com/saxman/kokua/actions/workflows/ci.yml/badge.svg)](https://github.com/saxman/kokua/actions/workflows/ci.yml) ![GitHub License](https://img.shields.io/github/license/saxman/kokua) ![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsaxman%2Fkokua%2Frefs%2Fheads%2Fmain%2Fpyproject.toml) [![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv) [![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

[Docs](https://saxman.info/kokua/) · [How agents work](https://saxman.info/kokua/how-agents-work/) · [How-to](https://saxman.info/kokua/how-to/) · [Configuration](https://saxman.info/kokua/reference/configuration/) · [Architecture](https://saxman.info/kokua/explanation/architecture/) · [Changelog](https://saxman.info/kokua/changelog/)

</div>

**Kokua** (Hawaiian: *help, assistance*) is a real, always-on personal assistant, built so people can learn how agentic assistants work from one they actually use. It runs on the [AIMU](https://saxman.info/aimu/) library: it chats with you, writes and runs its own skills, connects to remote MCP services, delegates subtasks to isolated sub-agents, schedules its own work, and remembers facts and documents across conversations. Every one of those mechanisms is meant to be read, run, and extended.

```bash
kokua                      # chat in the terminal
kokua --frontend web       # or in the browser at http://127.0.0.1:8000
kokua agents               # what each agent can call, and what asks you first
kokua --list-toolsets      # every capability this install offers
```

It runs as a single user in a single process, and can run code and call remote services with your privileges (see [Security](#security)).

## Why Kokua

A toy cannot teach what real work costs, so Kokua does real work, and its machinery is meant to be followed rather than taken on faith. You **read it**: the core is small enough to hold in your head, and each module opens by saying why it is shaped as it is. You **run it**: reasoning, tool calls and their results, sub-agent cards, and plan phases are on screen by default, so you watch the loop instead of inferring it. You **extend it**: capability arrives through the same plugin seam Kokua's own capabilities use, so the code you read is the code you would write.

Six principles keep it that way: a transport-agnostic core, growth by plugin rather than core change, `config.toml` as the single source of settings (which the app itself writes), all state under one directory you own, one user and one process with the concurrency rules written down, and security that is explicit and under your control. They sit on top of [AIMU's own](https://saxman.info/aimu/explanation/design-principles/), and the reasoning behind each is on the [design principles](https://saxman.info/kokua/explanation/design-principles/) page.

## Install

```bash
uv tool install 'kokua[web]'      # or: pip install 'kokua[web]'
```

Kokua needs Python 3.14+ and [AIMU](https://github.com/saxman/aimu) 0.41.0 or newer, which installs with it. The `web` extra adds the browser front end; without it the CLI works alone.

The bundled skills that `kokua skills install` copies ship with the repository, not the package. To get them, or to read and change the code while you run it, install from a checkout:

```bash
git clone https://github.com/saxman/kokua && cd kokua
uv sync --all-extras --no-sources   # AIMU from PyPI; see CONTRIBUTING to develop both together
```

## Quick start

```bash
kokua config init                   # scaffold ~/.kokua/config.toml, every key documented
kokua --model ollama:qwen3:8b
```

`--model` takes AIMU's [`provider:model_id`](https://saxman.info/aimu/how-to/switch-providers/) string, so any provider AIMU supports works. Leave it out and AIMU [resolves a default](https://saxman.info/aimu/how-to/switch-providers/#use-whatever-model-is-already-available-locally): `$AIMU_LANGUAGE_MODEL` if set, else the first running local model it finds. It never picks a cloud model for you.

In a conversation:

- `/new`, `/conversations`, `/switch <id>`: start, list, and move between conversations. Switching never cancels a reply that is still running.
- `/stop`: cancel a streaming reply and keep the partial turn.
- `/plan <task>`: draft a plan before acting on it.
- `/think <level>`: set the reasoning effort for the messages that follow.
- `/diag`: report the running turn, the turn gate, and a stuck turn's stack. It answers even when a turn hangs.

Logs rotate under `~/.kokua/data/logs/`, and `kill -USR1 <pid>` dumps every thread's stack there.

## Start exploring

1. **[How agents work](https://saxman.info/kokua/how-agents-work/)**: the turn loop, tool calling, declared capability, context and memory, delegation, and agent messaging, each shown in a real captured run before the code that did it.
2. **Read the core**, in this order: [`core/assistant.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/assistant.py) (which AIMU primitives an assistant is made of), [`core/turns.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/turns.py) (what a turn is; it opens with ten concurrency invariants, each naming the bug it prevents), [`registry/registry.py`](https://github.com/saxman/kokua/blob/main/src/kokua/registry/registry.py) then [`toolsets/image.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/image.py) (how a capability becomes a tool), [`channels/ui.py`](https://github.com/saxman/kokua/blob/main/src/kokua/channels/ui.py) (how a core that knows no transport still renders richly), and [`workflows/planning/runner.py`](https://github.com/saxman/kokua/blob/main/src/kokua/workflows/planning/runner.py) (a loop with more structure than chat). `tests/` mirrors `src/kokua/`, so a module's tests are its second explanation.
3. **[Architecture](https://saxman.info/kokua/explanation/architecture/)**: the whole map, and the concurrency model.
4. **Extend it**: [`toolsets/image.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/image.py) is a complete toolset, and [`skills/dice-roller/`](https://github.com/saxman/kokua/tree/main/skills/dice-roller/) is the same idea with no packaging. [Set up a toolset](https://saxman.info/kokua/how-to/set-up-toolsets/) walks through both.

## What people run it for

None of these is a mode. Each is the same declared machinery pointed at a different job.

- **A market watch.** *"Every weekday at 4:30, check my positions, flag anything that moved more than 3%, and email me the digest."* A scheduled task fans out to `researcher` sub-agents, one per ticker, delivers through the [`email-report`](https://github.com/saxman/kokua/tree/main/skills/email-report/) skill, and can have an independent reviewer check its numbers first. A scheduled run denies every gated tool, so it reaches only what you granted.
- **A job hunt.** *"Here is the posting. Tailor my resume and draft the cover letter."* [jobme](https://github.com/saxman/jobme) is a separate package that registers a Kokua toolset through the same entry point Kokua's own 23 use. [Install a third-party toolset](https://saxman.info/kokua/how-to/install-a-third-party-toolset/) uses it as the worked example.
- **A machine that looks after itself.** *"Check disk every morning, and back yourself up nightly."* Running commands and writing files ask first, while [`github_backup`](https://saxman.info/kokua/how-to/back-up-to-github/) takes no arguments at all, which is what lets it run ungated on a schedule.
- **A reading desk that remembers.** *"Read these six papers and tell me where they disagree."* Search your own folders by passage, keep facts in semantic memory, and pull in past conversations. The [`synthesize-documents`](https://github.com/saxman/kokua/tree/main/skills/synthesize-documents/) skill reads past the first page of each paper.

## Key features

### The turn loop, in the open

- **The loop is the interface.** A turn arrives as typed frames: reasoning, each tool call and its result, a card per sub-agent, a header per plan phase. Each frame degrades once, in `ChannelUI`, so the CLI shows the same loop with less decoration.
- **Turns run in the background.** A turn is an AIMU `RunHandle` while the channel keeps reading, so `/stop` cancels mid-stream and an Allow/Deny click reaches the tool call waiting on it.
- **Redirect a turn mid-flight.** A message typed while the assistant works reaches the running turn, and its workers, at the next model call, instead of queueing behind it.
- **Agents can message each other** over the same per-turn bus, by address, by label, or to everyone. [Agent messaging](https://saxman.info/kokua/how-agents-work/agent-messaging/) shows which directions can carry anything, and why.
- **Switching conversations does not cancel a turn.** Each conversation owns its agent: a reply you walk away from finishes, saves, and alerts you, and switching back catches you up.
- **See what the model saw.** In the web UI each model call has a `{ }` button that opens the exact request and response, downloadable as JSON.
- **Tool calls, live and named.** A card appears when a tool starts and fills in with its result, shown as plain text because a tool result is untrusted. Each call is named `toolset.tool`, the same name you grant or gate.
- **Transcripts replay and export.** Reloading replays reasoning and tool calls, and [`kokua export`](https://saxman.info/kokua/how-to/export-a-conversation/) writes a conversation to Markdown, even with the model server down.

### Tools, and how an agent gets one

- **A capability is declared, never defaulted.** Every capability is a named toolset in one namespace (AIMU's tool groups, Kokua's own, plugins, skills, and MCP servers), and an agent holds exactly what its `available` list names. Even `available = "all"` is written down. [Tool access](https://saxman.info/kokua/explanation/tool-access/) explains why.
- **Load on demand.** An agent's `resident` list says which toolsets are in context from the first turn, and it loads the rest with `load_tools` when a turn needs them. That cuts the shipped entry agent's tool block by 46.8%.
- **[Self-authored skills](https://saxman.info/aimu/how-to/use-skills/).** The assistant writes and revises `SKILL.md` skills with runnable scripts, so it takes on capabilities it did not ship with.
- **[Remote MCP services](https://saxman.info/kokua/how-to/add-mcp-services/).** Connect a server by flag, by config, or with the assistant's own `add_mcp_server` tool. OAuth links arrive in the chat, and bearer tokens come from an environment variable, never the config file.

### Agents and delegation

- **[Every agent is declared](https://saxman.info/kokua/how-to/set-up-toolsets/).** One `[agents.<name>]` table each: a description, a system message, and optionally its own model, reasoning effort, iteration cap, generation parameters, tools, and `delegates_to`. Nothing is inherited down the delegation graph. `kokua config init` writes four agents: `assistant`, `researcher`, `coder`, and `introspector`.
- **[Sub-agents](https://saxman.info/aimu/how-to/spawn-subagents/).** A non-empty `delegates_to` gives an agent `spawn_subagent` over exactly those agents. Delegation nests (a cycle fails at startup), spawns run concurrently, and each gets its own card in the web UI.
- **Composed on the spot.** When no declared role fits, `compose_subagent` builds a worker with exactly the capabilities a task needs, then discards it.
- **Effort per message.** The composer's **Think** picker or `/think` sets the reasoning effort for the messages you send, without touching `config.toml`.

### Memory and context

- **Persistent memory.** AIMU's semantic store for facts and document store for text, shared across conversations and agents, and kept as plain files under `~/.kokua/data/`.
- **Your folders, searched by passage.** Declare a folder (an Obsidian vault, say) as a `[[documents.collection]]` and `search_documents` ranks passages across it with hybrid keyword and embedding search, citing file and heading. [Retrieval](https://saxman.info/kokua/explanation/retrieval/) explains the machinery.
- **Context is a budget.** Every tool schema is resent on every model call, so the shipped agent you talk to is a lean supervisor that hands specialized work to workers with fresh contexts.
- **It looks things up.** Anything that could have changed since training (prices, releases, who holds a role) goes to the web or a worker, "even when you think you know".

### Knowing and changing itself

- **It reads and analyzes its own history.** `search_conversations` answers "what did we decide last week?", and `export_conversation` hands the `introspector` sub-agent a whole run to evaluate.
- **It names its own gaps.** `list_capabilities` lists what is installed but not held, which is what `compose_subagent` fills.
- **It changes its own settings, below a ceiling.** `update_config` edits `config.toml` and keeps your comments, but cannot touch locked keys such as `[agents.*]`, so it never widens its own reach.
- **It times its own model.** `benchmark_model` reports time to first token and decoding speed separately.

### Planning and self-review

- **[Plan before doing](https://github.com/saxman/kokua/tree/main/src/kokua/workflows/planning/).** The **Plan** toggle or `/plan <task>` drafts a plan first, which you can approve, edit, or reject.
- **Adversarial review.** An independent reviewer with no conversation context can critique the plan and the final answer, checking dates and numbers with its own small, read-only toolset.
- **Show all reasoning** streams every model call in a planned turn under a labeled phase.

### Acting without you

- **[Scheduled tasks](https://github.com/saxman/kokua/tree/main/src/kokua/scheduling/).** Ask for something on a schedule and the assistant writes a `[scheduling.task.*]` table you can read and edit. Tasks run once, at an interval, daily, or weekly; each firing gets its own conversation, and gated tools are denied since nobody is there to approve them.
- **Managed in place.** Pause, resume, run now, stop a run, or edit a task, from the assistant or the sidebar.

### Reaching outside the chat

- **Images in and out.** Attach an image for a vision model. Set `$AIMU_IMAGE_MODEL` and the assistant can generate and refine images, and see what it made.
- **PDFs and email**, as skills: [`markdown-to-pdf`](https://github.com/saxman/kokua/tree/main/skills/markdown-to-pdf/) and [`email-report`](https://github.com/saxman/kokua/tree/main/skills/email-report/), which can only email you.
- **[AIMU agents](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/aimu_agents.py)** mounts AIMU's prebuilt orchestrators as tools, the worked example of bringing an AIMU agent into Kokua.
- **[Backup](https://saxman.info/kokua/how-to/back-up-to-github/)** commits Kokua's own state to a private GitHub repository.

### In the browser

- A conversation sidebar with model-written titles, rename, duplicate, and export, and your scheduled tasks below it with their runs.
- Each turn can be branched into a new conversation, retried, or deleted along with everything after it.
- Replies render as Markdown while they stream, and LaTeX once the turn completes. The page reconnects on its own when the server restarts.

## Configuration

Settings live in `~/.kokua/config.toml` (or wherever `--config`, `$KOKUA_CONFIG`, or `$KOKUA_HOME` points). A command-line flag beats the file, which beats the built-in default. The file is required, because agents exist nowhere else: `kokua config init` writes one with every key documented.

```toml
[assistant]
agent = "assistant"          # which table below is the agent you talk to

[agents.assistant]
description = "The assistant the user talks to."
system_message = "You are a personal assistant running on the user's own machine. Be concise and helpful."
delegates_to = ["researcher", "coder", "introspector"]

[agents.assistant.tools]
available = ["memory", "documents", "skills", "config", "mcp", "scheduling", "conversations", "planning", "capabilities", "time", "benchmark", "messaging"]
resident = ["memory", "config", "conversations", "planning", "capabilities", "time", "messaging"]   # the rest load on demand
```

The app writes this file too (`update_config`, `add_mcp_server`), keeping your comments. `[security] locked_config_keys` lists what the assistant may not change, and `[agents.*]` is on it by default. The [configuration reference](https://saxman.info/kokua/reference/configuration/) covers every key.

All state lives under `~/.kokua`:

```
~/.kokua/
  config.toml            # all settings, hand-edited and app-written
  data/
    sessions.json        # conversations
    skills/              # authored skills
    memory/              # semantic facts
    documents/           # saved documents
    index/               # document search indexes, rebuilt from your folders
    downloads/           # generated files, served at /download
    images/              # uploaded and generated images, served at /images
```

## Extending Kokua

Front ends and toolsets are plugins, discovered through the `kokua.frontends` and `kokua.toolsets` entry-point groups. Kokua's own register exactly the same way.

```toml
# in your package's pyproject.toml
[project.entry-points."kokua.toolsets"]
weather = "my_weather_pack:TOOLSET"
```

`pip install` it and `kokua --list-toolsets` shows it, but no agent holds it until an `available` list names it. For something smaller, [add a skill](https://saxman.info/kokua/how-to/add-skills/): a directory with a script needs no packaging. [Set up a toolset](https://saxman.info/kokua/how-to/set-up-toolsets/) is the full walkthrough.

## Security

Kokua runs scripts as real subprocesses with your privileges (no sandbox) and calls whatever tools a connected MCP server exposes. A prompt-injected or mistaken model can do real damage, so run it only with models, inputs, and servers you trust.

- **Risky tools ask first.** Each agent's approval policy (`confirm` and `review` in `[assistant.tools]`, or its own table) gates calls by `toolset.tool`. The shipped default gates running code, writing files and documents, editing config, and adding skill scripts or MCP servers. An entry that would gate nothing fails startup, and an unattended turn denies gated calls outright.
- **A reviewer can approve for you, off by default.** `[security.auto_approval]` lets a model reviewer turn a prompt into an approval. It cannot deny, and it never approves the three tools that grant capability. Read [Auto-approval](https://saxman.info/kokua/explanation/auto-approval/) before switching it on.
- **Capability is granted by hand.** `[agents.*]` is locked against `update_config`, so the assistant cannot widen its own reach. Reading and writing files are separate toolsets, so you can grant one without the other.
- **Injection crosses conversations.** The assistant can read every saved conversation, so injected text in one can influence another. Gate the `conversations` tools if that matters to you.

[Tool access](https://saxman.info/kokua/explanation/tool-access/) explains the whole model.

## Development

```bash
uv sync --all-extras
uv run ruff check . && uv run ruff format --check .
uv run pytest -q              # mock-only: no model, network, or keys
uv run pytest -m e2e          # opt-in browser tests
```

[Contributing](https://saxman.info/kokua/contributing/) covers working against a sibling AIMU checkout, where a new module goes, and the conventions. The stable public import surface is `kokua.plugins`, `kokua.config`, `kokua.core`, `kokua.channels.web`, and `kokua.images`; everything else may move.

## Resources

- 🧭 [How agents work](https://saxman.info/kokua/how-agents-work/): the mechanisms an agentic system is made of, each shown in a captured run.
- 📘 [How-to guides](https://saxman.info/kokua/how-to/): set up a toolset, add a skill, add an MCP service, back up to GitHub, export a conversation.
- 💡 [Design principles](https://saxman.info/kokua/explanation/design-principles/) and [architecture](https://saxman.info/kokua/explanation/architecture/): why Kokua is shaped as it is, and how.
- ⚙️ [Configuration reference](https://saxman.info/kokua/reference/configuration/): every key, what it accepts, and who may write it.
- 🔌 AIMU: [switch providers](https://saxman.info/aimu/how-to/switch-providers/) · [personal-assistant primitives](https://saxman.info/aimu/how-to/build-personal-assistant/) · [tools](https://saxman.info/aimu/reference/api/tools/) · [model matrix](https://saxman.info/aimu/reference/model-matrix/) · [environment variables](https://saxman.info/aimu/reference/env-vars/).
- 📋 [Changelog](https://saxman.info/kokua/changelog/) · [Issues](https://github.com/saxman/kokua/issues).

## Contributing

See the [contributing guide](https://saxman.info/kokua/contributing/) for setup, checks, and conventions.

## License

Apache 2.0. See [LICENSE](https://github.com/saxman/kokua/blob/main/LICENSE).
