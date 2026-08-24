# Configuration reference

Every `config.toml` key Kokua reads, what it accepts, and what it does. This is the long form of
[`config.example.toml`](../../src/kokua/config.example.toml), which is the same set of keys with a line
of description each; read that file to see the shape, and this one when a key's behavior is not obvious
from its name.

Two rules run through the whole file and explain most of what follows:

- **A capability is declared, never defaulted.** An agent holds exactly the toolsets its
  `[agents.<name>].tools` list names. No code path adds one it did not name, and no flag can disagree
  with the declaration. See [set up a toolset](../how-to/set-up-toolsets.md).
- **`config.toml` is the single source of settings, and the app writes it too.** There is no second
  store, no dotfile, no database. The assistant's own `update_config` tool and the runtime
  `add_mcp_server` tool write back to this file, preserving your comments, which is why
  `[security].locked_config_keys` exists, and why it is the one key that list cannot unlock.

## Contents

- [Where the file lives](#where-the-file-lives)
- [Precedence](#precedence)
- [What happens when a key is wrong](#what-happens-when-a-key-is-wrong)
- [Who may change which key](#who-may-change-which-key)
- [Which keys apply live](#which-keys-apply-live)
- Sections: [`[assistant]`](#assistant) · [`[assistant.generation]`](#assistantgeneration) ·
  [`[security]`](#security) · [`[display]`](#display) · [`[agents.<name>]`](#agentsname) ·
  [`[[mcp.server]]`](#mcpserver) · [`[paths]`](#paths) · [`[frontend]`](#frontend) · [`[web]`](#web) ·
  [`[logging]`](#logging) · [`[email]`](#email) · [`[scheduling]`](#scheduling) ·
  [`[scheduling.task.<name>]`](#schedulingtaskname) · [`[planning]`](#planning) ·
  [`[capabilities]`](#capabilities) · [`[github_backup]`](#github_backup) ·
  [toolset sections in general](#toolset-sections)
- [Environment variables](#environment-variables)
- [Command-line flags](#command-line-flags)

## Where the file lives

Kokua reads the first location that is specified, and only that one:

1. `--config PATH`
2. `$KOKUA_CONFIG`
3. `$KOKUA_HOME/config.toml`, the default, where `$KOKUA_HOME` itself defaults to `~/.kokua`

A file missing from the *default* location is not an error at this stage; a file missing from a location
you named explicitly is, so a typo in `--config` fails loudly instead of silently running on defaults.

Scaffold one with:

```bash
kokua config init             # writes the documented example to the config location
kokua config init --force     # overwrite an existing file
kokua config init --path PATH # write somewhere else
```

**The file is required**, and Kokua will not start without one. Every individual *key* has a built-in
default, so you set only what you want to change, but the `[agents.*]` tables exist nowhere else and an
assistant with no agent cannot work. There is no useful "no config" state to fall back to, so startup
fails with a message pointing at `kokua config init`.

## Precedence

Highest wins:

**command-line flag > `config.toml` > built-in default**

For the reasoning-effort and generation tiers, AIMU adds precedence *below* the config file. Resolved
lowest first: the model client's own fallbacks, then the model card's tuned sampling profile, then what
Kokua writes from `[assistant.generation]` and `[agents.<name>.generation]`, then a per-call override.
The practical consequence is in [`[assistant.generation]`](#assistantgeneration): a key you set replaces
the card's recommendation for that one parameter, and a key you leave out keeps the card's value.

## What happens when a key is wrong

An unknown key, an unknown section, or a wrong-typed value is a **hard startup error** naming the key.
Kokua does not skip a line it cannot parse. A few keys that existed in earlier layouts are recognized
specifically so the error can say where they moved to, rather than reporting them as unknown.

`update_config` runs the same validation before it writes, so the assistant cannot save a value that
would prevent the next startup. `[assistant].model` gets one extra check there that the file itself does
not do: the string must resolve to a client this process could build, because a startup-only key written
by a tool outlives the conversation that wrote it.

## Who may change which key

`[security].locked_config_keys` decides this, and it is yours to set. Kokua ships with:

```toml
[security]
locked_config_keys = ["security.*", "email.to", "paths.data_dir", "agents.*", "scheduling.task.*"]
```

A pattern takes one of three forms:

| Form | Matches |
| --- | --- |
| `*` | every section and key |
| `<section>.*` | that section, every section under it, any key |
| `<section>.<key>` | exactly that key in exactly that section |

Keys never contain dots and sections do, so the last segment of the third form is always the key. To
lock one task's contents, write `"scheduling.task.morning-brief.*"`, not
`"scheduling.task.morning-brief"`. Anything that is none of the three forms fails startup naming the
pattern, and so does a pattern whose shape is right but whose section or key does not exist: `agnets.*`,
`Agents.*`, and `security.confirm_tool` are all refused. What no check can catch is a name only you
bring into being. `agents.resercher.*` and `scheduling.task.mornin-brief.*` name an agent and a task
that could be created tomorrow, so there is no closed set to test them against, and each one loads while
locking nothing. See [`locked_config_keys`](#locked_config_keys) for the full list of what is refused.

**One key is always locked:** `[security].locked_config_keys` itself, whatever the list says, including
when it is empty. Otherwise an assistant holding `update_config` would need a single call to disable
every other lock. Note how narrow that is: only this key, not the `[security]` section around it. Set
`locked_config_keys = ["email.to"]` and `[security].confirm_tools` becomes writable by the assistant,
because the shipped `security.*` pattern was the only thing that was locking it.

Everything else is yours to remove, and here is what removing each shipped pattern actually permits:

| Pattern | Removing it |
| --- | --- |
| `security.*` | lets the assistant change `confirm_tools`, and so remove its own approval gate |
| `email.to` | lets the assistant mail someone other than you |
| `paths.data_dir` | lets the assistant move its own state out from under you |
| `agents.*` | lets the assistant rewrite any agent's `tools`, `model`, `thinking`, `system_message`, `description`, and `delegates_to`, set its `[agents.<name>.generation]` parameters, and create new agents. It can widen its own reach, effective on the next restart. |
| `scheduling.task.*` | changes the error message only. `update_config` still cannot write a task: the scheduling tools are the write path, because a task write has to be paired with the scheduler arming or disarming to match, and a bare config write would leave the running scheduler firing the old schedule. |

A flat agent key (`tools`, `model`, `thinking`, `system_message`, `description`, `delegates_to`) is
checked before it is saved by the same `validate_agents` that runs at startup, so an unknown toolset
name, an unresolvable model, an unknown delegate, or a delegation cycle is refused at the tool rather
than breaking your next launch. That check reads `config.toml` itself, not the copy this session started
from, and reads both halves of it that startup reads: the agent tables and the `[assistant].agent`
naming the entry agent. So two writes that each looked fine alone cannot add up to a file your next
launch refuses. A `[agents.<name>.generation]` parameter gets a different check instead:
the same type-and-range check `[assistant.generation]` gets, not a `validate_agents` dry run, since a bad
`temperature` cannot break startup the way a bad delegate can. Either way, the write is checked before
it is saved; neither check tells you the result is one you wanted, only that it starts.

`[[mcp.server]]` is not in the list and is not writable by `update_config` either way; it is appended by
`add_mcp_server`, which connects the server as well as recording it.

## Which keys apply live

A **hot** key takes effect on the next turn, with no restart, and is written back to `config.toml`
immediately. Everything else is **startup-only**: change it, then restart.

| Hot | Startup-only |
| --- | --- |
| `[display].show_thinking`, `[display].show_tools` | everything in `[assistant]`, including `[assistant.generation]` |
| `[planning].plan_review`, `plan_review_agent`, `result_review`, `show_reasoning` | `[planning].review_rounds` |
| `[capabilities].max_depth` | all of `[agents.*]`, `[mcp]` (including `[[mcp.server]]`), `[security]`, `[paths]`, `[frontend]`, `[web]`, `[logging]`, `[email]` |
| `[scheduling].max_task_conversations` | |

The model and the reasoning effort read like runtime settings and are not. Nothing rebinds a live model
client, and no runtime writer can reach `[agents.*]`, so a hot `[assistant].model` could only ever report
a change it had not made, or disagree with an agent's own declaration. Generation parameters are
startup-only for a second reason as well: a live control always holds *some* value, so it would write
that tier whether or not you asked for anything, shadowing every model card's tuned profile. A key absent
from the file has to stay absent from the request.

A toolset declares its own settings and says which of them are hot. See [toolset
sections](#toolset-sections).

---

## `[assistant]`

Process-wide defaults. Everything here is read at startup.

### `model`

The default model every agent runs on. An agent that declares its own `model` runs on that instead.

```toml
model = "ollama:qwen3:8b"
```

Unset, Kokua resolves `$AIMU_LANGUAGE_MODEL`, and failing that the first already-running local model it
finds: a running Ollama server first, then a local OpenAI-compatible server. **A cloud model is never
auto-selected.** If nothing resolves, startup fails with an actionable message.

The string has two optional suffixes for servers AIMU does not know about:

| Form | Meaning |
| --- | --- |
| `provider:model` | the ordinary case |
| `provider:model@<base_url>` | override the endpoint (llama.cpp `llama-server`, vLLM, LM Studio, SGLang, HF-Serve) |
| `provider:model@<base_url>;<flags>` | also declare capabilities for a model id not built into AIMU |

`<flags>` is comma-separated from `tools`, `thinking`, `vision`, `audio`, `structured`.

```toml
model = "llamaserver:qwen3-8b.gguf@http://gpu-box:8080/v1"
model = "openai-compat:my-model@http://gpu-box:9000/v1;tools,thinking"
```

### `thinking`

The default reasoning effort every agent runs at. An agent that declares its own `thinking` runs at that
instead.

| Value | Effect |
| --- | --- |
| unset (default) | send nothing; the model keeps its own behavior |
| `false` | do not reason |
| `true` | reason, at the model's own default effort |
| `"low"`, `"medium"`, `"high"` | reason at this effort |

Two caveats. A level is **advisory** on a model whose card does not declare effort-level support: AIMU
warns once, and reasoning is still on, just not at the level asked for. And `false` additionally selects
the model card's instruct-mode sampling profile where the card specifies one (the Qwen 3.5, 3.6, and 3.8
cards do; most models have a single profile and are unaffected), because a model's thinking-mode and
non-thinking-mode sampling defaults are usually different.

An agent's declaration is resolved on "is it unset", not on truthiness, so `thinking = false` on an agent
genuinely overrides a `"high"` default rather than being swallowed by it.

This key is not the last word: a single turn can ask for its own effort from the web composer's picker
or the CLI's `/think`, beating whatever this key and an agent's own declaration resolve to for that turn
alone. Nothing about the request is stored in `config.toml`; see
[Architecture](../explanation/architecture.md#which-model-an-agent-runs-on-and-how-hard-it-thinks) for
the full resolution order.

### `system_message`

Fallback opener for any agent that sets no `system_message` of its own. The `--system` flag overrides the
*entry* agent's opener for one run, and leaves a worker's declared opener alone.

### `agent`

Which `[agents.<name>]` table is the agent you talk to, and the root of the delegation graph.
Default `"assistant"`.

### `concurrent_tools`

Run independent tool calls in one turn concurrently, so several `spawn_subagent` calls overlap.
Default `true`.

### `load_plugins`

Discover toolset plugins through the `kokua.toolsets` entry-point group. Default `true`. `--no-plugins`
turns it off for one run. Turning it off does not change what an agent holds unless that agent named a
plugin toolset, in which case startup fails on the now-unknown name.

### `agent_cache_cap`

Maximum per-conversation agents kept live in memory. Default `8`. An evicted agent rebuilds from
persisted state on next access, so the cap bounds memory, not correctness.

## `[assistant.generation]`

Sampling and length parameters for every agent, each overridable per key by an agent's own
`[agents.<name>.generation]` table. Startup-only.

| Key | Type | Range |
| --- | --- | --- |
| `temperature` | number | 0.0 to 2.0 |
| `top_p` | number | 0.0 to 1.0 |
| `top_k` | integer | at least 1 |
| `min_p` | number | 0.0 to 1.0 |
| `presence_penalty` | number | -2.0 to 2.0 |
| `repetition_penalty` | number | greater than 0.0 |
| `max_tokens` | integer | at least 1 |
| `context_length` | integer | at least 1 |

There is no default for any of them, and that is the design rather than an omission. **Only the keys you
set are sent.** This tier sits above the model card's own tuned profile in AIMU's precedence chain, so a
value set here replaces the card's recommendation for that one parameter and leaves the rest of the
profile in force. Setting nothing keeps the whole profile.

Three things to know:

- **A parameter the backend cannot take is dropped**, with a warning naming where to set it instead
  (Ollama has no `min_p`; the Anthropic API has no penalties). A value that works locally therefore does
  not break a cloud model, it just stops applying. That warning goes only to the rotating log at
  `data/logs/kokua.log` under your Kokua home, not to the chat or the terminal, so look there when a
  parameter you set has no effect.
- **`max_tokens` and `context_length` are different knobs and they interact.** `max_tokens` caps
  *generated* tokens; `context_length` sizes the whole window, which the prompt and the generated tokens
  share. With the window at 32768 and the cap at 4096, roughly 28k is left for the system prompt, the
  tool block, and the conversation. Note also that AIMU's own fallback sets `max_tokens = 1024` on
  Anthropic, the OpenAI-compatible family, and llama.cpp, which is low for a turn carrying tool results.
- **`context_length` is set per request only on Ollama's native API.** Everywhere else the window is
  fixed at model load time, at server launch (`--ctx-size`, `--max-model-len`), or by the vendor, and the
  key is dropped with a warning naming that remedy.

Because this is a TOML sub-table, it must come **last** in `[assistant]`: any plain `[assistant]` key
written after the `[assistant.generation]` header would belong to the sub-table instead. The same is true
of an agent's own generation table.

## `[security]`

### `confirm_tools`

Tools that require interactive confirmation before each call: a terminal `y/N`, or Allow/Deny in the web
UI. These are the tools that run with full machine access.

```toml
confirm_tools = ["add_skill_script", "add_mcp_server", "execute_python", "update_config"]
```

Set to `[]` to disable approval entirely. Proactive turns (scheduled tasks, anything the assistant starts
unprompted) **auto-deny** these regardless of the setting, since there is no one at the keyboard to ask.

`update_config` is in the default list because it lets the assistant rewrite this file, except for the
locked keys it can never change. This key is itself locked by default too, for the obvious reason.

Gating is **by tool name**, so it applies to a worker's call as much as the entry agent's: a worker's
gated call is routed to you. Note the flip side, which is easy to misread: there is no privilege tier
among agents. An agent whose table declares `config` really does get `update_config`, and one declaring
`compute` really does get `execute_python`. Your hand-edit is the consent, and this list is the gate at
call time.

**A name no configured agent provides fails startup**, naming every entry that matched nothing and
suggesting the close names. A gate is a plain name match, so a misspelled entry holds nothing back, and
the only sign of it is a prompt that never comes, which is not a thing anyone notices:
`confirm_tools = ["execute_pythn"]` once loaded clean and then let `execute_python` run unattended for
as long as the file stood. The vocabulary is every tool this config builds, which is wider than the
entry agent's own: `execute_python` is there because `[agents.coder]` declares `compute`, and
`spawn_subagent` and a skill's `{skill}__{script}` tools count as well. An empty list names nothing, so
there is nothing for the check to refuse.

The limit is anything that does not exist when startup ends. A tool from an MCP server the assistant
connects later with `add_mcp_server` has no name to match yet, and neither does a tool only
`compose_worker` would build, out of a toolset no `[agents.*]` table names. Listing either one is
refused, which will look wrong if you were deliberately gating ahead of the connection. To gate an MCP
tool, give its server a `[[mcp.server]]` table here and name that server in an agent's `tools`; to gate
a tool from a toolset nothing declares, name that toolset in an agent's `tools`. Both are known from the
next start.

Two names worth considering adding, both ungated by default: `read_conversation` and
`search_conversations`. A saved transcript is untrusted text, since a worker may have pasted web content
into it, so an injection landing in one conversation can influence another. They are ungated by default
because gating a read would make an unattended scheduled run that reads history fail silently.

### `locked_config_keys`

A list of patterns naming which keys `update_config` refuses. Default:
`["security.*", "email.to", "paths.data_dir", "agents.*", "scheduling.task.*"]`. Startup-only, and
always locked against `update_config` regardless of its own value. See
[Who may change which key](#who-may-change-which-key) for the pattern forms and what each shipped
pattern is holding back. A pattern that could never match anything is a hard startup error rather than a
line that silently locks nothing, and two checks decide that.

The first is the pattern's shape: a bare `display` with no dot, whitespace at either end of the pattern,
an empty segment (`agents.`), a `*` sharing a segment with other characters (`agent*.tools`), or a `*`
anywhere but the last segment (`*.*`, `agents.*.*`) all fail. The second is its vocabulary, read off the
schema this install actually has, so it knows the sections your installed toolsets contribute as well as
Kokua's own. The first segment must be a real section, which is what refuses `agnets.*` and, since TOML
keys are case-sensitive, `Agents.*`. An exact `<section>.<key>` in a section whose keys are known must
name one of them, which is what refuses `security.confirm_tool` while accepting
`security.confirm_tools`.

Neither check can see a name that does not exist yet, and neither tries to. The `[agents.<name>]` and
`[scheduling.task.<name>]` sections are yours to create, so `agents.resercher.*` is accepted and locks
nothing until an agent by that name exists. Locking a section you are about to add is a legitimate thing
to write; a misspelling of one is indistinguishable from it.

## `[display]`

Both keys are hot: change one with `update_config` and it applies from the next turn and is written
straight back to the file.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `show_thinking` | bool | `true` | stream the model's reasoning into the channel |
| `show_tools` | bool | `true` | stream tool calls into the channel |

## `[agents.<name>]`

Every agent, declared whole. The table name is the agent's name: it is what `[assistant].agent` selects,
what another agent's `delegates_to` names, and what `spawn_subagent` takes as its `agent_type`.

**This whole section is locked by default.** See [who may change which key](#who-may-change-which-key)
for what removing `agents.*` from `[security].locked_config_keys` actually permits.

| Key | Type | Meaning |
| --- | --- | --- |
| `description` | string | the label a delegator sees in its worker menu |
| `system_message` | string | this agent's opener; falls back to `[assistant].system_message` |
| `tools` | list of strings | **this agent's capability**, named from the one toolset namespace |
| `delegates_to` | list of strings | agent names this agent may spawn as workers |
| `model` | string | overrides `[assistant].model` for this agent alone |
| `thinking` | bool or level | overrides `[assistant].thinking` for this agent alone |
| `generation` | sub-table | overrides `[assistant.generation]`, **per key** |

### `tools`

An agent's `tools` list *is* its capability. Nothing is added in code, so a toolset you delete from a
list is gone, and one you add is there on the next start.

There is one namespace for every capability, so a name may be an AIMU built-in tool group (`web`, `fs`,
`compute`, `time`, `misc`, `audio`, `speech`, `transcription`), one of Kokua's own (`memory`,
`documents`, `skills`, `capabilities`, `config`, `conversations`, `mcp-admin`, `planning`, `scheduling`),
an installed plugin toolset (`aimu_agents`, `github_backup`, `image`), a skill in your skills folder
named by its own name, or an MCP server configured under `[[mcp.server]]`, named by its `name`. The list
does not say which kind a name is. Run `kokua --list-toolsets` for every name this install accepts,
grouped by what provides it.

An unknown name is a startup error listing the valid ones, so a typo can never quietly leave an agent
with a smaller toolset than you wrote. Nothing is implicit, including the clock: `time` is added to no
agent in code, which is why every shipped agent lists it. An installed plugin toolset and a configured
MCP server likewise do nothing until some agent names them, and startup warns about one that nothing
names.

The full inventory of what each toolset contains is in [set up a
toolset](../how-to/set-up-toolsets.md#one-namespace).

### `delegates_to`

A non-empty `delegates_to` is what makes an agent a delegator. There is no separate switch that could
disagree with it. The agent gets a `spawn_subagent(agent_type, task)` tool offering exactly the agents it
names, each built from its own table. The graph must be acyclic.

A worker's gated tool calls (see [`[security]`](#security)) are routed to you for approval, not run
unattended.

### `model`, `thinking`, `generation`

All three are resolved per agent and **never inherited down the delegation graph**: a delegator that pins
a big model, reasons hard, or runs cold does not drag its workers along. A worker declaring nothing runs
on the `[assistant]` defaults like every other undeclared agent.

An agent's `model` is the same string [`[assistant].model`](#model) is, suffixes included: a worker can
be pinned to its own endpoint, or to the same remote server the assistant uses. Worth stating because the
two are checked by different code (a worker's is parsed at startup, `[assistant].model` by building a
throwaway client), so a reader has no way to tell from the code alone that they accept the same thing.

`generation` merges per key rather than table for table, so an agent that wants only a colder temperature
keeps the default's context length.

```toml
[agents.researcher]
description = "Research specialist: gather and verify information from the web."
model = "ollama:qwen3:32b"     # this worker alone runs on a bigger model
thinking = "high"              # ...and reasons harder than the rest
tools = ["web", "misc", "time"]
system_message = "You are a research sub-agent. ..."

[agents.researcher.generation]  # last in the table, like [assistant.generation]
temperature = 0.2
context_length = 131072
```

A model string AIMU cannot resolve fails startup naming the table it came from.

## `[[mcp.server]]`

Remote MCP servers to connect at startup. An array of tables: one `[[mcp.server]]` header per server.

| Key | Required | Meaning |
| --- | --- | --- |
| `url` | yes | the server's endpoint |
| `name` | yes | how the server enters the toolset namespace |
| `token_env` | no | environment variable holding a bearer token, read at startup |

`name` is what an agent lists in its `tools`, so a server no agent can name reaches no agent. Omit
`token_env` for an unauthenticated server, or one using the OAuth flow, which triggers automatically on
an auth challenge. The token stays in the environment, never in this file.

```toml
[[mcp.server]]
url = "https://api.githubcopilot.com/mcp/"
name = "github"
token_env = "GITHUB_MCP_TOKEN"

[[mcp.server]]
url = "https://broker.example.com/mcp"
name = "stocks"
```

Servers added at runtime with the `add_mcp_server` tool are appended here automatically, with a `name`
derived from the host and a numeric suffix if that name is taken, so the file always loads. That name
reaches no agent until you add it to a `tools` list **and restart**, since the toolset namespace and the
agent tables are both read only at startup. See [add an MCP
service](../how-to/add-mcp-services.md).

### The OAuth callback

Two scalar keys sit in `[mcp]` itself, alongside the server array. They decide where an OAuth provider
sends your browser once you approve, which is also where Kokua listens for the code.

| Key | Default | Meaning |
| --- | --- | --- |
| `oauth_callback_host` | `"localhost"` | host in the registered redirect URI, and the interface the callback server binds |
| `oauth_callback_port` | unset (a free port, chosen per process) | port for both |

```toml
[mcp]
oauth_callback_port = 8765
```

The defaults assume the browser runs on the same machine as Kokua. If it does not, the provider sends
your approval to *your* loopback, Kokua never receives it, and the connection fails only when the flow
times out. Pin the port and forward it (`ssh -L 8765:localhost:8765 kokua-host`), or set the host to
Kokua's own name if your provider accepts a non-loopback redirect URI. Pinning the port also protects a
re-authorization in a later session, since the client registration is cached across restarts while a
random port is not. [Add an MCP service](../how-to/add-mcp-services.md#authorizing-when-kokua-runs-on-another-machine)
covers both setups.

## `[paths]`

### `data_dir`

Absolute override for where all transient and user content lives. Default: `$KOKUA_HOME/data`.
Locked by default.

```toml
data_dir = "/path/to/kokua-data"
```

Every leaf derives from this one setting, so overriding it moves all of them together:

```
$KOKUA_HOME/
  config.toml
  data/                  <- data_dir
    sessions.json        conversations
    memory/              the memory store
    documents/           the document store
    skills/              installed skills
    images/              uploaded and generated images, served at /images
    downloads/           generated binary artifacts, served at /download
    logs/kokua.log       the rotating diagnostic log
```

To move the whole tree including `config.toml`, set `$KOKUA_HOME` instead. That one is an environment
variable rather than a setting because the config file lives inside it, so it has to resolve before the
file can be read.

## `[frontend]`

### `name`

Which front end `kokua` runs: `"cli"`, `"web"`, or any installed plugin. Default `"cli"`. `kokua-web` is
a convenience for `--frontend web`. List what is installed with `kokua --list-frontends`.

## `[web]`

Bind address for the web front end. Ignored by other front ends.

| Key | Type | Default |
| --- | --- | --- |
| `host` | string | `"127.0.0.1"` |
| `port` | integer | `8000` |

The default binds to loopback only. Kokua is a single-user assistant with no authentication layer of its
own, so putting it on a routable address exposes an unauthenticated assistant holding your machine's
tools. Front it with something that authenticates if you need remote access.

## `[logging]`

### `level`

Level for the rotating diagnostic log at `<data_dir>/logs/kokua.log` (5 files, 2 MB each). One of
`DEBUG`, `INFO`, `WARNING`, `ERROR`. Default `"INFO"`.

The log records the turn lifecycle (submitted, lock acquired, done, error), so a hung turn is visible
after the fact. It is also where a dropped generation parameter reports itself. For live state, the
`/diag` chat command reports turn and lock state and dumps a wedged turn's async stack, and
`kill -USR1 <pid>` dumps all thread stacks.

## `[email]`

Lets the assistant email information to you (digests, summaries, reports) over SMTP, through the
`email-report` skill. Install it with `kokua skills install email-report`, then name it in an agent's
`tools` alongside `fs` and `compute` so that agent can run its script.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `host` | string | unset | SMTP server |
| `port` | integer | `587` | 587 with STARTTLS, or 465 with `use_ssl = true` |
| `username` | string | falls back to `from`, then `to` | SMTP login user |
| `from` | string | falls back to `to` | `From:` header |
| `to` | string | unset | **the only address the assistant can send to**; locked by default |
| `use_ssl` | bool | `false` | `false` is STARTTLS on connect, `true` is implicit TLS (SMTP_SSL) |

Leave everything unset to disable email.

Three properties worth understanding before enabling it:

- **The recipient is locked.** Kokua passes these settings to the script's environment and the script has
  no recipient flag, so it can only ever mail `to`. That is also why `to` is locked by default: it is
  the whole guarantee.
- **Sending is ungated.** There is no per-send confirmation, because a scheduled or proactive turn has no
  one to ask and auto-denies gated tools. The recipient lock is what makes that safe.
- **The password is never in this file.** It is read from `$KOKUA_EMAIL_PASSWORD`, and a `password` key
  here is an unknown-key error rather than a working shortcut. For Gmail or Google Workspace, use an App
  Password. The script sends nothing, and says so, unless `host` and `to` are set *and* that variable is
  present.

## `[scheduling]`

This section exists because the `scheduling` toolset declares it. See [toolset
sections](#toolset-sections).

### `max_task_conversations`

How many conversations a scheduled task keeps. Default `3`. Hot.

Every firing runs in its own conversation. Once a task has more than this many, its oldest are deleted
after the next firing succeeds. `1` means each run replaces the one before it; `0` keeps every run
forever. A task can override this for itself through the `max_conversations` argument of `schedule_task`,
and this is what the tasks that do not override it follow.

## `[scheduling.task.<name>]`

Scheduled tasks, one table per task. The table name is the task's identity: it is what the sidebar shows,
what `cancel_scheduled_task` takes, and what is stamped on every conversation the task runs in.

**Changed through the scheduling tools or by hand, never by `update_config`.** See [who may change which
key](#who-may-change-which-key).

| Key | Type | Meaning |
| --- | --- | --- |
| `prompt` | string | what the assistant is asked when the task fires |
| `schedule` | inline table | when it fires (below) |
| `enabled` | bool | omit to leave it enabled; written as `false` when disabled or retired |
| `max_conversations` | integer | omit to follow `[scheduling].max_task_conversations` |

`schedule` is one of:

```toml
schedule = { type = "once",     at = "2026-08-20T09:00:00" }   # ISO-8601 local datetime
schedule = { type = "interval", seconds = 3600 }
schedule = { type = "daily",    at = "09:00" }
schedule = { type = "weekly",   day = "fri", at = "16:00" }    # mon/tue/wed/thu/fri/sat/sun
```

A fired one-shot is **not** deleted. It is left in the file with `enabled = false` and a `fired_at`
stamp, so the run stays on the record and re-running it is a one-character edit.

A task's turn is proactive, which means gated tools auto-deny inside it. Give a task work its agent can
finish unattended.

```toml
[scheduling.task.morning-brief]
prompt = "Summarize my calendar and any unread mail, and flag anything that needs a reply today."
schedule = { type = "daily", at = "09:00" }
enabled = true
max_conversations = 3
```

## `[planning]`

Deep planning is a toolset: an agent's `tools` must list `planning` for the `/plan <task>` command to
exist at all. It drafts an explicit plan, naming which tools, skills, and MCP services to use or build,
before doing the work. Every key here belongs to that toolset and is read only when an agent declares it.

| Key | Type | Default | Hot | Meaning |
| --- | --- | --- | --- | --- |
| `plan_review` | bool | `false` | yes | pause the planned turn for your Approve / Edit / Reject; off runs the plan autonomously |
| `plan_review_agent` | bool | `false` | yes | an independent, context-free agent critiques the plan, and Kokua re-plans on rejection |
| `result_review` | bool | `false` | yes | an independent agent checks the final answer before it is shown, and Kokua revises on rejection |
| `review_rounds` | integer | `2` | no | bounds each replan or revise loop |
| `show_reasoning` | bool | `false` | yes | stream every LLM call in a planned turn under labeled phase headers |

`result_review` runs the answer non-streamed, since it has to exist in full before it can be checked.
`show_reasoning` shows the planner, each reviewer's prose reasoning and verdict, the executor, and every
revision, including each intermediate version, which overrides `result_review`'s hide-until-vetted gate.

## `[capabilities]`

Capability discovery is a toolset: an agent's `tools` must list `capabilities` for the agent to see what
is installed beyond its own tools. It gets two tools. `list_capabilities` reads the registry, every
installed capability except this one, which the agent reading it already holds. `compose_worker` builds a
sub-agent holding exactly the capabilities one task needs, and runs it.

Unlike the workers in `[agents.*]`, a composed worker is not declared anywhere: its capabilities are
chosen per task from everything installed, except two names it can never be given. `skills` works only
on the agent Kokua constructs directly, and `capabilities` itself would hand the worker a fresh
composition budget. It runs on `[assistant].model` with the `[assistant]` thinking
and generation defaults, and its tools still go through `[security].confirm_tools`, so `execute_python`
and `add_mcp_server` still ask you first. Composing itself is not in the shipped `confirm_tools`, so it
does not ask; add `compose_worker` there to gate that too.

### `max_depth`

How far composition may nest. Default `3`. Hot.

At `3` a chain reaches three workers, and the last of them holds neither `compose_worker` nor
`list_capabilities`, since a `compose_worker` with no way to look up capability names is useless to
whatever holds it. `0` switches composing off entirely. The cap is read when `compose_worker` is called,
so an `update_config` change applies to the next composition with no restart, though a chain already
running keeps the count it started with.

## `[github_backup]`

Declared by the `github_backup` toolset: an agent's `tools` must list `github_backup` for
`backup_kokua_state` to exist. The tool takes no arguments; everything it needs comes from here.

| Key | Type | Default | Hot | Meaning |
| --- | --- | --- | --- | --- |
| `repo` | string | `""` | no | `owner/name` of the backup repository. Required: with it blank the toolset offers no tool at all, the same gate the `image` toolset applies to its model env var |
| `branch` | string | `"main"` | no | the branch backups are pushed to |

Both keys need a restart to apply. Neither is hot, so an `update_config` write reaches the file without
reaching the `AssistantConfig` the running process holds; `repo` is additionally read only once, in
`build`, when the toolset's tools are assembled for an agent. (`branch` is read per call, but off that
same unchanged in-memory copy, which is why it needs the restart too.)

Changing `repo` after a backup has run takes one more step: delete the `data/backup` working tree.
Its `origin` is whatever the first run recorded, and Kokua refuses to push to that while checking a
different repository's privacy, so a repointed key fails with a message naming both until the tree is
gone. The next backup then clones the new repository from scratch, carrying none of the old
repository's history with it.

The push token is **not** a config key. It is read from the `GITHUB_BACKUP_TOKEN` environment variable,
fixed rather than named in `config.toml`, so that repointing `repo` (which `update_config` can do unless
you lock the key yourself with `"github_backup.repo"` in [`locked_config_keys`](#locked_config_keys), as
no toolset section is locked by default) can never widen the capability past whatever repository that one
token already writes. Scope the token, a fine-grained GitHub PAT with `contents: write`, to the backup
repository alone.

The repository must be private. Kokua checks before it ever pushes and refuses to back up into a public
one. A backup copies `config.toml`, the memory store, saved documents, authored skills, and the
conversation transcripts, as a single git commit.

## Toolset sections

`[scheduling]`, `[planning]`, and `[capabilities]` are not special cases in the config layer. A section
named after a toolset is how *any* toolset ships its settings, including a third party's:

- The section name is always the toolset's own name, so two toolsets cannot claim one section. A toolset
  named after a section Kokua's core already parses is refused at startup.
- Each key is one `Setting` declared on the toolset, carrying its type, its default, and whether it is
  hot. Being flagged hot *is* what makes it hot; there is no second list.
- Keys are read only when some agent declares the toolset, but they are validated whichever way, so a
  typo in a section for a toolset no agent holds still fails startup rather than waiting to surprise you.
- A section whose toolset is installed but which **no agent declares** produces a startup warning: its
  settings are read by nobody, which is otherwise silent until you notice a flag doing nothing. A
  section no installed toolset owns at all is the ordinary unknown-key error.

Writing one is covered in [set up a toolset](../how-to/set-up-toolsets.md).

## Environment variables

Kokua reads these directly; none of them has a `config.toml` key.

| Variable | Meaning |
| --- | --- |
| `KOKUA_HOME` | root for all state. Default `~/.kokua`. Holds `config.toml` and `data/`, so it must resolve before the file is read, which is why it is not a setting |
| `KOKUA_CONFIG` | path to the config file, below `--config` and above the default location |
| `KOKUA_EMAIL_PASSWORD` | SMTP password for [`[email]`](#email). Never read from the file |
| `GITHUB_BACKUP_TOKEN` | push token for [`[github_backup]`](#github_backup). Fixed here rather than a config key, so a repointed `repo` can't widen the capability past what the token already writes |
| `AIMU_LANGUAGE_MODEL` | the model to use when `[assistant].model` is unset, before the running-local-server search |
| `AIMU_IMAGE_MODEL` | required by the `image` toolset, which offers no tool at all without it. For example `gemini:nano-banana`, or a HuggingFace diffusers `hf:<repo>` |
| `AIMU_AUDIO_MODEL`, `AIMU_SPEECH_MODEL`, `AIMU_TRANSCRIPTION_MODEL` | required by the `audio`, `speech`, and `transcription` built-in groups respectively |

The generative model variables have no default on purpose: the tool raises if the variable is unset,
rather than downloading weights you did not ask for. Accepted values and formats are in AIMU's [env var
reference](https://saxman.info/aimu/reference/env-vars/).

Reading images needs no variable, only a vision-capable model. The assistant reads what you attach
through the web UI (upload or paste) or the CLI's `/attach <path>`. Uploaded and generated images are
stored under `data/images` and served by the web UI at `/images`; a conversation keeps only a small
reference, not inline data.

## Command-line flags

Flags override the file for one run and are never written back.

| Flag | Overrides |
| --- | --- |
| `--config PATH` | which file is read |
| `--frontend NAME` | `[frontend].name` |
| `--model STRING` | `[assistant].model` |
| `--system TEXT` | the entry agent's opener, leaving a worker's alone |
| `--show-thinking` / `--no-show-thinking` | `[display].show_thinking` |
| `--show-tools` / `--no-show-tools` | `[display].show_tools` |
| `--confirm-tools NAMES` | `[security].confirm_tools`, comma-separated; empty string disables |
| `--mcp URL` | adds a server for this run, unauthenticated or OAuth |
| `--plugins` / `--no-plugins` | `[assistant].load_plugins` |
| `--host`, `--port` | `[web].host`, `[web].port` |

Introspection, which exits without running the assistant:

```bash
kokua --list-frontends    # installed front ends
kokua --list-toolsets     # every name a tools list may use, grouped by provider
```

## See also

- [`config.example.toml`](../../src/kokua/config.example.toml): the same keys, one line each, and what
  `kokua config init` writes.
- [Set up a toolset](../how-to/set-up-toolsets.md): declaring `tools` and `delegates_to`, and writing a
  toolset of your own.
- [Add a skill](../how-to/add-skills.md), [add an MCP service](../how-to/add-mcp-services.md).
- [Design principles](../explanation/design-principles.md): why the config layer is shaped this way,
  particularly "`config.toml` is the single source of settings" and "a capability is declared, never
  defaulted".
- [Architecture](../explanation/architecture.md): how the file is layered, parsed, and written.
