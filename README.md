<p>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/kokua-horizontal-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/kokua-horizontal-light.svg">
  <img alt="Kokua — a personal AI assistant that extends itself" src="docs/assets/kokua-horizontal-light.svg" width="360">
</picture>
</p>

**Help, assistance** (Hawaiian). A hackable, modular personal-assistant application (OpenClaw / Hermes Agent
style) built on the [AIMU](https://github.com/saxman/aimu) library. Kokua runs an always-on assistant
that chats with you, authors and runs its own skills, connects to remote tool services, delegates
independent subtasks to isolated sub-agents, and remembers facts and documents across conversations.
Kokua **extends itself**: it writes and runs new skills to take on capabilities it didn't ship with, and
grows its reach by connecting to remote MCP services on its own. And where it can't extend itself, you
extend it: front ends and tool-packs are **plugins** you add by installing modules, not by editing the core.

It runs as a single user in a single process, and can run code and connect to remote services with your
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

## Quick Start

```bash
kokua --model ollama:qwen3:8b
```

Omit `--model` to use `AIMU_LANGUAGE_MODEL`, or else the first already-running local model found (Ollama,
then a local OpenAI-compatible server); a cloud model is never auto-selected, and startup fails with an
actionable message if none is found. Chat at the prompt; Ctrl-D exits.
Send `/stop` to cancel a reply that's still streaming (the partial turn is kept, so the conversation
continues); the web UI has a Stop button for the same. Send `/diag` if the assistant ever stops
responding: it reports the in-flight turn, whether the turn lock is held, and dumps a stuck turn's
async stack (it is handled without the lock, so it answers even when a hung turn holds it). Diagnostic
logs are written to `$KOKUA_HOME/data/logs/kokua.log` (rotating); `kill -USR1 <pid>` dumps all thread
stacks there.

Run the **web** front end instead (needs the `web` extra):

```bash
kokua --frontend web              # or: kokua-web
# then open http://127.0.0.1:8000
```

## Using Kokua

Reloading the page replays the prior conversation (the assistant already keeps its context across
reconnects; this makes it visible again), including reasoning and tool calls when `show_thinking` /
`show_tools` are on. Assistant replies are rendered as GitHub-flavored markdown (tables, nested lists, code, links, task
lists, etc.) once each turn completes, via vendored `marked` + `DOMPurify` (bundled, no CDN); the HTML
is sanitized, so model or tool output can't inject scripts or markup. LaTeX math (`$...$`, `$$...$$`) is
typeset with vendored KaTeX, applied after sanitization with `trust` disabled so untrusted output stays
safe.

The web UI holds multiple conversations: the left sidebar lists them (titled automatically from the first
message) and has a "+ New conversation" button; click any conversation to continue it, or hover a
conversation and click its `×` to delete it (you are asked to confirm; deleting the current conversation
drops you into the most recent remaining one). Memory (facts and documents) is shared across all
conversations. The CLI remains single-conversation for now.

The header's gear button opens a settings panel to change, at runtime, the model generation parameters
(`temperature`, `max_tokens`, `top_p`, `top_k`, `presence_penalty`, `repetition_penalty`), display prefs
(`show_thinking` / `show_tools`), and the active model. These changes apply on the next turn and are
remembered across restarts (saved to `data/runtime-settings.json`, layered over the optional
`[generation]` config section). Leave a generation field blank to use the model/provider default; note
that thinking models ignore `top_p`/`top_k` and force `temperature`, and Anthropic does not support the
penalty parameters. The panel also has a theme selector (auto / light / dark; auto follows your OS
preference); the theme is a per-browser choice remembered locally, applied before first paint to avoid a
flash.

List what's installed:

```bash
kokua --list-frontends     # cli, web, + any installed plugins
kokua --list-tool-packs    # example, pdf, image, email, + any installed plugins
```

Useful flags: `--tools web,fs,compute,misc` (AIMU built-in tool groups), `--mcp <url>` (repeatable, connect
a remote MCP server; `--mcp-bearer` for auth), `--no-memory`, `--no-plugins`, `--no-subagents`, `--system`, `--config <path>`,
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

### State

All state lives under `~/.kokua` (override the root with the `KOKUA_HOME` environment variable). The root
holds an optional `config.toml` and a single `data/` directory for all transient and user-provided content:

```
~/.kokua/
  config.toml          # optional (see Configuration file)
  data/
    skills/            # authored skills
    sessions.json      # conversations (web UI can hold several)
    memory/            # semantic facts
    documents/         # saved documents (text; scanned by the DocumentStore)
    downloads/         # generated files (e.g. PDFs), served by the web UI at /download
    images/            # uploaded + generated images, served by the web UI at /images
    runtime-settings.json  # runtime model settings from the web settings panel
    scheduled_tasks.json   # durable scheduled tasks (agent-managed)
```

Point `data/` elsewhere with `[paths] data_dir` in the config file. Nothing is written to your working
directory.

## Features

A built-in `pdf` tool-pack gives the assistant a `markdown_to_pdf` tool: ask it to turn something into a
PDF and it writes the file to `data/downloads/`. In the web UI the assistant hands back a download link
(files are served at `/download/<name>`); from the CLI it reports the file path.

**Images.** Attach an image and the assistant reads it (needs a vision-capable model; Claude models
qualify). In the web UI, use the composer's paperclip or paste an image; from the CLI, run
`/attach <path>` before your message. The assistant can also generate images when the `AIMU_IMAGE_MODEL`
env var is set (e.g. `gemini:nano-banana` or a HuggingFace diffusers `hf:<repo>`); without it, no image
generation tool is offered. Images are stored under `data/images/` and served at `/images/<name>`; a
conversation keeps only a small reference, so `sessions.json` stays compact.

**Email.** A built-in `email` tool-pack gives the assistant a `send_email` tool so it can email
information to you (digests, summaries, reports). It can only email *you*: the recipient is fixed to the
`[email] to` address, so the tool takes no recipient and cannot mail anyone else. The body is written in
Markdown and delivered as formatted HTML with a plain-text fallback; the assistant can attach files that
already live in `data/downloads/` or `data/images/`. Configure the `[email]` section (SMTP `host`, `port`,
`from`, `to`, `use_ssl`) and set the password in the `KOKUA_EMAIL_PASSWORD` environment variable, never in
the config file (for Gmail / Google Workspace, use an App Password). The tool appears only once host, `to`,
and the password are all present. Sending is ungated, so scheduled/proactive turns can send too, e.g. a
daily digest.

**Scheduled tasks.** Ask the assistant to do something on a schedule ("every weekday at 9am, summarize
my calendar") and it uses its `schedule_task` / `list_scheduled_tasks` / `cancel_scheduled_task` tools
to persist the task to `data/scheduled_tasks.json`; it survives restarts. Schedules can be one-shot, an
interval, daily at a time, or weekly on a weekday. When a task is due it runs an unprompted turn (shown
with the amber "proactive" styling); ask for a task to run in its own conversation and each run lands in
a fresh chat you can review and follow up on. Scheduled runs auto-deny the approval-gated tools, since
no one is present to approve them.

**Sub-agents.** The assistant can delegate an independent subtask to a fresh, isolated sub-agent via a
`spawn_subagent(agent_type, task)` tool (on by default; `--no-subagents` to disable). Sub-agents come in
roles — built-in `researcher` (web lookups), `coder` (files + code), and `generalist` (everything) — each
cloning the active model with its own tool subset (a role's tools are its groups intersected with the
enabled `[tools]` groups; parent-only memory/skills/MCP tools are withheld). Define or override roles
under `[subagents.roles.*]` in the config. Independent spawns in one turn run concurrently
(`[subagents] concurrent`, on by default). A sub-agent's gated-tool calls (the `confirm_tools`, e.g.
`execute_python`) are routed to the parent for your approval and are not run unattended.

**Deep planning (per request).** When you ask for it, the assistant drafts an explicit plan before doing
the work — which tools, skills, and MCP services it will use, what it will web-search for, and where it
needs to build a skill or connect a new MCP server — then carries it out. Planning is per request, not a
global mode: flip the **Plan** toggle next to the message box (it stays on until you turn it off) or send
`/plan <task>` (the latter also works in the CLI). Enable *Review the plan before executing* in the
settings panel (or `[planning]` in the config file) to pause a planned turn for your Approve / Edit /
Reject; otherwise it runs the plan automatically.

For extra rigor, turn on **adversarial review**: an independent reviewer agent with no conversation
context critiques the plan (*Adversarial plan review* — Kokua re-plans on rejection) and/or the final
answer before it's shown (*Review the result* — Kokua revises on rejection, up to `review_rounds`).
Reviewing the result means the final answer can't stream live; the agentic loop (thinking and tool calls)
still streams, but the answer appears only after it passes review. Both are off by
default and combine with human plan-review (the critique is shown to you before you decide). In the web
UI the reviewers appear as their own cards ("Plan reviewer / Result reviewer — reviewing…" → approved /
rejected with the issues), and those cards replay in order when you reload the conversation. (With **Show
all reasoning** on, these cards are replaced by the reviewers' full streamed reasoning — see below.)

The reviewers are tool-using agents: each runs a bounded tool-calling assessment over a curated
verification toolset (the current date/time, web lookup, and computation) before returning its verdict,
so it can check recency and factual/numeric claims instead of rejecting anything it can't confirm from
the request alone. (This is why, e.g., a correct "as of today" answer is no longer rejected for
date-unawareness: the reviewer fetches the date the same way the main agent does.) The toolset excludes
your memory/documents, skills, and MCP servers, so the reviewer stays an independent critic with no
access to your state. See [Security](#security) for a known limitation of this toolset.

Turn on **Show all reasoning** for the full trace: every LLM call in a planned turn (planner, each
reviewer, executor, and each revision) streams its reasoning + output live under a labeled phase header,
and every intermediate plan and result version is shown (reviewers stream a prose assessment). This
overrides result review's gate — you see every version — and only the final answer is saved to the
conversation. The whole raw trace is recorded per turn, so reloading a verbose turn replays the same raw
output you saw live (not a summary); in this mode the summary reviewer cards are not shown. Thinking is
shown when the model emits it (adaptive models may skip it on simple requests).

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

**Known limitation -- reviewer tools bypass the approval gate.** When adversarial review is on (the
deep planning mode described under [Features](#features)), the reviewer is a tool-using agent, and its
verification toolset includes `execute_python` (so it can run calculations to check numeric claims). Unlike the main
agent, the reviewer has **no** approval gate -- an autonomous critic can't pause to ask you mid-review --
so it can run arbitrary Python unattended while reviewing. This is an intentional short-term tradeoff we
intend to revisit (e.g. sandboxing the reviewer, or restricting it to safe `calculate`-only arithmetic).
Until then, treat "review on" as granting the reviewer the same code-execution reach the main agent has,
and only enable it with a model and inputs you trust.

## Development

```bash
uv sync --all-extras            # installs the editable ../aimu + all extras (see Install)
ruff check . && ruff format --check .
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
