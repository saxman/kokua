# Capability is declared

**An agent holds exactly the capability someone wrote down, and nothing a code path decided to grant
it.**

## The idea

Software's usual default is that installing something makes it available. Import the library and its
functions are callable. Drop the plugin in the directory and it loads. That default is safe because a
human writes every call site: the capability is present, but nothing reaches for it until somebody
types its name.

An agent breaks that arrangement, because the call site is chosen at runtime by a model reading a list
of what it holds. "Available" and "used" collapse into one thing. Everything installed becomes
something the system may decide to do, the set changes whenever you install an unrelated package, and
nobody wrote down what the set is.

There is a second cost, less alarming and more constant. Every tool an agent holds is described to the
model on every round of every turn ([Tool calling](tool-calling.md)), so an unused capability is paid
for on each call out of the same [context window](context-and-memory.md#the-idea) the conversation is
competing for, and it makes the model's choice harder rather than easier. Reach and cost push the same
way.

The alternative is to declare. An agent's capability is a list a person wrote, and installing something
puts it within reach of that list rather than into the agent's hands. Adding a capability becomes two
steps instead of one: install it, then name it. What you get back is that the question "what can this
thing do?" has an answer you read in one place, instead of one you derive from a dependency tree.

Two properties decide whether declaring is worth the extra step, and both are about failure rather than
philosophy. A declared name that resolves to nothing has to be a loud error and never a silently
shorter list, because a typo that quietly removes a capability is indistinguishable from a model that
chose not to use one. And the declaration has to be *per agent*, since the reason to run more than one
agent is that they should reach different things.

The step you pay for is the one people forget, and forgetting it does not announce itself. A tool that
was never declared produces no error, no warning, and no log line; it produces an assistant that says
it cannot do the thing you installed last week. That failure looks like a bad model rather than a
missing line of configuration, which is why a system that declares capability needs a command that
prints what is installed, separately from what is held.

No real system's rule is absolute. The honest
move is to name the exceptions out loud rather than to state the rule and quietly break it, and a
defensible exception is one bounded in time, or one entered through the rule itself.

## Watch it

Two prompts at the terminal: what can you do, and then something it cannot. Quoted exactly as it was
printed ([`qwen3.5:9b` on Ollama](get-it-running.md), Kokua commit `34831a1`).

```text
[notice] This assistant can author and run Python/shell scripts with full access to this machine (no sandbox), and can connect to remote MCP servers and run whatever tools they expose. Only use it with a model, inputs, and MCP servers you trust.
> List your capabilities.
> 
[thinking] The user is asking me to list my capabilities. I should use the `list_capabilities` function to show them what tools and capabilities are available on this machine.
[tool] list_capabilities()

[thinking] I should summarize these capabilities for the user in a clear, organized way.
Here are my capabilities:

**Core Tools:**
- **memory** - Store facts about you and remember them across conversations
- **conversations** - Read saved chat transcripts from other sessions
- **documents** - Search and manage files stored in `/notes/` or similar directories
- **benchmarks** - Measure model speed (time to first token, tokens/sec)

**File & System:**
- **fs** - Read/list files and directories on your machine
- **save_document** / **read_document** - Save and read text documents
- **config** - Read and update `config.toml` settings

**Automation:**
- **scheduling** - Create, manage, and run scheduled tasks
- **skills** - Author reusable skills and attach executable scripts to them
- **mcp** - Connect/disconnect remote MCP servers dynamically

**Generation & Processing:**
- **compute** - Run Python code, shell commands, and calculations
- **web** - Search the web and retrieve pages
- **audio** / **speech** / **image** / **transcription** - Generate audio, speak text, create images, transcribe speech (requires model config)

**Planning & Coordination:**
- **planning** - Deep planning before acting with optional review
- **subagents** - Spawn specialized agents (coder, researcher) via `spawn_subagent` and `compose_subagent`
- **aimu_agents** - Prebuilt orchestrator agents for code review, research reports, content creation

Let me know if you'd like details on any specific capability!
```

The first thing to notice is that the model did not answer this from its prompt. It called a tool,
`list_capabilities()`, and read the answer off the registry, because the list is a property of the
running installation rather than of the model.

The second thing is that the list is wrong about the assistant, and instructively wrong. That tool's
own docstring says it lists everything installed on this machine **whether or not you currently hold
it**, and the model presented the result as "Here are my capabilities". Of the entries above, `fs`,
`compute`, `web`, `audio`, `speech`, `image`, `transcription`, and `aimu_agents` are not in this
assistant's declared list at all. It cannot call any of their tools directly. It can reach some of them
another way, by composing a worker for one task, which is the second exception named below and is a
real route, not a technicality. But the catalogue is not the holdings, and nothing in a catalogue makes
a capability yours.

Then the request it cannot satisfy:

```text
Send an email to nobody@example.com saying hi.
> 
[thinking] The user is asking me to send an email to somebody@example.com (actually they said "nobody@example.com"). However, looking at my available capabilities, I don't see any direct email sending capability listed. 

Let me think about this:
1. My capabilities include config management (read/write config.toml) which mentions [assistant].generate_titles but doesn't mention email capabilities
2. There's no mail tool or SMTP sender in my built-in tools
3. The user might be expecting me to use an email-related capability

Wait, looking more carefully at the capabilities list:
- **config** - Read config.toml and change runtime settings, persisted back to file
- The description mentions "email" as a setting that can be updated with update_config

But actually, I don't have a direct "send_email" tool available. The user might be assuming I can send emails, but that capability isn't in my toolset.

I should let the user know that I don't have email sending capabilities available. However, before saying no, let me check if there's an MCP server or other tool that could handle this... No, the list_capabilities response doesn't show any mail-related tools.

I should be honest and tell the user I can't send emails with my current capabilities.
I don't have email sending capabilities available in my current toolset. My available capabilities include file management, document storage, web search, scheduling tasks, running code, and more—but not direct email sending.

If you'd like to send an email, you could:
1. Use your regular email client
2. Set up an MCP server for mail (if your system supports one)
3. Have me draft the email content in a file for you to use

Would you like me to save a draft of that "hi" email message instead?
```

The model checks (`There's no mail tool or SMTP sender in my built-in tools`), gets briefly confused by
the `[email]` section of the configuration file it can read, and lands in the right place: it holds no
tool that sends mail, so it says so and offers something it can do instead.

Two honest points about that. This is the model behaving well, and the mechanism does not depend on it
behaving well: had it emitted `send_email(...)` anyway, that request would have matched no tool in the
set this agent holds, and dispatch answers an unknown name with an error message rather than a call.
Refusal by declaration happens whether or not the model cooperates.

And the capability genuinely exists. Kokua can send you email through the `email-report` skill, over
the `[email]` settings the model half-remembered. In this run the skill was not installed and no
agent's table named it, so it was not in the registry, not in the catalogue, and not in reach of any
composition. Installed and declared are two different states, and this transcript is what the gap
between them sounds like.

## In Kokua

The rule is written down, in exactly these words:
[a capability is declared, never
defaulted](../explanation/design-principles.md#corollary-a-capability-is-declared-never-defaulted). An
agent's capability is what its `[agents.<name>]` table declares: the `tools` list, plus the
`spawn_subagent` that a non-empty `delegates_to` earns. No code path adds a capability no key in that
table asked for, and no flag can disagree with a declaration.

The table is one block in
[`config.example.toml`](https://github.com/saxman/kokua/blob/main/src/kokua/config.example.toml), and
the transcript above is that block's behavior:

```toml
[agents.assistant]
...
description = "The assistant the user talks to."
system_message = "You are a personal assistant running on the user's own machine. Be concise and helpful."
tools = ["memory", "documents", "skills", "config", "mcp", "scheduling", "conversations", "planning", "capabilities", "time", "benchmark"]
delegates_to = ["researcher", "coder", "introspector"]
```

Eleven names, no `fs`, no `compute`, no `web`. Every name is resolved against a registry built at
startup, and an unknown one stops the process rather than shrinking the list:
[`registry/registry.py`](https://github.com/saxman/kokua/blob/main/src/kokua/registry/registry.py)'s
`select` raises, and
[`core/agents.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/agents.py)'s
`validate_agents` runs it over every agent before anything is built.

What can be named comes from one table in `pyproject.toml`, and every toolset Kokua ships registers
through it the same way a third party's does:

```toml
[project.entry-points."kokua.toolsets"]
aimu_agents = "kokua.toolsets.aimu_agents:TOOLSET"
audio = "kokua.toolsets.audio:TOOLSET"
benchmark = "kokua.toolsets.benchmark:TOOLSET"
capabilities = "kokua.toolsets.capabilities:TOOLSET"
...
web = "kokua.toolsets.web:TOOLSET"
```

`build_registry`'s own docstring draws the line the transcript walked into: this "decides what a name
can resolve to, not what any agent holds."

A hand-maintained index rots, so it is pinned in both directions by
[`tests/toolsets/test_registration.py`](https://github.com/saxman/kokua/blob/main/tests/toolsets/test_registration.py),
which is the guard worth stealing:

```python
def test_every_toolset_module_is_registered():
    """A module nobody registered is dead code: no name resolves to it, so no agent can declare it, and
    nothing anywhere says so."""
    unregistered = sorted(_module_stems() - set(_declared_entry_points()))

    assert not unregistered, f"toolset modules with no kokua.toolsets entry point: {unregistered}"
```

Its sibling asserts the other direction, and a third asserts that the entry-point key, the module name,
and `TOOLSET.name` all agree, since `register` keys on the last of those while the key feeds only a
provenance label. A mismatch would otherwise register a real toolset under a name nobody wrote.

**Three documented exceptions, named rather than glossed.**

*`delegates_to` grants a tool the `tools` list does not name.* A non-empty `delegates_to` earns the
agent `spawn_subagent(agent_type, task)` over exactly the agents it names, built by
`make_delegation_tool` in
[`core/agents.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/agents.py). This is the
mildest of the three, because it is still a declaration and only a different key: the assistant in the
block above holds `spawn_subagent` because of its last line, not because of the line before it. It is
listed here anyway, since a reader counting tools from the `tools` list alone will come up one short,
and [Delegation](delegation.md) shows the tool being used by an agent whose `tools` never mentions it.

*A composed sub-agent draws from the whole registry.* `compose_subagent`
([`toolsets/capabilities.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/capabilities.py))
builds a worker from names the model picked out of the catalogue, with no table involved. It is an
exception at one level and not at the next: only an agent whose own `tools` names `capabilities` holds
that tool, so the exception is entered by declaration. The worker is built per call and discarded with
the call, it may not be handed `capabilities` itself, how far composition nests is
`[capabilities].max_depth` rather than the model's decision, and its calls still route through
`[security].confirm_tools`. What the rule protects is a *persistent* agent's reach, and a worker
composed for one task is not an agent the config describes. [Delegation](delegation.md) is that
mechanism in full.

*The entry agent gets its skills regardless.* The agent you talk to is an AIMU `SkillAgent`, so AIMU
hands it the skill catalogue, `activate_skill`, and a tool per skill script whether or not the table
names `skills`; declaring `skills` only adds `author_skill` and `add_skill_script`. A spawned worker is
a plain `aio.Agent` and gets none of it, which is also why `skills` is the one `entry_point_only`
toolset, pinned as such by that same test file.

## What it costs

**The forgotten declaration.** You `uv add` a toolset, `kokua --list-toolsets` shows it, you ask
the assistant to use it, and it says it cannot. Nothing errored. This silence is deliberate: Kokua used
to warn at startup about a provisioned toolset no agent named, and the warning was removed, because
telling a toolset somebody installed on purpose from one that merely ships would take a provenance rule
over the whole namespace, and a line printed on every start is a line people learn to skip. So the
diagnosis is a command rather than a warning. `kokua --list-toolsets` prints every name an
`[agents.<name>].tools` list may use, grouped by provider, after reading your config, so it includes
your MCP servers and your skills. What it does not tell you is which agent holds what: for that you
read the tables.

**Declaration is per agent, and workers are the easy thing to get wrong.** The shipped `researcher`
declares `["web", "misc", "time"]`. It cannot read a file, and delegating a file-reading task to it
returns an apology rather than an error. Widening the entry agent widens nothing for its workers.

**What is loud, so you know what to expect.** An unknown name in a `tools` list fails startup. A cycle
in `delegates_to` fails startup. A `[security].confirm_tools` entry naming no real tool fails startup,
with the near misses suggested, because a gate that matches nothing prompts for nothing and nobody
notices a prompt that never comes. The quiet failure is confined to exactly one case: installed, and
never named.

**The tax you keep paying.** The declared list is not free either. The shipped entry agent's eleven
toolsets resolve to thirty tools, plus the `spawn_subagent` its `delegates_to` earns, and all 31
descriptions are re-sent on every round of every turn. Declaring a capability you never use costs context on each call, which is the argument
[Context and memory](context-and-memory.md) picks up.

## Go deeper

- [Tool calling](tool-calling.md): why every declared tool is re-sent every round, and what a schema
  is.
- [Delegation](delegation.md): `delegates_to`, `compose_subagent`, and why a composed worker is not an
  agent the config describes.
- [Set up a toolset](../how-to/set-up-toolsets.md) and [Install a third-party
  toolset](../how-to/install-a-third-party-toolset.md): the two-step, end to end.
- [Design principles: a capability is declared, never
  defaulted](../explanation/design-principles.md#corollary-a-capability-is-declared-never-defaulted),
  and [Architecture: agents and delegation](../explanation/architecture.md#agents-and-delegation) for
  how a declaration becomes a built agent.
- [The shipped entry agent's
  inventory](../explanation/architecture.md#the-shipped-entry-agents-inventory): all 33 tools and where
  each comes from.
- [Configuration reference](../reference/configuration.md): every key in the tables above.
