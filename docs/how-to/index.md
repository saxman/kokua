# How-to guides

Task-oriented recipes for giving Kokua new capability. Each guide answers one question: how do I add
*this*?

All three share a single rule, which is the thing to understand before reading any of them. The
assistant is a lean supervisor: it holds only cross-cutting tools (memory, skills, config, scheduling,
MCP management, the clock, reading its other conversations) and delegates every piece of specialized
work to a sub-agent worker. So a built-in tool group, a tool-pack, and an MCP server all reach an agent
by exactly one route, a `[subagents.roles.*]` table in `config.toml` that names them. Installing or
connecting something is never the last step.

## Capability

- [Set up a toolset](set-up-toolsets.md): what `[tools] groups` and a role's `groups` / `tool_packs` /
  `mcp_servers` each contribute, and how to write a tool-pack plugin of your own.
- [Add a skill](add-skills.md): the `SKILL.md` format, the one directory Kokua scans, and the three ways
  a skill gets there (by hand, `author_skill`, `add_skill_script`).
- [Add an MCP service](add-mcp-services.md): `[[mcp.server]]` and the runtime `add_mcp_server` tool,
  authentication, and what persists across a restart.

## Beyond these guides

- Every setting, documented at its default, is in
  [`config.example.toml`](../../src/kokua/config.example.toml). Run `kokua config init` to scaffold it.
- [Architecture](../explanation/architecture.md) and
  [design principles](../explanation/design-principles.md) explain *why* the core is shaped this way,
  which is the better read if a guide's rule looks arbitrary.
- Kokua is a thin application over [AIMU](https://saxman.info/aimu/), so most capability questions are
  really AIMU questions. Its [how-to guides](https://saxman.info/aimu/how-to/) cover the primitives:
  providers and models, the `@tool` decorator, MCP, skills, sub-agents, and memory.
