# How-to guides

Task-oriented recipes for giving Kokua new capability. Each guide answers one question: how do I add
*this*?

All three share a single rule, which is the thing to understand before reading any of them. **A
capability is declared, never defaulted.** Every capability an agent can hold is a named *toolset* in one
namespace, and a built-in tool group, an installed plugin toolset, and a connected MCP server all reach an
agent by exactly one route: an `[agents.<name>]` table in `config.toml` whose `tools` list names it.
Installing or connecting something is never the last step, and no code path adds a tool an agent did not
name. The one exception is reached by declaration too: an agent whose table names `capabilities` can
compose a sub-agent out of anything installed, for the length of one task. Two names are out of
reach even there: `skills`, which only the entry agent may hold, and `capabilities` itself, which
would hand a worker a fresh composition budget.

## Capability

- [Set up a toolset](set-up-toolsets.md): the one namespace and what is in it, declaring an agent's
  `tools` and `delegates_to`, and how to write a toolset plugin of your own.
- [Add a skill](add-skills.md): the `SKILL.md` format, the one directory Kokua scans, and the three ways
  a skill gets there (by hand, `author_skill`, `add_skill_script`).
- [Add an MCP service](add-mcp-services.md): `[[mcp.server]]` and the runtime `add_mcp_server` tool,
  authentication, and what persists across a restart.

## Beyond these guides

- [Configuration reference](../reference/configuration.md): every `config.toml` key, what it accepts,
  which keys apply live, and who may write each one. Its short form is
  [`config.example.toml`](../../src/kokua/config.example.toml), one line per key, which
  `kokua config init` scaffolds for you.
- [Architecture](../explanation/architecture.md) and
  [design principles](../explanation/design-principles.md) explain *why* the core is shaped this way,
  which is the better read if a guide's rule looks arbitrary.
- Kokua is a thin application over [AIMU](https://saxman.info/aimu/), so most capability questions are
  really AIMU questions. Its [how-to guides](https://saxman.info/aimu/how-to/) cover the primitives:
  providers and models, the `@tool` decorator, MCP, skills, sub-agents, and memory.
