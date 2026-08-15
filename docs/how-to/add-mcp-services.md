# Add an MCP service

Kokua connects to remote [MCP](https://modelcontextprotocol.io) servers and hands their tools to
sub-agent workers. There are two ways in: a `[[mcp.server]]` table in `config.toml`, connected at
startup, or the assistant's own `add_mcp_server` tool, called mid-conversation. Both end at the same
place, and it is the step people miss:

> A server's tools are never mounted on the supervisor. They reach an agent only through a
> `[subagents.roles.*]` table that names the server.

Connect a server without naming it in a role and you get a working connection that no agent can use.

## Add one in `config.toml`

Each server is its own array-of-tables entry. Only `url` is required.

```toml
[[mcp.server]]
url = "https://broker.example.com/mcp"
name = "stocks"

[[mcp.server]]
url = "https://api.githubcopilot.com/mcp/"
token_env = "GITHUB_MCP_TOKEN"
```

| Key | Required | Purpose |
| --- | --- | --- |
| `url` | yes | The server endpoint. |
| `name` | no | A friendly label a role can reference instead of the full URL. |
| `token_env` | no | Name of an **environment variable** holding a bearer token. Never the token itself. |

Then give a role the server, and restart:

```toml
[subagents.roles.stock-trader]
description = "Looks up quotes and places trades."
mcp_servers = ["stocks"]        # the [[mcp.server]].name, or the raw url
groups = ["compute"]
```

A reference is matched against the configured `name` first, then the raw URL. An unmatched reference is
dropped silently, so a typo in `mcp_servers` produces a worker with fewer tools and no error.

Unknown keys and wrong types in `[[mcp.server]]` are hard startup errors that name the offending server.

## Authentication

Three modes, chosen automatically:

- **None.** An unprotected server connects directly.
- **OAuth.** On an auth challenge, the flow starts by itself: the assistant posts an authorization link
  into the chat and opens a browser window. Once you approve, the connection completes and the token is
  saved under `$KOKUA_HOME/data/mcp-oauth/` for later sessions.
- **Bearer token.** Set `token_env` to the name of an environment variable, exported however you already
  manage secrets. It is read at connect time, so the token never enters `config.toml`.

Some servers require authentication but cannot use the automatic OAuth flow, because they do not support
dynamic client registration. `add_mcp_server` detects this and returns a message asking for a token
rather than failing opaquely; the assistant relays that request to you.

## Add one mid-conversation

The supervisor carries `add_mcp_server` and `remove_mcp_server`, so you can just ask:

> "Connect to https://broker.example.com/mcp and use it to check my positions."

`add_mcp_server(url, bearer_token=None)` connects, reports the tool names it found, and rebuilds every
live conversation's `spawn_subagent` so the roles that name the server pick up its tools **in the same
turn**. It is gated by `[security] confirm_tools`, so it waits for your approval first.

What persists differs by auth mode, and this is the one asymmetry to remember:

| Auth mode | Written to `config.toml`? | Survives restart? |
| --- | --- | --- |
| None | yes, URL only | yes |
| OAuth | yes, URL only | yes (the saved token is reused) |
| Bearer token | **no** | **no**: the secret is not written to disk |

So a bearer-token server added at runtime is session-only by design. To keep it, add a `[[mcp.server]]`
table with `token_env` by hand.

Two consequences of the runtime path being real:

- A server added at runtime is written with its URL and no `name`, so a role that should use it must
  reference the raw URL, or you must add the `name` by hand afterwards.
- `remove_mcp_server` disconnects and drops the entry, again fanned out to every live agent.

## Check that it reached an agent

A connected-but-unreachable server is the failure mode this design invites, so Kokua looks for it at
startup:

```
MCP server 'stocks' is configured but no [subagents.roles.*] names it in `mcp_servers`;
the supervisor mounts no MCP tools itself, so this server reaches no agent.
```

That warning goes to `$KOKUA_HOME/data/logs/kokua.log`. There is no console log handler, so **it does not
appear in your terminal**; check the file, or ask the assistant to spawn the role and list its tools. The
server still connects and still spends a handshake either way. The check lives in
[`unreferenced_mcp_servers`](../../src/kokua/core/build.py).

## The `--mcp` flag

```bash
uv run kokua --mcp https://broker.example.com/mcp --mcp https://other.example.com/mcp
```

Two caveats, both from the flag-over-file precedence rule:

- It **replaces** the entire `[[mcp.server]]` list for that run rather than adding to it, so any server
  configured in the file is skipped.
- Flag-supplied servers get no `name` and no `token_env`. A role must reference them by raw URL, and a
  server needing a static bearer token needs the config file instead.

## Security

An MCP server's tools run on your behalf, with whatever access the worker calling them has. The tool
descriptions the server publishes are also model-visible text you did not write, which makes a hostile
server an injection vector as well as an ordinary trust decision.

- **Only connect servers you trust.** This is the same class of decision as installing a package.
- Because `add_mcp_server` lets the assistant expand its own capability, it is gated by default, and
  proactive or backgrounded turns auto-deny it. A scheduled task cannot connect a new server unattended.
- A server reaches only the roles that name it. Prefer a narrow role over adding the server to
  `generalist`: that is the practical use of the "a role must name it" rule, rather than an obstacle to
  work around.

## Write your own server

Kokua consumes MCP over HTTP and has no opinion about how a server is built. AIMU's
[use MCP tools](https://saxman.info/aimu/how-to/use-mcp-tools/) covers standing one up with FastMCP and
exposing tools over it; point Kokua at the result with a `[[mcp.server]]` table.

If the capability you want is local Python rather than a separate process, a tool-pack is less machinery
for the same result. See [Set up a toolset](set-up-toolsets.md#write-your-own-tool-pack).

## See also

- [Set up a toolset](set-up-toolsets.md): how a role assembles tools from groups, packs, and servers.
- [Add a skill](add-skills.md): the capability that stays with the supervisor.
- [Architecture](../explanation/architecture.md): where the MCP subsystem sits, and the connection
  lifecycle.
- AIMU: [use MCP tools](https://saxman.info/aimu/how-to/use-mcp-tools/) and
  [A2A vs MCP](https://saxman.info/aimu/explanation/a2a-vs-mcp/).
