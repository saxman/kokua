# Add an MCP service

Kokua connects to remote [MCP](https://modelcontextprotocol.io) servers and hands their tools to the
agents that ask for them. There are two ways in: a `[[mcp.server]]` table in `config.toml`, connected at
startup, or the assistant's own `add_mcp_server` tool, called mid-conversation. Both end at the same
place, and it is the step people miss:

> A configured server is a **toolset**, named by its `name`. It reaches an agent only when that agent's
> `tools` list in `[agents.<name>]` names it.

Connect a server without naming it in an agent and you get a working connection nothing can use.

## Add one in `config.toml`

Each server is its own array-of-tables entry. `url` and `name` are both required.

```toml
[[mcp.server]]
url = "https://broker.example.com/mcp"
name = "stocks"

[[mcp.server]]
url = "https://api.githubcopilot.com/mcp/"
name = "github"
token_env = "GITHUB_MCP_TOKEN"
```

| Key | Required | Purpose |
| --- | --- | --- |
| `url` | yes | The server endpoint. |
| `name` | **yes** | How the server enters the toolset namespace, and therefore the only way an agent can name it. |
| `token_env` | no | Name of an **environment variable** holding a bearer token. Never the token itself. |

`name` shares one namespace with every other toolset, so it must not collide with a built-in group
(`web`, `fs`, `compute`, ...), a core capability (`memory`, `config`, `scheduling`, ...), an installed
plugin toolset, or another server. A collision is a startup error naming both claimants;
`kokua --list-toolsets` prints every name already taken.

Then give an agent the server, and restart:

```toml
[agents.stock-trader]
description = "Looks up quotes and places trades."
tools = ["stocks", "compute", "time"]     # "stocks" is the [[mcp.server]].name above
```

A name in `tools` that matches no toolset is a **startup error** listing the available names, so a typo
here cannot quietly produce an agent with fewer tools than you wrote. Unknown keys and wrong types in
`[[mcp.server]]` are hard startup errors that name the offending server, as they always were.

The server's tools are resolved from the live connections when the agent is built, so a server that is
configured but not currently connected simply contributes nothing rather than failing the build.

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

The shipped entry agent declares the `mcp-admin` toolset, so it carries `add_mcp_server` and
`remove_mcp_server` and you can just ask:

> "Connect to https://broker.example.com/mcp and use it to check my positions."

`add_mcp_server(url, bearer_token=None)` connects, reports the tool names it found, and rebuilds every
live agent's `spawn_subagent`. It is gated by `[security] confirm_tools`, so it waits for your approval
first.

**What that connection can actually reach depends on whether the server was already in `config.toml` at
startup, and the difference is the whole story of this section:**

- **A server already declared in `[[mcp.server]]`** (even one that was unreachable at startup, or that
  you disconnected earlier in the session) already has a name in the toolset namespace, so an agent's
  `tools` may already reference it. Connecting it reaches the next **worker** spawned by such an agent, in
  the same turn. The agent you are talking to is the exception even here: it resolved its own tools when
  it was built, so if *it* declares the server, it picks them up only the next time it is built (a new
  conversation, or a restart).
- **A server new to this session reaches nothing at all until you restart.** The toolset namespace is
  built once, at startup, from the `[[mcp.server]]` tables as they were then, and `config.toml` is not
  reread mid-process. So a newly connected server's name is not in the namespace, no `[agents.*]` table
  can reference it yet, and editing that file while Kokua runs changes nothing until the next start.
  `add_mcp_server` has recorded the server for you; the remaining two steps are yours.

This is why the tool's own reply tells the model it cannot use the new tools itself, and it is worth
knowing before you ask the assistant to "connect to X and use it": for a genuinely new X, it can do the
first half and not the second.

What persists differs by auth mode, and this is the one asymmetry to remember:

| Auth mode | Written to `config.toml`? | Survives restart? |
| --- | --- | --- |
| None | yes, URL + a derived name | yes |
| OAuth | yes, URL + a derived name | yes (the saved token is reused) |
| Bearer token | **no** | **no**: the secret is not written to disk |

So a bearer-token server added at runtime is session-only by design. To keep it, add a `[[mcp.server]]`
table with `token_env` by hand.

Two consequences of the runtime path being real:

- **Finishing a runtime add takes an edit and a restart.** Since every server needs a name, the write
  derives one from the server's host (`broker.example.com` becomes `broker-example-com`, with a numeric
  suffix if that name is already taken), so the config it leaves behind always loads. Then:
  1. add that name (or rename it to something friendlier) to an agent's `tools` by hand. `[agents.*]` is
     hand-edit only: `update_config` refuses the whole section, precisely so the assistant cannot grant
     itself the capability it just connected;
  2. **restart Kokua**, because the namespace and the agent tables are both read only at startup.

  Until you do, the server connects, reconnects on each start, and startup warns that nothing names it.
- `remove_mcp_server` disconnects, drops the entry, and fans the rebuild out to every live agent. A worker
  spawned after it will not have the server's tools.

If you want a server the assistant can connect *and use* within one session, declare it in
`[[mcp.server]]` and name it in an agent up front. It does not have to be reachable at startup: an
unconnected server contributes no tools rather than failing the build, so a later `add_mcp_server` for the
same URL reaches that agent's next worker immediately.

## Check that it reached an agent

A connected-but-unreachable server is the failure mode this design invites, so Kokua looks for it at
startup:

```
Toolset 'stocks' is provisioned but no [agents.*] table names it in `tools`, so it reaches no agent.
```

That warning goes to `$KOKUA_HOME/data/logs/kokua.log`. There is no console log handler, so **it does not
appear in your terminal**; check the file, or ask the assistant to delegate to the agent and list its
tools. The server still connects and still spends a handshake either way. The check lives in
[`unreferenced_toolsets`](../../src/kokua/toolsets/agents.py), and it reports installed plugins in the
same position for the same reason.

## The `--mcp` flag

```bash
uv run kokua --mcp https://broker.example.com/mcp --mcp https://other.example.com/mcp
```

Two caveats, both from the flag-over-file precedence rule:

- It **replaces** the entire `[[mcp.server]]` list for that run rather than adding to it, so any server
  configured in the file is skipped, and an agent naming one of those servers then fails startup with an
  unknown-toolset error.
- Flag-supplied servers get a name derived from their host and no `token_env`. That name is what an agent
  must already declare for the server to reach anything, and a server needing a static bearer token needs
  the config file instead.

## Security

An MCP server's tools run on your behalf, with whatever access the agent calling them has. The tool
descriptions the server publishes are also model-visible text you did not write, which makes a hostile
server an injection vector as well as an ordinary trust decision.

- **Only connect servers you trust.** This is the same class of decision as installing a package.
- Because `add_mcp_server` lets the assistant expand its own reach, it is gated by default, and proactive
  or backgrounded turns auto-deny it. A scheduled task cannot connect a new server unattended, and even an
  approved connection cannot reach an agent without your hand-edit.
- A server reaches only the agents that name it. Prefer a narrow agent over adding the server to
  `generalist`: that is the practical use of the "an agent must name it" rule, rather than an obstacle to
  work around.

## Write your own server

Kokua consumes MCP over HTTP and has no opinion about how a server is built. AIMU's
[use MCP tools](https://saxman.info/aimu/how-to/use-mcp-tools/) covers standing one up with FastMCP and
exposing tools over it; point Kokua at the result with a `[[mcp.server]]` table.

If the capability you want is local Python rather than a separate process, a toolset plugin is less
machinery for the same result. See
[Set up a toolset](set-up-toolsets.md#write-your-own-toolset).

## See also

- [Set up a toolset](set-up-toolsets.md): the one namespace a server's `name` joins, and how an agent
  declares what it holds.
- [Add a skill](add-skills.md): the one capability only the entry agent can hold.
- [Architecture](../explanation/architecture.md): where the MCP subsystem sits, and the connection
  lifecycle.
- AIMU: [use MCP tools](https://saxman.info/aimu/how-to/use-mcp-tools/) and
  [A2A vs MCP](https://saxman.info/aimu/explanation/a2a-vs-mcp/).
