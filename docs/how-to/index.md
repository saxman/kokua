# How-to guides

Task-oriented recipes for giving Kokua new capability. Each guide answers one question: how do I add
*this*?

Extending Kokua is one of the three ways to learn from it, alongside reading it and running it (see
[why Kokua exists](../explanation/design-principles.md#why-kokua-exists)). These guides are the short
path through the third: each ends with something of yours running, and none of them asks you to change
Kokua's core.

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
- [Install a third-party toolset](install-a-third-party-toolset.md): the installer's side of that same
  seam, for a toolset someone else already wrote: installing the package, declaring it, giving it its own
  config section, and gating its expensive tool, with jobme as the worked example.
- [Add a skill](add-skills.md): the `SKILL.md` format, the one directory Kokua scans, and the three ways
  a skill gets there (by hand, `author_skill`, `add_skill_script`).
- [Add an MCP service](add-mcp-services.md): `[[mcp.server]]` and the runtime `add_mcp_server` tool,
  authentication, and what persists across a restart.
- [Back up to GitHub](back-up-to-github.md): the `github_backup` toolset, a private repository and a
  scoped token, and the scheduled task that runs it unattended.

## Working with conversations

- [Export a conversation](export-a-conversation.md): `kokua export` writes one saved conversation as
  Markdown you can diff, paste into a review, or keep once the terminal is gone, whether from the
  command line or the web UI's sidebar.

## Beyond these guides

- [Configuration reference](../reference/configuration.md): every `config.toml` key, what it accepts,
  which keys apply live, and who may write each one. Its short form is
  [`config.example.toml`](https://github.com/saxman/kokua/blob/main/src/kokua/config.example.toml), one line per key, which
  `kokua config init` scaffolds for you.
- [Architecture](../explanation/architecture.md) and
  [design principles](../explanation/design-principles.md) explain *why* the core is shaped this way,
  and why it is shaped to be read. Either is the better next stop if a guide's rule looks arbitrary.
- Kokua is a thin application over [AIMU](https://saxman.info/aimu/), so most capability questions are
  really AIMU questions. Its [how-to guides](https://saxman.info/aimu/how-to/) cover the primitives:
  providers and models, the `@tool` decorator, MCP, skills, sub-agents, and memory.
