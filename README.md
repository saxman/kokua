<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/kokua-horizontal-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/kokua-horizontal-light.svg">
  <img alt="Kokua: understandable agents" src="docs/assets/kokua-horizontal-light.svg" width="275">
</picture>

**A personal AI assistant built to be understood and extended.**

[![CI](https://github.com/saxman/kokua/actions/workflows/ci.yml/badge.svg)](https://github.com/saxman/kokua/actions/workflows/ci.yml) ![GitHub License](https://img.shields.io/github/license/saxman/kokua) ![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsaxman%2Fkokua%2Frefs%2Fheads%2Fmain%2Fpyproject.toml) [![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv) [![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

[Design principles](docs/explanation/design-principles.md) · [Architecture](docs/explanation/architecture.md) · [Configuration](docs/reference/configuration.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

</div>

**Kokua** (Hawaiian: *help, assistance*) exists so people can learn how agentic systems work. It is a hackable, modular personal-assistant application (OpenClaw / Hermes Agent style) built on the [AIMU](https://saxman.info/aimu/) library, and a real one rather than a demo: an always-on assistant that chats with you, authors and runs its own skills, connects to remote tool services, delegates independent subtasks to isolated sub-agents, schedules its own proactive work, and remembers facts and documents across conversations. Every one of those mechanisms is there to be read, run, and extended.

Kokua **extends itself**: it writes and runs new skills to take on capabilities it didn't ship with, and grows its reach by connecting to remote MCP services on its own. Where it can't extend itself, you extend it: front ends and toolsets are **plugins** you add by installing a package, never by editing the core.

```bash
kokua                              # chat in the terminal
kokua --frontend web               # or a browser UI at http://127.0.0.1:8000
kokua --list-toolsets              # see what capability is installed
kokua config init                  # scaffold ~/.kokua/config.toml, every key documented
```

It runs as a single user in a single process, and can run code and connect to remote services with your privileges (see [Security](#security)).

## Why Kokua exists

Kokua exists so people can learn how agentic systems work. It is a real assistant rather than a demo, because a toy cannot teach what real work costs, and it is designed to be understood: the machinery is meant to be followed, not taken on faith. There are three ways in.

### Read it

The core is small enough to hold in your head, and each module opens by saying why it is shaped the way it is rather than restating what it does. A reading order:

| Start here | What it teaches |
| --- | --- |
| [core/assistant.py](src/kokua/core/assistant.py) | The composition root and the serve loop. Which AIMU primitives an assistant is actually made of, and how they are wired together. |
| [core/turns.py](src/kokua/core/turns.py) | What one turn is, reactive and proactive. It opens with seven concurrency invariants, each naming the bug it prevents. |
| [registry/registry.py](src/kokua/registry/registry.py), then [toolsets/image.py](src/kokua/toolsets/image.py) | How a capability becomes a tool an agent holds: one flat namespace, then the smallest complete toolset in the repo. |
| [channels/ui.py](src/kokua/channels/ui.py) | How a core that knows no transport still renders richly. Every optional frame is degraded once, at construction. |
| [workflows/planning/runner.py](src/kokua/workflows/planning/runner.py) | An agentic loop with more structure than chat: draft a plan, review it, execute it, review the result. |

Then [architecture](docs/explanation/architecture.md) for the whole map, and [design principles](docs/explanation/design-principles.md) for why the boundaries fall where they do. `tests/` mirrors `src/kokua/` exactly, so a module's tests are its second explanation.

### Run it

```bash
kokua --frontend web
```

Reasoning, tool calls, tool results, sub-agent cards, and plan phases are all visible by default: the core streams every one of them and leaves it to a front end to decide how to draw them. You watch the loop instead of inferring it: what the model was thinking, which tool it chose, what arguments it passed, what came back, and what it did next. Ask for something on a schedule and watch the task fire on its own. Send `/plan <task>` and the assistant shows you the plan it drafted before it acts on it; turn on adversarial review and *Show all reasoning* (see [Planning and self-review](#planning-and-self-review)) and every call in that turn, planner through reviewer through executor, streams under its own labeled phase. `/diag` reports the in-flight turn and the gate state even when a turn is stuck, and `kokua --list-toolsets` prints every capability this install can offer, grouped by what provides it.

The entire state of a running assistant is plain files under `~/.kokua`, so you can read what it remembers while it is still running.

### Extend it

Capability arrives through the same seam Kokua's own capabilities use, so the code you read is the code you would write. [`toolsets/image.py`](src/kokua/toolsets/image.py) is one tool and a `build()` in 85 lines, registered through the same entry point a third party's package would use. [`skills/dice-roller/`](skills/dice-roller/) is the same idea with no packaging at all: a `SKILL.md` and a script. [Set up a toolset](docs/how-to/set-up-toolsets.md) is the full walkthrough. Nothing in the core changes for either.

### What keeps it that way

Kokua is small on purpose. Six principles decide what belongs in the core: a **transport-agnostic core** that knows a channel rather than a terminal or a socket, **growth by plugin** rather than core change, **`config.toml` as the single source of settings** that the app itself writes, **all state under one directory you own**, **a single user and one process with concurrency rules written down**, and **security that is explicit and under your control**. The first two keep Kokua readable, the next two keep it observable, the fifth keeps it runnable by anyone who clones it, and the last keeps its capability yours to bound. These sit on top of, and inherit, [AIMU's six library-level principles](https://saxman.info/aimu/explanation/design-principles/). The reasoning behind each of Kokua's, and the patterns each one excludes, is on the [design principles](docs/explanation/design-principles.md) page; the shape that falls out of them is in [architecture](docs/explanation/architecture.md).

The practical consequence: the assistant core is a few hundred lines that knows nothing about terminals or browsers, and adding a transport or a capability means shipping a package, not patching Kokua.

## Install

Kokua needs Python 3.11+ and [AIMU](https://github.com/saxman/aimu) 0.23.0 or newer.

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
> The `aimu>=0.23.0` requirement governs the PyPI path only: uv installs a path source without checking it against the specifier, so a sibling checkout is not constrained by it. If yours falls behind, startup says so and names the fix rather than failing on an import.

## Quick start

```bash
kokua --model ollama:qwen3:8b
```

The model string is AIMU's [`provider:model_id`](https://saxman.info/aimu/how-to/switch-providers/) form, so any provider AIMU supports works here. Omit `--model` and AIMU [resolves a default](https://saxman.info/aimu/how-to/switch-providers/#use-whatever-model-is-already-available-locally): [`$AIMU_LANGUAGE_MODEL`](https://saxman.info/aimu/reference/env-vars/) if set, else the first already-running local model found (Ollama, then a local OpenAI-compatible server). A cloud model is never auto-selected, and startup fails with an actionable message if nothing resolves. Chat at the prompt; Ctrl-D exits.

Useful flags: `--list-toolsets` (every capability name this install accepts, grouped by what provides it), `--mcp <url>` (repeatable), `--system` (overrides the entry agent's system message for this run only; a worker's own declared message is untouched), `--config <path>`, `--host` / `--port` (web). What each agent can *do* is not a flag: it is that agent's `tools` list in `config.toml` (see [Configuration](#configuration)).

Two commands worth knowing: **`/stop`** cancels a reply that's still streaming and keeps the partial turn, so the conversation continues (the web UI has a Stop button for the same). **`/diag`** reports the in-flight turn, the gate state, and a stuck turn's async stack; it never takes the turn gate, so it answers even when a hung turn is holding it. Rotating logs go to `$KOKUA_HOME/data/logs/kokua.log`, and `kill -USR1 <pid>` dumps all thread stacks there.

## Key features

### The turn loop, in the open

- **The loop is the interface.** A turn arrives as a sequence of typed frames rather than a finished answer: the model's reasoning, each tool call with its arguments, each result, a card per sub-agent, a header per plan phase. Those frames are optional, and every one of them degrades exactly once, in `ChannelUI`, to a documented fallback, so the CLI shows the same loop with less decoration rather than showing less of the loop.
- **A reactive turn runs as a background handle.** [`core/turns.py`](src/kokua/core/turns.py) opens with seven concurrency invariants, each naming the bug it prevents. The turn runs as an AIMU `RunHandle` while the channel keeps reading, and that one decision is what makes the rest work: **`/stop`** can cancel a reply mid-stream and keep the partial turn, and a web Allow/Deny click can reach the tool call still waiting on it.
- **Switching conversations does not cancel a turn.** Each conversation owns its own agent and model client, so a reply you walk away from keeps running: it persists to its own conversation, mutes its stream, and posts a dismissible notification when it lands. Switch back into one still working and it catches you up (the reasoning, tool calls, and answer so far, with a pulsing indicator and Stop in the composer) then keeps streaming from where it left off.
- **Tool calls show their results.** A tool card carries what the call returned as well as what it was called with, in a nested foldable labeled with the result's size and clamped behind a "show all" button. It renders as plain text rather than markdown, because a tool result is untrusted input: whatever a web page or an MCP server just returned is now in the model's context, and it does not get to be markup. Results show on live cards, on a background turn's catch-up, inside a sub-agent's card, and on reload, where a stored call is rejoined to its result by `tool_call_id` so a replayed card shows what the live one did.
- **Replayable transcripts.** Reloading replays the conversation, reasoning and tool calls included: what streamed live is what comes back. Auxiliary blocks (thinking, tool calls, phases, sub-agent cards, drafted plans) come back collapsed behind a one-line header carrying the call, its condensed arguments, and a result size, while messages and prompts stay open.

### Tools, and how an agent gets one

- **One namespace, and nothing is implicit.** Every capability an agent can hold is a named toolset in a single namespace: AIMU's built-in tool groups, Kokua's own memory / documents / skills / config / MCP admin / scheduling / conversation / capability-discovery capabilities, each installed plugin toolset, and each configured MCP server by its `name`. `kokua --list-toolsets` prints the lot, grouped by what provides it. **A capability is declared, never defaulted:** no code path adds a tool an agent did not name, not even the clock, so an installed toolset or a connected server reaches nothing until some agent's `tools` list names it. An unknown name is a startup error listing the valid ones, and two providers claiming one name is one too.
- **[Self-authored skills](https://saxman.info/aimu/how-to/use-skills/).** Built on AIMU's `SkillAgent`: the assistant writes `SKILL.md` skills (the same format Claude Code uses), and bundles runnable Python/shell scripts it can author and execute in the same turn, so it takes on capabilities it did not ship with.
- **[Remote MCP services](https://saxman.info/aimu/how-to/use-mcp-tools/).** Kokua wires AIMU's `MCPClient` to config and to the agent. Connect a server with `--mcp <url>` or `[[mcp.server]]`, or let the assistant connect one itself with its `add_mcp_server` tool. OAuth is handled by posting the authorization link into the chat and persisting the tokens; bearer-token servers read their secret from an env var named by `token_env`, never from the config file.

### Agents and delegation

- **[Every agent is declared, none is baked in](docs/how-to/set-up-toolsets.md).** One `[agents.<name>]` table per agent in `config.toml`, each with a `description`, a `system_message`, an optional `model` and `thinking`, a `tools` list of toolset names, and a `delegates_to` list. `[assistant].model` and `[assistant].thinking` are the model every agent runs on and the reasoning effort it runs at; an agent naming its own runs on that instead, so a lean supervisor and an expensive researcher need not share either. Resolution is per agent and never inherited, so a delegator that pins a model or reasons hard does not drag its workers along. `thinking` takes `false` (do not reason), `true` (reason at the model's own default), or `"low"` / `"medium"` / `"high"`; unset, nothing is sent and each model keeps its own behavior. `[assistant].agent` says which one you talk to. `kokua config init` writes `assistant`, `researcher`, and `coder`, and you edit, delete, or add to them freely. At least one agent is required, so a config with none is refused at startup rather than run as an assistant that can do nothing.
- **And you can change the effort for one message.** The composer's **Think** picker sets the reasoning effort for the messages you send after it (`off`, `low`, `medium`, `high`, or the configured default), and `/think <level>` does the same in the CLI. It is per request, not a setting: nothing is written to `config.toml`, and a reload returns to what the config declares. It reaches the agent you are talking to and nothing else, so a sub-agent still runs at the effort its own table names, and a planned turn ignores it entirely (the picker greys out while **Plan** is on). Each turn records the effort it actually ran at, so a transcript says how its answers were produced.
- **[Sub-agents](https://saxman.info/aimu/how-to/spawn-subagents/).** A non-empty `delegates_to` is itself the declaration: that agent gets AIMU's `spawn_subagent(agent_type, task)` offering exactly the agents it names, each running on its own `[agents.<name>].model` and `.thinking` (or the `[assistant]` defaults) with the tools its own table declares. Delegation nests, since an agent you delegate to that delegates in turn gets its own menu of its own targets; the graph has to be acyclic, and a cycle is a startup error that prints the path. Independent spawns in one turn run concurrently, and a worker's approval-gated call is routed to you rather than run unattended. In the web UI each spawn gets its own foldable card in the conversation that spawned it, holding the sub-agent's thinking, tool calls, and live-streamed answer as the same collapsible blocks the assistant's own turn uses, and the card replays on reload.
- **Sub-agents composed on the spot.** When no declared role fits, the assistant can build one, which is why the shipped config declares two specialists and no catch-all role: `compose_subagent` assembles a sub-agent carrying exactly the capabilities a task needs, runs the task on it, and discards it. This is the one code path that draws on the whole registry instead of a table, and it is still entered by declaration, since only an agent whose own `tools` names `capabilities` can do it at all. `[capabilities].max_depth` (default 3, `0` to switch composing off) bounds how far composition nests, and a composed sub-agent's gated calls are routed to you like any declared one's.
- **[Generation parameters](src/kokua/config.example.toml).** `[assistant.generation]` sets `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `repetition_penalty`, `max_tokens`, and `context_length` for every agent; an agent's own `[agents.<name>.generation]` overrides it per key, so naming only `temperature` still inherits the default's `context_length`. Only what you set is sent: AIMU layers this tier over a model card's own tuned sampling profile, so a key you leave out keeps the card's value. A parameter the backend cannot take is dropped with a warning naming the remedy, written to the rotating log rather than shown in the chat. Resolved once per agent, never inherited down the delegation graph, and read at startup only, like `model` and `thinking` above.

### Memory and context

- **Persistent memory.** AIMU's [semantic store](https://saxman.info/aimu/how-to/use-semantic-memory/) for facts and [document store](https://saxman.info/aimu/how-to/use-document-memory/) for text, shared across every conversation and every agent that declares the `memory` and `documents` toolsets, and surviving restarts. Both live under [`data/`](#state) as plain files you can read while the assistant is still running. Drop those names from an agent's `tools` and it has neither; drop them from every agent and no store is opened at all.
- **Every capability an agent declares is spent from its context.** A tool is a schema in the prompt on every turn, so the reach you grant an always-on agent is paid for in tokens before it does any work. That is why the shipped tables make the agent you talk to a **lean supervisor**: the cross-cutting toolsets it needs to manage itself, no domain tools, and everything specialized delegated to a worker whose context is built fresh for the task. Nothing in the code enforces this. It is what the config declares, and giving that agent `"compute"` makes it run Python itself.
- **What it will not answer from memory.** A model that believes it knows an answer never reaches for a tool, so the prompt names the trigger as a property of the *question* rather than as the model's own confidence, which is the signal it is worst at reporting: an answer that could have moved since training, or one you could check against a source (current events, prices, releases, published figures, who holds a role, what a page says today), goes to a worker or to the web tools "even when you think you know". Both halves are declared, not hardcoded behavior: the delegating agent's clause arrives with a non-empty `delegates_to`, and the look-it-up clause is the `web` toolset's own guidance, reaching whoever declares it.

### Knowing and changing itself

- **It can read its own history.** `list_conversations`, `read_conversation`, and `search_conversations` give the assistant read-only sight of every saved conversation, so it can answer "what did we decide about this last week?" and carry context out of a past thread into a sub-agent's task. A transcript comes back as bounded plain text (what was said, no reasoning or tool calls), oldest messages dropped first behind a count when it does not fit, which is the context window made visible as a rule you can read. Reads come from the last saved snapshot, so a conversation still streaming is flagged as having unsaved messages, and the one you are in is flagged too, since its current turn is not saved yet.
- **It can enumerate what it cannot do.** `list_capabilities` lists every capability installed on the machine other than capability discovery itself, whether or not the agent holds it. An agent that can name its own gaps is the precondition for `compose_subagent` filling one, and it is why "no tool for that" can become a composed sub-agent instead of a refusal.
- **It can rewrite its own settings, up to a ceiling it cannot raise.** `read_config` returns the live `config.toml` so the assistant can diagnose a configuration problem before touching it, and `update_config` changes one setting, preserving your comments. The distinction it has to reason about is hot versus cold: a display or planning flag applies to the next turn, everything else (the model and the reasoning effort among them) is saved and waits for a restart, and the tool says which happened so the assistant knows a saved setting is not yet in force. A model string is checked against the providers actually installed before it is saved, so a name that does not resolve is refused there rather than breaking the next startup. Four things are refused outright, `[agents.*]` chief among them, so the assistant can adjust how it behaves but never widen what it may call (see [Security](#security)).
- **It can measure how fast its own model is.** `benchmark_model` streams a fixed short prompt and reports two numbers the model can relay to you: time to first token, which is mostly prompt processing and queueing, and output speed in tokens per second, which is decoding. Separating them is the point, since a slow reply is one or the other. It discards a warmup request first (on Ollama a first request can carry a cold model load worth many seconds) and reports the warmup's own figure on its own line, so you can tell "this model is slow" from "this model was not loaded yet". Token counts come from the provider where the provider reports them and from counting stream chunks where it does not, and the report says which. The tool takes no arguments: it measures the model `config.toml` gave the agent holding it, with that agent's declared sampling and reasoning effort, so the numbers describe what you actually run. The shipped config gives it to the agent you talk to. What it measures is its own holder, so a worker declaring it and running on a model of its own is timed on that model rather than on the assistant's, and the report names which agent the figures describe.
- **And you can diagnose it while it is stuck.** `/diag` reports the in-flight turn, the gate state, and a hung turn's async stack, and it never takes the turn gate, so it answers in exactly the case it exists for. `kokua --list-toolsets` prints every capability this install can offer, `kill -USR1 <pid>` dumps all thread stacks to the rotating log, and a startup preflight checks the AIMU underneath and prints the fix rather than failing on an import.

### Planning and self-review

- **[Plan before doing](src/kokua/workflows/planning/).** When you ask for it, the assistant drafts an explicit plan first (which tools, skills, and MCP services it will use, what it will search for, where it needs to build a skill or connect a server) and then carries it out. Planning is per request, not a global mode: use the **Plan** toggle beside the message box, or send `/plan <task>`, which also works in the CLI.
- **Human review.** Enable *Review the plan before executing* to pause a planned turn for your Approve / Edit / Reject; otherwise the plan runs automatically.
- **Adversarial review.** An independent reviewer agent with no conversation context can critique the plan (Kokua re-plans on rejection) and/or the final answer before you see it (Kokua revises, up to `[planning].review_rounds`). Both are off by default and combine with human review, whose prompt shows you the critique. Reviewing the result means the answer cannot stream live: the agentic loop still streams, but the answer appears only once it passes.
- **Reviewers check their claims.** Each reviewer is a tool-using agent that runs a bounded assessment over a curated verification toolset (current date/time, web lookup, arithmetic) before returning a verdict, so it can confirm recency and numeric claims instead of rejecting what it cannot verify from the request alone. The toolset excludes your memory, documents, skills, and MCP servers, keeping the reviewer an independent critic with no access to your state, and excludes `execute_python` and `run_command` because a reviewer cannot be approval-gated (see [Security](#security)).
- **Show all reasoning.** Turn it on for the full trace: every LLM call in a planned turn (planner, each reviewer, executor, each revision) streams live under a labeled phase header, and every intermediate version is shown. This overrides result review's hide-until-vetted gate; only the final answer is saved. The raw trace is recorded per turn, so reloading replays exactly what you saw rather than a summary.

### Acting without you

- **[A scheduled task is a turn with no human in it](src/kokua/scheduling/).** Ask for something on a schedule ("every weekday at 9am, summarize my calendar") and the assistant persists it as a `[scheduling.task.<name>]` table in `config.toml` through its own `schedule_task` / `list_scheduled_tasks` / `cancel_scheduled_task` tools, so it survives restarts and can equally be hand-written and commented. Schedules are one-shot, interval, daily, or weekly. Because nobody is present to approve anything, a scheduled run auto-denies the approval-gated tools rather than waiting on a click that will not come.
- **Every firing gets its own conversation,** nested under the task in the sidebar. Ask for a number and that task keeps only its newest N runs, deleting older ones as it goes (1 means each run replaces the last, 0 keeps everything); a task that names no number follows `[scheduling] max_task_conversations`, 3 by default. Nothing is deleted until a firing succeeds, so a failed run never costs you the last good one.
- **Pause, dry-run, stop, and edit in place.** `disable_scheduled_task` stops a task firing while keeping it and `enable_scheduled_task` resumes it; `run_scheduled_task` fires it now without touching its schedule, reproducing exactly what the scheduled run would do; `stop_scheduled_task` ends a run under way without ending the task, leaving whatever it produced in its own chat. `get_scheduled_task` shows one task in full, prompt included, and `update_scheduled_task` revises any subset of its fields, keeping its id, its past runs, and everything you leave out, so changing a task's wording or its time edits that task rather than rewriting it from memory.

### Reaching outside the chat

- **[Images in and out](src/kokua/images.py).** Attach an image and the assistant reads it (needs a [vision-capable model](https://saxman.info/aimu/reference/model-matrix/)): the composer's paperclip or a paste in the web UI, `/attach <path>` in the CLI. It can also [generate images](https://saxman.info/aimu/how-to/generate-images/) when [`$AIMU_IMAGE_MODEL`](https://saxman.info/aimu/reference/env-vars/) is set (e.g. `gemini:nano-banana`, or a HuggingFace diffusers `hf:<repo>`); without it, no generation tool is offered at all. Images live in `data/images/` and are served at `/images/<name>`; a conversation stores only a short reference, so `sessions.json` stays compact.
- **[PDFs](skills/markdown-to-pdf/).** The `markdown-to-pdf` skill renders Markdown to a PDF in `data/downloads/`, handing back the `/download/<name>` link the web UI serves. Install it with `kokua skills install markdown-to-pdf` and name it in an agent's `tools`. It ships as a skill rather than a toolset because its script declares `fpdf2` and `markdown` inline (PEP 723) and `uv` resolves them per run, so neither is a Kokua dependency. Give it to an agent that also has `fs` and `compute`, which is what runs the script.
- **[Email](skills/email-report/).** The `email-report` skill mails information to you (digests, summaries, reports), written in Markdown and delivered as formatted HTML with a plain-text fallback, optionally attaching files already in `data/downloads/` or `data/images/`. It can only email **you**: the address comes from the host's configuration and the script has no recipient flag at all. Configure `[email]` (`host`, `port`, `from`, `to`, `use_ssl`) and put the password in `$KOKUA_EMAIL_PASSWORD`, never in the config file (for Gmail, an App Password). Kokua passes those settings to the script's environment (by both routes a skill script can reach an agent: the entry agent's own and a spawned worker's), so the script never re-derives your config. Without host, `to`, and the password it sends nothing and says so. Install with `kokua skills install email-report`, name it in an agent's `tools`, and give that agent `fs` and `compute` so it can run the script.
- **[AIMU agents](src/kokua/toolsets/aimu_agents.py).** The `aimu_agents` toolset mounts AIMU's prebuilt orchestrators (`code_review`, `research_report`, `create_content`) and is the worked example of wiring an agent built with AIMU into Kokua: any `Runner` exposes `.run(task) -> str`, so a toolset is the whole bridge and the core learns nothing new. Nothing is mounted until you ask for it: name it in an agent's `tools` in `config.toml`. They are synchronous, so a nested run gets no sub-agent card, no `/stop`, and no approval gate on its workers; and an agent declaring `fs` + `compute` is a stronger reviewer than the tool-less `CodeReviewAgent`. Copy the shape, not necessarily the agents.
- **[Backup](src/kokua/toolsets/github_backup.py).** The `github_backup` toolset copies Kokua's own durable state (config.toml, memory, documents, authored skills, conversation transcripts) into a **private** GitHub repository as a git commit, and makes no commit when nothing changed. The tool takes no arguments at all: the repository, branch, and file list come from `[github_backup]` in `config.toml`, which is what makes it safe to run without per-call approval, and therefore usable from a scheduled task (a proactive turn auto-denies gated tools). The token comes from `$GITHUB_BACKUP_TOKEN`, never the config file, and never reaches a command line. Kokua refuses a public repository, and never force-pushes: a diverged remote is reported for you to reconcile. Logs, downloads, and generated images are left out. Restore is manual and documented. See [Back up to GitHub](docs/how-to/back-up-to-github.md).

### In the browser

- **What the web front end adds on top.** The sidebar lists conversations, titled automatically from the first message, with new/delete and a collapsible, resizable rail whose state is remembered per browser; below it a tasks section lists each task's schedule and next firing with pause/resume, run-now (Stop, while a run is under way), and delete on each row, and that task's own conversations nested under it, so the chat list above holds only conversations you started. It hides when you have no tasks and collapses when you do. Every row carries a localized datetime caption, revealed on hover, that survives reloads. Replies render as GitHub-flavored markdown *while they stream*, via vendored `marked` + `DOMPurify` (no CDN): the page reparses the whole accumulated answer on a 50ms timer rather than committing each finished block, because a newline is not a block boundary in CommonMark and a committed prefix could be wrong in a way no later token could repair. LaTeX (`$...$`, `$$...$$`) is typeset by vendored KaTeX *after* sanitization with `trust` disabled, and only once the turn completes, since half-typed TeX is noise. There is no settings window: the header's theme button is the one control the browser owns, cycling auto / light / dark and applied before first paint, and it is a per-browser preference rather than a `config.toml` setting. The CLI stays single-conversation and renders the same loop in plain text.

## Configuration

Settings come from a TOML file, so you don't repeat flags. Precedence, highest first: **command-line flag > config file > built-in default**. The file is read from `--config <path>`, else `$KOKUA_CONFIG`, else `$KOKUA_HOME/config.toml` (default `~/.kokua/config.toml`).

**The file is required.** Kokua will not start without one, and will tell you to run `kokua config init`. The `[agents.*]` tables exist nowhere else and the assistant cannot work without at least one agent, so there is no useful "no config" state to fall back to. Every individual *key* still has a built-in default, so you set only what you want to change.

```bash
kokua config init           # writes the documented example; --force to overwrite
```

The scaffold comments every key at its default, so changing a default in a later release still reaches keys you left commented, and it ships the four `[agents.*]` tables live rather than commented, since an agent is the one thing Kokua cannot default. See [`config.example.toml`](src/kokua/config.example.toml) for the full set at a glance, the [configuration reference](docs/reference/configuration.md) for what each key accepts and when it applies, and [set up a toolset](docs/how-to/set-up-toolsets.md) for the walkthrough of declaring an agent.

An agent table is a handful of keys:

```toml
[assistant]
agent = "assistant"          # which table below is the agent you talk to

[agents.assistant]
description = "The assistant the user talks to."
system_message = "You are a personal assistant running on the user's own machine. Be concise and helpful."
tools = ["memory", "documents", "skills", "config", "mcp", "scheduling", "conversations", "planning", "capabilities", "time"]
delegates_to = ["researcher", "coder"]
```

`config.toml` is also **app-written**: the assistant's own `update_config` tool and a runtime `add_mcp_server` write back to it, with your comments preserved. There is no second settings store. Which keys `update_config` refuses is `[security] locked_config_keys`, a list of patterns that is yours to set; it ships locking `[security] confirm_tools`, `[email] to`, `[paths] data_dir`, and the whole `[agents.*]` section, so which capability an agent holds stays your decision by default. `locked_config_keys` itself is always locked, whatever the list says, so the assistant can never unlock itself in one call. `[scheduling.task.*]` is refused too, but for a different reason that removing it from the list does not change: the assistant writes scheduled tasks through its own scheduling tools instead, because a bare `update_config` write would edit the file without arming or disarming the scheduler to match. `[compute] command_env_passthrough` is locked for a security reason of its own: without it the assistant could name one of its own credentials there and read it back out of a `run_command` child's environment.

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
```

Point `data/` elsewhere with `[paths] data_dir`. Nothing is written to your working directory or inside the installed package. Scheduled tasks are the one exception to "content lives under `data/`": each is a `[scheduling.task.<name>]` table in `config.toml` itself, since a task is a declaration you should be able to write and comment like any other setting.

## Extending Kokua

Kokua discovers two kinds of plugin at runtime through Python entry points, so a third party adds capability by publishing a package, with no change to Kokua's core:

- **Front ends** (`kokua.frontends` group): how the assistant runs -- terminal, web, a future Telegram or Slack. A front end is a `kokua.plugins.FrontEnd` whose `run(config, args)` drives the assistant.
- **Toolsets** (`kokua.toolsets` group): one named capability an agent can declare. A toolset is a `kokua.plugins.Toolset` whose `build(ctx)` returns [`@aimu.tool`](https://saxman.info/aimu/how-to/add-custom-tool/) callables, plus an optional `guidance` string appended to the prompt of any agent holding it. Installing it puts the name in the namespace; an agent still has to declare it.

The built-in `cli` / `web` front ends and Kokua's three built-in toolsets are registered exactly this way in Kokua's own `pyproject.toml` -- if the built-in path and the plugin path ever diverge, the plugin path is the broken one. To add your own from another package:

```toml
# in your package's pyproject.toml
[project.entry-points."kokua.toolsets"]
weather = "my_weather_pack:TOOLSET"
```

`pip install` it, and `kokua --list-toolsets` shows it. Its tools do **not** appear automatically to any agent your config describes: name it in an agent's `tools` list in `config.toml`, since a capability is declared and never defaulted. An agent that declares `capabilities` can compose a worker holding it for the length of one task with no config edit, which is the one route that does not go through a table. See [`toolsets/image.py`](src/kokua/toolsets/image.py) for the template (one tool, and a `build` that returns nothing when its prerequisite is missing), and [`toolsets/aimu_agents.py`](src/kokua/toolsets/aimu_agents.py) for the same shape carrying a whole AIMU agent rather than a plain function. For something simpler than a toolset, [add a skill](docs/how-to/add-skills.md) instead: a directory with a script needs no packaging at all. [Set up a toolset](docs/how-to/set-up-toolsets.md) is the full walkthrough: what is in the namespace, how an agent declares its `tools` and `delegates_to`, and exactly which mistakes fail at startup.

## Security

Kokua can author and run Python/shell scripts as **real subprocesses with your user privileges (no sandbox)**, and connect to remote MCP servers and run whatever tools they expose. Real capability is the point of a personal assistant, but it means a prompt-injected or mistaken model can run arbitrary code on your machine and call arbitrary remote tools. Only run Kokua with a model, inputs, and MCP servers you trust. The CLI prints a notice on startup.

**Tool approval.** The riskiest tools require confirmation before each call -- a `y/N` prompt in the terminal, Allow/Deny buttons in the web UI. By default this gates `add_skill_script`, `add_mcp_server`, `execute_python`, `run_command`, and `update_config`. Adjust with `[security] confirm_tools` or `--confirm-tools name1,name2` (empty disables it). Gating is by tool name, so it applies to a sub-agent's call as much as the assistant's own: a worker's gated call is routed to you. Proactive and backgrounded turns auto-deny these regardless, so the assistant never runs a full-access tool unattended, and an approval prompt only ever appears for the conversation you are currently viewing.

**Capability is granted by hand.** `[agents.*]` decides what every agent can call, and it is locked by default in `[security] locked_config_keys`: `update_config` refuses it. So the assistant can connect an MCP server, but it cannot give itself or a worker the server's tools: that takes an edit you make. Removing `agents.*` from `locked_config_keys` is a hand-edit that hands the assistant its own capability table, so leave it in place unless that is what you want. Note the flip side, since it is easy to misread: there is no privilege tier among agents. An agent whose table declares `config` really does get `update_config`, and one declaring `compute` really does get `execute_python` and `run_command`. Your hand-edit is the consent, and `confirm_tools` is the gate at call time.

**Injection crosses conversations.** The assistant can read every saved conversation, and a transcript is untrusted text (a worker may have pasted web content into it), so an injection that lands in one conversation can influence what the assistant does in another. The three read tools are ungated by default, since they only read and gating them would make an unattended scheduled run that reads history fail silently; add `read_conversation` and `search_conversations` to `[security] confirm_tools` if you would rather approve each one.

**The reviewer needs no gate.** With adversarial review on, the reviewer is a tool-using agent, and an autonomous critic cannot pause to ask you mid-review. Rather than exempt it from the approval gate, its verification toolset holds nothing the gate exists to cover: web lookup, `calculate`, and the clock, with no `execute_python` or `run_command`, no memory or document access, and no MCP mutation. A test pins this against the shipped `confirm_tools` default, so adding a name there fails the suite until the reviewer's toolset is re-checked.

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
scheduling/   recurrence math, the durable task lifecycle over config.toml, the agent-facing tools
channels/     ChannelUI plus the concrete channels
frontends/    cli, web        -- registered as plugins, exactly like a third party's would be
registry/     the Toolset type, name resolution, and the live state a toolset is built against
toolsets/     one file per toolset, and nothing else
```

Outside `src/`, the repository also carries `skills/`: Agent Skills Kokua ships as content rather than as
Python, so they are not in the wheel. `kokua skills install` copies them into your skills folder.

The stable public import surface is `kokua.plugins`, `kokua.config`, `kokua.core`, `kokua.channels.web`, and `kokua.images`. Everything else is internal and may move.

## Resources

### Kokua

- 📘 [How-to guides](docs/how-to/index.md): [set up a toolset](docs/how-to/set-up-toolsets.md) (the namespace, declaring an agent, writing a toolset) · [add a skill](docs/how-to/add-skills.md) · [add an MCP service](docs/how-to/add-mcp-services.md) · [back up to GitHub](docs/how-to/back-up-to-github.md).
- 💡 [Design principles](docs/explanation/design-principles.md): why Kokua exists, and the six principles that serve it, each with the code that backs it and the patterns it excludes.
- 🏗️ [Architecture](docs/explanation/architecture.md): module layout, control flow, and the concurrency model.
- ⚙️ [Configuration reference](docs/reference/configuration.md): every `config.toml` key, what it accepts, which apply live, and who may write each. Short form: [`config.example.toml`](src/kokua/config.example.toml).
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
