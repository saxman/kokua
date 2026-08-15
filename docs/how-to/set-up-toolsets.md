# Set up a toolset

A *toolset* is the set of tools one agent can actually call. Kokua does not have a single toolset: the
supervisor's is fixed in code, and every other toolset is assembled per **sub-agent role** in
`config.toml`. This guide covers both halves of that: configuring a role's toolset from what is already
installed, and writing a tool-pack to install something new.

## The rule that decides everything

The assistant is a lean supervisor. It mounts no built-in tool groups, no tool-pack tools, and no MCP
tools; it delegates all specialized work to workers via `spawn_subagent`. So:

> **Nothing reaches an agent until a `[subagents.roles.*]` table names it.**

An installed tool-pack does nothing. A connected MCP server does nothing. An enabled tool group does
nothing. Each becomes reachable only when a role lists it, and the worker spawned for that role carries
it. This is why `config.toml` refuses to start with zero roles: a role is the only route from a tool to
an agent.

## The two layers

### `[tools] groups` is a ceiling, not a toolset

This list is the set of AIMU built-in groups that workers *may* draw from. Nothing mounts it directly.

```toml
[tools]
groups = ["web", "fs", "compute", "time", "misc"]
```

| Group | Tools | Notes |
| --- | --- | --- |
| `web` | `web_search`, `get_webpage`, `get_webpage_html`, `wikipedia`, `get_weather` | |
| `fs` | `list_directory`, `read_file` | **Read-only.** Writing a file needs `execute_python`. |
| `compute` | `calculate`, `execute_python` | `execute_python` is gated by default |
| `time` | `get_current_date_and_time`, `convert_time` | see the ambient-clock rule below |
| `misc` | `echo` | |
| `image` | `generate_image` | needs `$AIMU_IMAGE_MODEL` |
| `audio` | `generate_audio` | needs `$AIMU_AUDIO_MODEL` |
| `speech` | `generate_speech` | needs `$AIMU_SPEECH_MODEL` |
| `transcription` | `transcribe_audio` | needs `$AIMU_TRANSCRIPTION_MODEL` |

`["all"]` enables every group as a ceiling; `["none"]` enables nothing and leaves your workers with no
built-in tools to draw on. The four generative groups have no default model, so they raise at call time
rather than download weights you did not ask for; see AIMU's
[environment variables](https://saxman.info/aimu/reference/env-vars/) for the accepted values.

`"all"` means something narrower inside a **role**, and this is easy to get wrong. A role with
`groups = ["all"]` resolves to AIMU's `ALL_TOOLS`, which deliberately excludes `execute_python` as
higher-risk than the rest. So `groups = ["all"]` is *not* a superset of `groups = ["compute"]`: a role
that needs to run code has to name `compute`.

`--tools web,fs` overrides this list for one run. Note that it **replaces** the list rather than adding
to it.

### A role's `[subagents.roles.*]` table is the toolset

A worker's toolset is the union, deduplicated by tool name, of four sources
([`core/build.py`](../../src/kokua/core/build.py)):

1. the role's `groups`, **intersected** with `[tools] groups` (a role narrows within what is enabled and
   can never exceed it);
2. the tools of every MCP server named in `mcp_servers` (matched by the server's configured `name`
   first, then its raw URL);
3. the tools of every tool-pack named in `tool_packs`;
4. the `time` group, added to every role whenever `time` is globally enabled.

That fourth source is the one addition a role does not ask for. A worker scoped to a single tool-pack
still has to resolve "by tomorrow morning", so a role that forgot to ask for a clock produced a worker
that silently could not tell the time. It still respects the global list: drop `"time"` from
`[tools] groups` and no agent has a clock.

Deduplication keeps the **first** tool of a given name, in the source order above. A tool-pack tool that
shares a name with a built-in one is therefore dropped for any role that also enables that group, so
give your own tools distinct names.

## Add a role

Each role is one table. `description` is required in practice (the model reads it to choose a worker)
and becomes the first line of the worker's system message; `system_message` is optional.

```toml
[subagents.roles.report-writer]
description = "Builds and emails PDF reports."
tool_packs = ["pdf", "email"]
system_message = """\
You are a reporting sub-agent. Write the report in Markdown, render it to PDF, and email it. \
Report the file path and whether the mail was sent."""

[subagents.roles.stock-trader]
description = "Looks up quotes and places trades."
mcp_servers = ["stocks"]        # matches a [[mcp.server]].name
groups = ["compute"]
```

Neither role needs to list `"time"`. Restart Kokua and the new roles appear in the supervisor's
`spawn_subagent` menu.

## Check what you got

```bash
uv run kokua --list-tool-packs      # which packs are installed and nameable
uv run kokua --list-frontends       # the other plugin group
```

The most direct check is to ask the assistant to spawn the role and report its tools. Two quieter
signals are worth knowing about:

- A configured MCP server that no role names is reported as a warning in
  `$KOKUA_HOME/data/logs/kokua.log`. There is no console handler, so it does not appear in your
  terminal.
- A tool-pack that raises while building is skipped with a warning in the same log, and the assistant
  starts without it rather than failing.

## Where mistakes surface

The two config layers fail differently, which is the main thing to keep in mind while editing:

| Mistake | What happens |
| --- | --- |
| Unknown name in `[tools] groups` | **Startup fails** and names the key and the valid groups. |
| Unknown *value* in a role's `groups`, `mcp_servers`, or `tool_packs` | **Dropped silently.** The role builds with a smaller toolset and nothing says so. |
| Unknown *key* in a role table (`group = [...]`, say) | **Startup fails** and names the key. Only `description`, `groups`, `mcp_servers`, `tool_packs`, and `system_message` are accepted. |
| `groups = ["none"]` in `[tools]` | Starts fine; every worker has no built-in tools. |
| `groups = ["all"]` in a role | Starts fine; the worker has everything **except** `execute_python`. |
| No roles at all | Startup fails: the assistant would have no capability. |
| Tool name collides with a built-in | The built-in wins for any role that has both. |

Because a typo in a role drops quietly, re-read a new role's names against `--list-tool-packs` and your
`[[mcp.server]]` tables before concluding that a tool is broken.

## Write your own tool-pack

A tool-pack is a `kokua.plugins.ToolPack` whose `build(config)` returns
[`@aimu.tool`](https://saxman.info/aimu/how-to/add-custom-tool/) callables. Kokua discovers it through
the `kokua.tools` entry-point group, so your package needs no change to Kokua's core.

```python
# my_weather_pack/__init__.py
from aimu.tools import tool

from kokua.config import AssistantConfig
from kokua.plugins import ToolPack


def build(config: AssistantConfig) -> list:
    """Return this pack's tools. Receives the config in case a pack needs to read it."""

    @tool
    def current_conditions(city: str) -> str:
        """Report the current weather for a city.

        Args:
            city: City name, e.g. "Seattle".
        """
        return f"It is raining in {city}."

    return [current_conditions]


TOOL_PACK = ToolPack(
    name="weather",
    description="Current conditions and forecasts.",
    build=build,
)
```

```toml
# in your package's pyproject.toml
[project.entry-points."kokua.tools"]
weather = "my_weather_pack:TOOL_PACK"
```

`pip install` it, confirm with `kokua --list-tool-packs`, then give a role `tool_packs = ["weather"]`.
Installing the pack is not enough, for the reason at the top of this guide.

Three constraints on `build`:

- **It must be a pure function of the config.** Each pack is built once per agent and the resulting
  tool list is shared across every live conversation's agent when a runtime MCP change rebuilds their
  worker toolsets. A pack that carries per-call state would have that state shared in ways you did not
  plan.
- **Failure is contained, not reported loudly.** An exception is caught, logged, and the pack is
  skipped. Nothing else breaks, and nothing in the UI says the pack is missing.
- **Name tools distinctly** from AIMU's built-ins, per the deduplication rule above.

Kokua's own five packs register exactly this way in its
[`pyproject.toml`](../../pyproject.toml). If the built-in path and the plugin path ever diverge, the
plugin path is the broken one.

- [`toolpacks/example.py`](../../src/kokua/toolpacks/example.py) is the minimal template.
- [`toolpacks/email.py`](../../src/kokua/toolpacks/email.py) shows a pack that reads config and offers
  its tool only when the config and environment are complete.
- [`toolpacks/aimu_agents.py`](../../src/kokua/toolpacks/aimu_agents.py) shows the same shape carrying a
  whole AIMU agent instead of a plain function: any `Runner` exposes `.run(task) -> str`, so a tool-pack
  is the entire bridge and the core learns nothing new.

## See also

- [Add an MCP service](add-mcp-services.md): the second way to give a role tools.
- [Add a skill](add-skills.md): instructions and scripts, which reach the supervisor rather than a role.
- [Architecture](../explanation/architecture.md#the-supervisors-tools): the full inventory of the
  supervisor's own toolset, pinned by a test.
- [Design principles](../explanation/design-principles.md): why capability arrives as a plugin.
- AIMU: [add a custom tool](https://saxman.info/aimu/how-to/add-custom-tool/) for the `@tool` decorator
  rules, and [built-in tools](https://saxman.info/aimu/reference/api/tools/) for what each group holds.
