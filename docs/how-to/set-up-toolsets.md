# Set up a toolset

A *toolset* is one **named capability** an agent can ask for: a `Toolset` object carrying a name, a
description, and a `build(ctx)` that returns tool callables. Every toolset an install offers, wherever it
came from, lives in one namespace. An agent's `tools` list names toolsets from that namespace, and the
tools the agent can actually call are whatever those toolsets built.

A toolset can also carry a `Workflow` instead of, or alongside, its tools: a turn strategy the user
invokes with a `/`-command (`/plan` is the shipped example -- see
[Architecture](../explanation/architecture.md#workflows)). Declaring the toolset is what grants the
command, exactly as declaring it grants its tools, and only the **entry** agent's declaration counts,
since a command arrives on the channel and only the entry agent has one. The command word must be a
single lowercase token with no whitespace, and cannot be `stop` or `diag`, which are the assistant's
own; two toolsets offering the same command is a startup error, not a silent shadowing.

So there are two jobs here, and this guide covers both: declaring what an agent holds in `config.toml`,
and writing a toolset of your own to put something new in the namespace.

## The rule that decides everything

> **A capability is declared, never defaulted. Nothing reaches an agent until an `[agents.<name>]` table
> names it in `tools`.**

There is no code path that adds a tool an agent did not name, and no flag that can disagree with a
declaration. An installed plugin toolset does nothing on its own. A connected MCP server does nothing.
Neither does the clock: `time` is a toolset like any other, so an agent that wants to know what day it is
declares it. Kokua refuses to start with no `[agents.*]` tables at all, because an agent is the only
route from a capability to the model.

One capability reaches past the rule while staying inside it. An agent whose table names `capabilities`
can call `compose_worker` to build a sub-agent from anything installed except `skills` (entry-agent
only) and `capabilities` itself (which would hand the worker a fresh composition budget), chosen per
task rather than declared: the exception is entered by declaration, and what it grants dies with the
task rather than widening any agent your config describes.

## One namespace

Ask your install what it accepts:

```bash
uv run kokua --list-toolsets      # every name, grouped by what provides it
uv run kokua --list-frontends     # the other plugin group
```

`--list-toolsets` reads your config file, since the registry depends on it (which servers are configured,
whether plugins are loaded), so run `kokua config init` first if you have no config yet. The output groups
names by provider, because a `tools` list deliberately does not say where a name comes
from (`"stocks"`, not `"mcp:stocks"`), so this command is the one place provenance is visible:

| Provider | What is in it |
| --- | --- |
| **AIMU capability** | the built-in tool groups (`web`, `fs`, `compute`, `time`, `misc`, `audio`, `speech`, `transcription`), plus `memory` and `documents` over AIMU's two stores and `skills` for skill authoring |
| **core subsystem** | Kokua's own: `capabilities`, `config`, `conversations`, `mcp-admin`, `planning`, `scheduling` |
| **built-in toolset** | the three `Toolset`s Kokua's own distribution registers under the `kokua.toolsets` entry-point group: `aimu_agents`, `github_backup`, `image` |
| **skill** | one entry per skill in your skills folder, so an individual skill is declarable by name (see [add skills](add-skills.md)) |
| **plugin** | every other `Toolset` installed under the `kokua.toolsets` entry-point group -- i.e. one a third party's package registered |
| **MCP server** | one per `[[mcp.server]]` table, named by its required `name` |

Because a name carries no provider prefix, a name must be unique: **two providers claiming one name is a
startup error** that names both sides and their descriptions, rather than one silently shadowing the
other. That is also why AIMU's own `image` group is deliberately not registered: Kokua's `image` plugin
toolset provides `generate_image` writing into the servable `data/images/`, and registering both would
let deduplication quietly pick one.

What the AIMU-provided toolsets hold:

| Toolset | Tools | Notes |
| --- | --- | --- |
| `web` | `web_search`, `get_webpage`, `get_webpage_html`, `wikipedia`, `get_weather` | |
| `fs` | `list_directory`, `read_file` | **Read-only.** Writing a file needs `execute_python`. |
| `compute` | `calculate`, `execute_python` | `execute_python` is approval-gated by default |
| `time` | `get_current_date_and_time`, `convert_time` | not implicit; declare it |
| `misc` | `echo` | |
| `memory` | `store_memory`, `search_memories`, `list_memories` | one store shared by every agent that declares it |
| `documents` | `save_document`, `read_document`, `list_documents`, `search_documents` | likewise |
| `skills` | `author_skill`, `add_skill_script` | entry agent only; see [Add a skill](add-skills.md) |
| `audio` | `generate_audio` | needs `$AIMU_AUDIO_MODEL` |
| `speech` | `generate_speech` | needs `$AIMU_SPEECH_MODEL` |
| `transcription` | `transcribe_audio` | needs `$AIMU_TRANSCRIPTION_MODEL` |

The three generative groups have no default model, so they raise at call time rather than downloading
weights you did not ask for; see AIMU's
[environment variables](https://saxman.info/aimu/reference/env-vars/) for the accepted values.

## Declare an agent

Every agent is one `[agents.<name>]` table, and `[assistant].agent` says which of them you talk to. Four
keys, all optional individually; anything else is a startup error naming the key.

```toml
[assistant]
agent = "assistant"          # the entry agent, and the root of the delegation graph

[agents.assistant]
description = "The assistant the user talks to."
system_message = "You are a personal assistant running on the user's own machine. Be concise and helpful."
tools = ["memory", "documents", "skills", "config", "mcp-admin", "scheduling", "conversations", "planning", "capabilities", "time"]
delegates_to = ["researcher", "report-writer"]

[agents.researcher]
description = "Research specialist: gather and verify information from the web."
tools = ["web", "misc", "time"]
system_message = """\
You are a research sub-agent. Investigate the task with web search and page lookups, verify claims \
against sources rather than memory, and return a concise findings summary that names its sources."""

[agents.report-writer]
description = "Builds and emails PDF reports."
tools = ["markdown-to-pdf", "email-report", "time"]
```

- **`tools`** is the whole capability declaration, in one flat list over the one namespace. A built-in
  group, a core capability, a plugin toolset, and an MCP server are all named the same way.
- **`delegates_to`** is itself the switch: a non-empty list gives that agent a
  `spawn_subagent(agent_type, task)` tool offering **exactly** the agents it names, each built from its
  own table. There is no separate "enable sub-agents" setting to disagree with it. The nesting is
  Kokua's, not AIMU's: an agent you delegate to that delegates in turn gets its own menu of its own
  targets, so each level offers only what its table declares. The graph must be acyclic.
- **`description`** is the label a delegator sees in its worker menu (it becomes the first line of that
  agent's system message), so write it as the basis on which a worker is chosen.
- **`system_message`** is the agent's opener. Omit it and the agent falls back to
  `[assistant].system_message`, then to the built-in default. For the entry agent only, `--system`
  overrides whichever of those it would otherwise use, for that run; it never touches a worker's own
  declared opener.

Two agents declaring the same toolset share the state behind it: one memory store, one set of live MCP
connections, one skill directory. If two toolsets an agent declares contribute the same tool name, the
one declared **first** wins, so declared order is meaningful.

Restart Kokua after editing. Agents are read at startup and the new declaration takes effect there.

### Guidance travels with the capability

A toolset can carry a `guidance` string, and it is appended to the system message of **any** agent that
declares that toolset. Declaring `memory` therefore brings the paragraph that tells the model to call
`store_memory` for durable facts; undeclaring it takes that paragraph away. There is no prompt constant
listing capabilities that has to be kept in step by hand.

An agent's full system message is: its own opener, then its toolsets' guidance in declared order, then
the delegation instructions if `delegates_to` is non-empty, and finally a "you are a lean supervisor,
you MUST delegate" clause only when *every* toolset it declares is marked cross-cutting (something an
agent holds to manage itself: memory, documents, skills, config, `mcp-admin`, scheduling,
conversations, `planning`, `capabilities`, the clock). Give that agent one domain toolset and the lean clause disappears, since it
would then contradict the tools the model can see.

Note what `cross_cutting` is **not**: it is not a permission boundary. An agent whose table says
`tools = ["config"]` really does get `update_config`. See
[design principles](../explanation/design-principles.md#corollary-a-capability-is-declared-never-defaulted)
for why that is the intended shape, and what the actual security boundary is.

### `[agents.*]` is locked by default

The assistant holds `update_config`, so a writable agent table would let it widen its own reach.
`[security].locked_config_keys` ships locking the whole section by prefix; removing `agents.*` from
that list is the hand-edit that hands the assistant its own capability table. Which capability an agent
gets stays a human decision by default, made in the file. See [the configuration
reference](../reference/configuration.md#who-may-change-which-key) for the full policy.

## Where mistakes surface

Every wrong *name* is now a startup error, and every failing check below runs before Kokua opens the
session store or connects to anything, so a bad config fails with nothing written and nothing connected:

| Mistake | What happens |
| --- | --- |
| Unknown name in an agent's `tools` | **Startup fails**, naming the agent, the name, and every available toolset. |
| `skills` on any agent but the entry agent | **Startup fails.** Spawned workers are plain AIMU agents and cannot host it. |
| Unknown key in an `[agents.*]` table | **Startup fails**, naming the key. Only `description`, `system_message`, `tools`, and `delegates_to` are accepted. |
| An old per-role key (`groups`, `tool_packs`, `mcp_servers`) | **Startup fails**, telling you to list it in `tools` instead. |
| `[assistant].agent` naming no table | **Startup fails**, listing the agents you did configure. |
| `delegates_to` naming an unknown agent | **Startup fails**, listing the agents you did configure. |
| A delegation cycle | **Startup fails**, printing the cycle as a path. |
| No `[agents.*]` tables at all | **Startup fails**, pointing at `config.example.toml` to copy from, or `kokua config init --force` to overwrite this file with it. |
| Two providers claiming one toolset name | **Startup fails**, naming both providers and their descriptions. |
| A third-party plugin toolset or MCP server no agent names | Starts fine. One warning line in the log: it reaches no agent. Kokua's own built-in toolsets and your installed skills are exempt: they ship, or sit on disk, regardless of what any agent declares. |
| A plugin toolset whose `build` raises | Logged and skipped; the agent starts without those tools. A core or AIMU toolset failing this way is a bug and is *not* tolerated. |
| Two declared toolsets sharing a tool name | The one declared first wins. |

Two of those are quiet by design, and both land in `$KOKUA_HOME/data/logs/kokua.log`. There is no
console log handler, so they do not appear in your terminal.

The most direct check that an agent got what you meant is to ask the assistant to delegate to it and
report its tools.

## Write your own toolset

A toolset is a `kokua.plugins.Toolset` whose `build(ctx)` returns
[`@aimu.tool`](https://saxman.info/aimu/how-to/add-custom-tool/) callables. Kokua discovers it through
the `kokua.toolsets` entry-point group, so your package needs no change to Kokua's core.

```python
# my_weather_toolset/__init__.py
from aimu.tools import tool

from kokua.plugins import Toolset, ToolsetContext


def build(ctx: ToolsetContext) -> list:
    """Return this toolset's tools. `ctx.config` is the resolved AssistantConfig."""

    @tool
    def current_conditions(city: str) -> str:
        """Report the current weather for a city.

        Args:
            city: City name, e.g. "Seattle".
        """
        return f"It is raining in {city}."

    return [current_conditions]


TOOLSET = Toolset(
    name="weather",
    description="Current conditions and forecasts.",
    build=build,
    guidance=" When the user asks about weather, call `current_conditions` rather than guessing.",
)
```

```toml
# in your package's pyproject.toml
[project.entry-points."kokua.toolsets"]
weather = "my_weather_toolset:TOOLSET"
```

`pip install` it, confirm with `kokua --list-toolsets`, then give an agent `tools = ["weather", ...]`.
Installing it is not enough, for the reason at the top of this guide.

Four things to know about `build`:

- **It creates closures, not process state.** `build` runs once per agent, so anything it constructs is
  constructed once per agent: two agents declaring your toolset would get two of whatever it opened.
  Shared state belongs on `LiveState` and reaches you through the context, where it is a lazy property
  built at most once (`ctx.state.memory_store`, for one, is opened only because some agent declared the
  toolset that needs it). A toolset that needs its own expensive object should build it inside the
  tool call, as [`toolsets/aimu_agents.py`](../../src/kokua/toolsets/aimu_agents.py) does and explains.
- **`ctx.agent` is the live agent, and is `None` for a spawned worker.** A toolset that genuinely needs
  the agent object should be marked `entry_point_only=True`, which makes declaring it on any other agent
  a startup error instead of a `None` at build time. `skills` is the one built-in in that position.
- **Failure is contained, not reported loudly.** An exception from a *plugin's* `build` is caught,
  logged, and the toolset contributes nothing; the assistant still starts. Nothing in the UI says so.
- **`guidance` is optional and appended to every agent that declares you**, so write it as instructions
  that make sense wherever the toolset lands, not as a description of one agent's job.

### Own a config section

A toolset can also own a whole `[<name>]` section of `config.toml`, by declaring
`settings=(Setting(key, kind, default, hot=...), ...)` on itself, the way
[`toolsets/planning.py`](../../src/kokua/toolsets/planning.py) owns `[planning]`. The section is never
named separately: it is always the toolset's own name, so the namespace's existing duplicate-name check
also keeps two toolsets from claiming one section, and a name that collides with a section Kokua's own
core already parses (`assistant`, `email`, `security`, and the rest) is refused at startup rather than
silently taking over the core setting behind it.

`hot=True` marks a setting the user can change without restarting, and the change reaches whatever reads
it in the same session. `update_config` is the one path that does this, and it works off the declared
table rather than any hand-written markup, so a third party's hot setting is live the moment it is
declared -- nothing to add to a front end. (The web UI has no settings window; the theme button in its
header is a per-browser preference, not a `RuntimeSetting`.) Leave `hot` off (the default) for a
value read once, with no live surface to offer it --
`[planning].review_rounds` is that case, since a round budget is read once per turn. A `Setting`'s
`kind` must be `bool`, `int`, or `str`; anything else fails at startup, naming the toolset and the
unsupported type.

How you read a declared value back depends on what you are writing. A workflow gets
`WorkflowContext.settings`, attribute access over its carrying toolset's section
(`ctx.settings.review_rounds`), always complete because every declared default is seeded before any
workflow or toolset runs. A plain toolset's `build(ctx)` has no view of its own that way -- a
`ToolsetContext` is one agent's handle onto the tools it builds, not onto one toolset's settings -- so
it reads `ctx.config.toolset_settings["<name>"]` directly, the same dict a workflow's view wraps.

### Or carry a workflow instead of tools

Set `workflow=` on your `Toolset` and `build` can return no tools at all: the toolset exists solely to
grant a turn strategy, the way [`toolsets/planning.py`](../../src/kokua/toolsets/planning.py) does for
`/plan`. A workflow's `build` returns an `aimu.aio.AsyncRunner`. **Base tier** is any `AsyncRunner`, so
AIMU's own `aimu.aio.workflows` (`Chain`, `Parallel`, `Router`, `EvaluatorOptimizer`,
`PlanExecuteEvaluator`) work with just the declaration above and no adapter -- Kokua streams `run()`
into the reply. **Rich tier** additionally implements `run_turn()` and is handed the channel, a slot for
a human decision, and control of the agent's transcript, which is what deep planning needs to show
phases and reviewer cards and save a clean turn.

Three things to know before choosing a tier. A base-tier turn is **not persisted** when the runner is
self-contained: it appends nothing to the agent's own transcript, so the exchange is gone after a
reload (a runner that closes over `ctx.agent` and calls it directly is the exception -- see
`_drive_base_tier` in [`core/turns.py`](../../src/kokua/core/turns.py)). Reaching the model as a
callable tool via `AsyncRunner.as_tool()` needs your runner to actually *inherit* `aio.AsyncRunner`, not
merely match its shape -- `as_tool()` is a concrete method the base class provides, not a name Kokua
looks up. And `WorkflowContext.settings` is attribute access over the carrying toolset's own `[<name>]`
section -- see [own a config section](#own-a-config-section) above for how to declare what is in it. See
[`workflows/protocol.py`](../../src/kokua/workflows/protocol.py) for the full contract.

Kokua's own three toolsets register exactly this way in its
[`pyproject.toml`](../../pyproject.toml). If the built-in path and the plugin path ever diverge, the
plugin path is the broken one.

- [`toolsets/image.py`](../../src/kokua/toolsets/image.py) is the minimal template, and also shows a
  `build` that returns no tools when its prerequisite is absent, so the model never sees a tool it cannot
  satisfy.
- [`skills/dice-roller/`](../../skills/dice-roller/) is the equivalent template for a skill, which is the
  lighter path when your capability does not need live process state. Kokua's own `markdown-to-pdf` and
  `email-report` ship that way. See [add skills](add-skills.md).
- [`toolsets/aimu_agents.py`](../../src/kokua/toolsets/aimu_agents.py) carries a whole AIMU agent instead
  of a plain function: any `Runner` exposes `.run(task) -> str`, so a toolset is the entire bridge and
  the core learns nothing new.

## See also

- [Add an MCP service](add-mcp-services.md): a server is a toolset too, named in the same list.
- [Add a skill](add-skills.md): the one toolset only the entry agent can hold.
- [Architecture](../explanation/architecture.md#how-an-agents-tools-resolve): how a declaration becomes
  tools, and the shipped entry agent's full inventory, pinned by a test.
- [Design principles](../explanation/design-principles.md): why capability arrives as a plugin, and why
  it is declared rather than defaulted.
- AIMU: [add a custom tool](https://saxman.info/aimu/how-to/add-custom-tool/) for the `@tool` decorator
  rules, and [built-in tools](https://saxman.info/aimu/reference/api/tools/) for what each group holds.
