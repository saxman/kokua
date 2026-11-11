# Tool access

An agent is a model plus the tools it may call, and "which tools" turns out to be three separate
questions that most early agent code answers with one list. Kokua names them apart, gives each its own
key in `config.toml`, and this page explains why they are separate, what each one does, and how the
same three questions show up in other frameworks under other names.

| Axis | The question | Key |
| --- | --- | --- |
| **Availability** | What can this agent ever call? | `available` |
| **Residency** | What is in its context from the start? | `resident` |
| **Approval** | What asks before it runs, and who answers? | `confirm`, `review` |

Every key lives in one subtable per agent, `[agents.<name>.tools]`, with `[assistant.tools]` as the
default for the approval half. The whole reference is
[`[agents.<name>.tools]`](../reference/configuration.md#agentsnametools); this page is the argument.

```toml
[assistant.tools]
confirm = ["skills.add_skill_script", "mcp.add_mcp_server", "config.update_config"]  # asks you
review = ["compute.run_command", "compute.execute_python", "fs_write"]              # asks a reviewer, then you

[agents.researcher.tools]
available = ["web", "misc", "time", "messaging"]   # inherits [assistant.tools]'s confirm and review

[agents.assistant.tools]
available = ["memory", "documents", "skills", "config", "mcp", "scheduling", "conversations", "planning",
             "capabilities", "time", "benchmark", "messaging"]
resident = ["memory", "config", "conversations", "planning", "capabilities", "time", "messaging"]  # the rest load on demand
```

## Availability: fixed by the host, never by the model

`available` is the list of toolsets an agent can call, and nothing else: no code path adds a tool to an
agent that did not name it, with a few exceptions that are themselves entered by declaration: a
non-empty `delegates_to` earns `spawn_subagent`, the entry agent's `SkillAgent` carries the skill
catalogue and script tools whether or not it declares `skills`, and `compose_subagent` draws from the
whole registry for an agent that declares `capabilities`. That is the corollary to
[principle 2](design-principles.md#corollary-a-capability-is-declared-never-defaulted), which states
the exceptions in full, in the terms of this page, and it is the same everywhere the field has looked.

- Anthropic's Messages API fixes the capability in the request's `tools` array. Its tool search can
  hide a tool's definition until needed (`defer_loading`), but only for tools already in that array;
  the model finds a tool, it does not acquire one.
- The OpenAI Agents SDK's `tool_filter` narrows what a configured MCP server may offer an agent.
- Claude Code gives a subagent a `tools` list in its definition, and its permission rules are written
  by the user in settings, not by the model.
- Hermes children inherit their parent's toolsets, and the model cannot name others.
- OpenClaw enforces per-agent `tools.allow` and `tools.deny`, revalidated on the host side.
- `langgraph-bigtool` retrieves from a registry the host built.

No surveyed framework lets the model grant itself a capability the host did not, and Kokua's list is
the same idea with a name. (Those descriptions are as each project documented itself in October 2026;
treat them as pointers, not as their current state.)

**Why it fails closed.** An agent holds exactly the toolsets named, so a toolset added later reaches
no agent with a list until a person writes its name there. The alternative, "everything is available
unless excluded", fails open on growth: a new plugin, a new Kokua toolset, or a skill the assistant wrote
would reach every agent, the researcher included, with nothing to notice the omission. (One agent can
choose that growth on purpose, by declaring [`available = "all"`](#available-all-every-installed-toolset-still-declared);
the point is that it is chosen per agent, in the agent's own table, and never the default.) The worked example is
AIMU 0.31.0, when the `fs` group gained `write_file` and `edit_file`. Had availability been "whatever
`fs` holds", an upgrade alone would have given every agent declaring `fs` a capability nothing
declared. Kokua instead split the group by reach into two toolsets, `fs` (reads) and `fs_write`
(writes), so "read a file and do not write one" is something an agent's list can say. The shipped
`introspector` holds `fs` for exactly that reason.

One name inside the approval lists, `core`, is reserved for the tools no toolset provides:
`spawn_subagent` and `activate_skill`, plus `available_tools` and `load_tools` once any agent defers a
toolset (see [Residency](#residency-what-is-in-context-and-what-loads-on-demand)). A bare `core` covers
all of them and `core.<tool>` names one. A delegating agent earns `spawn_subagent` by naming workers in
`delegates_to` rather than by `available`.

## Residency: what is in context, and what loads on demand

Residency asks a different question from availability: of what an agent *could* call, how much sits in
its context window on every request? Every other surveyed framework treats this as the dynamic axis.
Anthropic's tool search, Claude Code's deferred tools, Hermes' tool search, and `langgraph-bigtool`'s
embedding retrieval are one pattern: a cheap index stays resident, and a tool's full schema is loaded
when it is wanted. Agent Skills' progressive disclosure is the same idea applied to instructions.

Kokua's version is one key and AIMU's tool groups. **`resident` names the toolsets an agent holds from
its first turn,** and it is a subset of `available`. Whatever `available` names and `resident` does not
is *deferred*: the agent can reach it, but none of its tool schemas are in the request until the model
asks for them. Leave `resident` out and every available toolset is resident, which is how the shipped
workers are declared. A `resident` entry that `available` does not hold is a startup error, because a
resident toolset is one the agent already holds. The order you write `resident` in changes nothing:
tools are presented in `available` order either way (for `available = "all"`, in trust order). `kokua agents` prints each agent's resident list,
or "every available toolset" when it declares none.

Note what residency cannot do: widen anything. A deferred toolset is one the agent's own `available`
list already names, so loading it is the model choosing *when* to pay for a capability the host granted,
never acquiring one. That is the same line Anthropic's `defer_loading` draws, for the same reason.

### What the model sees

An agent with at least one deferred toolset carries two more tools, which come from AIMU rather than
from any toolset: `available_tools`, which lists every toolset the agent holds or can load, and
`load_tools(name)`, which loads one. Its system prompt gains a single line naming what it can load, and
only the names:

```text
Also available, load before use: documents, skills, mcp, scheduling, benchmark.
```

This is what `available_tools` returns for the shipped entry agent, abridged:

```text
memory [resident]: Facts about the user, remembered across conversations.
config [resident]: Read config.toml and change a runtime setting, persisted back to the file. Asks first: update_config.
...
skills [loadable]: Author skills, revise them, and attach runnable scripts to them. Asks first: add_skill_script.
mcp [loadable]: Connect and disconnect remote MCP servers at runtime. Asks first: add_mcp_server.
scheduling [loadable]: Schedule, list, edit, and cancel recurring or one-off proactive tasks.
```

The description is the toolset's own (for an MCP server, its
[`description`](../reference/configuration.md#mcpserver) key), and the `Asks first` note comes from the
agent's approval policy, so a model choosing what to load knows a tool will stop for an answer before it
plans around that tool. A deferred toolset's `guidance`, the instructions it would otherwise add to the
prompt, arrives with its tools as part of `load_tools`' result, so the prompt pays for none of it until
it is used. A load lasts for the rest of the agent's life, and nothing unloads. Loaded tools go after
everything the agent already holds, so within a conversation a request's tool list only grows, which
keeps a provider's prompt cache valid. A loaded tool whose name the agent already holds is skipped, and
the result says so.

### What a load costs: the build, paid when it happens

A resident toolset is built when the agent is wired. A deferred one is built when it is loaded, through
the same `build_tools` call, in a worker thread so the event loop keeps serving other conversations
meanwhile. So whatever building a toolset does (opening the document store, for `documents`) is paid
only by an agent that loads it.

A build that fails, fails at load. The model reads AIMU's `Could not load '<name>': <error>` as the
tool's result, you get an alert naming the toolset and the agent, and the toolset stays loadable, so a
retry after the cause is fixed works. A deferred MCP server that is not connected refuses to load,
rather than loading empty. Resident, the same server builds empty and a reconnect reaches the next
rebuild; but a load is recorded as done, so an empty one would leave the server unloadable for the rest
of the conversation even after it came back.

### What a load checks: approval, before any tool is callable

An approval entry naming a deferred toolset that is not built yet cannot be resolved at startup,
because a tool name exists only once its toolset has built it. A toolset some agent holds resident is
built at startup, and so is every skill the entry agent's `SkillAgent` holds (every skill when it declares `skills`, otherwise the skills it names); an entry on
either resolves there like any other. So startup checks the entry's shape and
holds it as *pending*, and the build resolves it: `build_tools` reports each toolset it builds to a
hook, and the hook resolves the waiting entries before a single tool is returned. That holds whoever builds the toolset: the model's
`load_tools`, a [composed worker](architecture.md#agents-and-delegation), or a rebuilt agent restoring
what its conversation loaded (below).

The hook matches every entry naming the built toolset, not only the waiting ones, and only ever adds
names. That matters because a toolset's tools can grow after startup: a skill gains a script
mid-session, and `review = ["weekly-digest"]` means every script the skill has now, not the ones it
had when Kokua started. Each script write reports every skill's tools to the same hook before any
worker can be handed the new script (see [what arrives mid-session](#what-arrives-mid-session)).

An entry that still matches nothing once its toolset is built, such as `mcp.add_mcp_servr`, refuses that
build and every later one, so the toolset cannot be loaded until the entry is fixed and Kokua
restarted (approval lists are startup-only, and locked by default). That is failing
closed on purpose: the alternative is a tool running ungated because its gate was misspelled. On a load,
the model reads the refusal as a `Could not load` result and you get the alert; a `compose_subagent`
whose build is refused the same way answers `Could not compose`, naming the entry, with the same alert.
With auto-approval enabled, `[security].never_auto_approve` entries naming a deferred toolset are
pending in the same way, and a build after which some policy's `review` list reaches that floor is
refused too. In the shipped config this is how
`skills.add_skill_script` and `mcp.add_mcp_server` are gated: both name toolsets the entry agent
defers, and both resolve the first time their toolset is built.

A conflict the entries' own text already settles does not wait for that build. `review = ["mcp"]`
(or `"mcp.*"`) against the floor's `mcp.add_mcp_server`, or `mcp.add_mcp_server` in a pair's `confirm`
with `mcp` in its `review`, fails startup with the same error a build would give: if the tool exists,
both lists hold it; if it does not, the tool entry is dead and refuses the build anyway. Waiting would
only turn one startup error into a refusal on every load of the toolset. Two different tools of one
toolset (`mcp.add_mcp_server` against `mcp.remove_mcp_server`) say nothing about each other, so they
wait, and the build-time check stays as the backstop for an overlap no text reveals, such as a review
list that already holds the same tool name through another toolset.

### When an agent is rebuilt, it preloads

A conversation's agent is rebuilt from the stored transcript more often than you might expect: on
restart, when switching among more than eight conversations evicts the least recently used one, and when
a conversation is truncated. The new agent preloads the toolsets the transcript's `load_tools` calls
loaded, in the order they were first loaded, instead of replaying the calls. The reason is mechanical: the
agent builder is synchronous and AIMU's load is async. The end state is the same tools, with the
restored toolsets' guidance in the prompt and the names line listing only what is still loadable.

Only a load that succeeded comes back, judged by the result stored beside the call. The call alone is
not evidence, because it is stored whether or not it ran: if you gate `core.load_tools` and deny a load,
or a turn nobody is watching has it auto-denied, the transcript still holds the call, and restoring
from calls would hand you the toolset you refused at the next restart. A denied load has to stay
denied. A load that failed, or that a cancelled turn left without a result, is left out too, and stays
loadable.

The cost is that a preloaded toolset is held like a resident one: `available_tools` lists it as
`resident`, not `loaded`, and its tools sit with the resident ones, ahead of `spawn_subagent`, any
skill tools, and the two loaders. A preload goes through the same build and the same approval hook as a load. One that fails (a refused
approval entry, an MCP server still down) is logged at WARNING rather than raised as an alert, since a
rebuild is something nobody asked for, and the toolset stays deferred and loadable so the model can try
again and read the real error. A preloaded tool whose name the agent already holds is skipped
and logged. Truncation rebuilds from the shortened transcript, so a load made in the deleted turns is not
restored.

### Workers, and the shipped split

`resident` works the same on every agent. A delegated worker that declares one gets the same two tools
and the same names line, and its loads are its own: a worker is built per spawn, so what it loads ends
with it. A composed worker has no table and so no residency; everything it is composed from is built
when it is.

The shipped entry agent keeps `memory`, `config`, `conversations`, `planning`, `capabilities`, `time`,
and `messaging` resident, the toolsets it reaches for on most turns, and loads `documents`, `skills`,
`mcp`, `scheduling`, and `benchmark` on demand. Measured as the JSON of every tool spec the model is
sent, its tool block went from 30,560 bytes for all 37 tools to 16,244 bytes for nineteen (seventeen
resident, plus the two loaders): 46.8% smaller, on every round of every turn that loads nothing. The
approval gate covers the same seven tools it did. What it costs is a round: a turn that needs a deferred
toolset spends one model call on `load_tools` before it can use it. Each model call in the web UI still
shows its `N tools` count, which is residency as the model experienced it.

## `available = "all"`: every installed toolset, still declared

`available` takes one other value besides a list: the string `"all"`, meaning every toolset this install
registers. It needs a `resident` list, for a reason the section below gives.

```toml
[agents.researcher.tools]
available = "all"
resident = ["web", "misc", "time", "messaging"]   # the rest load on demand
```

The shipped config does not use it. The entry agent keeps its list, with a commented
`# available = "all"` line beside it, because an entry agent holding everything would undo the lean
supervisor it is designed as, and would pull untrusted web content into the context of the agent you
talk to.

### What it expands to, and in what order

`"all"` is expanded wherever an agent's availability is read (`core/agents.py`'s `available_for`), so
`kokua agents`, the startup summary, the prompt, the build, and the approval checks all see the same
list. It is every registered toolset, in **trust order**, most trusted first:

1. Kokua's own toolsets: everything Kokua's distribution registers, wrappers over AIMU's groups included,
   which is the `built-in toolset` group `kokua --list-toolsets` prints (`aimu_agents`, `benchmark`,
   `github_backup`, and `image` among them);
2. third-party plugin toolsets;
3. skills, one toolset per skill on disk;
4. MCP servers, one toolset per `[[mcp.server]]`.

Within each kind, toolsets keep the order they registered in. Three things are never in it:

- **An `entry_point_only` toolset, on any agent but the entry agent.** That is `skills` (the authoring
  tools), which a spawned worker cannot host. `"all"` leaves it out on a worker rather than listing it
  and then refusing the config for a name nobody wrote. Individual skills stay in, since a worker holds a
  skill through the registry rather than through `SkillAgent`.
- **The `core` tools.** They belong to no toolset, so a set of toolsets cannot name them.
  `spawn_subagent` is still earned only by a non-empty `delegates_to`.
- **A toolset named `all`.** The name is reserved, refused at registration as `core` is, because no
  agent could declare it on its own.

`kokua agents` prints the expansion with its count, `available: all (22): aimu_agents, audio, ...`, and
the startup summary line reads `researcher: all (22) available, 4 resident, ...`. The value is exactly
the lowercase string: `["all"]`, `"All"`, or `"all"` in any casing inside a list fails startup with a message
saying how to write it. `update_config` can write it too, once you unlock `[agents.*]`, and its dry run
applies the same checks the next startup would.

### Why it is still a declaration

`"all"` is one value a declaration can take, not a default. A person writes it, in the agent's own table,
which is locked against `update_config` by default, so the rule that the model cannot grant itself a
capability holds unchanged. What changes is the failure mode the [fails-closed
argument](#availability-fixed-by-the-host-never-by-the-model) is about: an `"all"` agent *does* grow
when you install a plugin or write a skill, and that is the thing you asked for. Every other agent's
list still grows only by an edit.

It does not widen what the other two axes control. Approval entries still gate whatever the agent can
reach, now that it can reach more, and residency still decides what costs context.

### Why it requires `resident`

Without a `resident` list, everything available is resident, and for `"all"` that is every tool schema
the install offers, re-sent on every round. That is the opposite of what an agent holding everything
needs, so the combination is a startup error. Every `resident` name has to be within the expansion,
which config parsing cannot check (it cannot see the registry that says what `"all"` holds), so
validation does, and says which of two reasons a name fails for: nothing installs it, or it is `skills`
on a worker, which `"all"` leaves out there.

### Which of two same-named tools wins

Toolsets never share a name (`register` refuses two providers claiming one), but tools can: two MCP
servers both offering `search`, or a remote server offering a `read_file` beside `fs`'s. Where toolsets
are built together, `build_tools` keeps the first tool of a name, and for `"all"` "first" is trust order.
Registration order would have put MCP servers first, letting a remote host's `read_file` shadow Kokua's.

Built together means the resident build and a delegated worker's resident tools. A deferred toolset is
settled by load order instead: AIMU keeps whichever tool the agent already holds, and the load result
names what it skipped and what holds it, so a later load never replaces a held tool. The holder is named
as a group only when it was itself loaded (`Not loaded, because the name is already taken: read_file
(by 'files').`); a resident holder reads `read_file (by a tool given to this agent directly)`.

A resident tool that loses is reported, never dropped in silence. After every startup build, Kokua checks
each agent's resident toolsets and raises one alert per hidden tool:

```text
researcher: read_file from 'files' is shadowed by 'fs' (the earlier resident toolset keeps the name).
```

The check runs on every declared agent, with a list or with `"all"`; on a list agent the order it
reports against is the list's own. The `activate_skill` every skill carries is not counted: it is one
shared tool, so a second skill offering it hides nothing. It reads what each toolset built, and startup builds only the entry
agent and every worker reachable from it by `delegates_to`. So an agent is skipped, and logged at INFO
rather than reported as clean, when any of its resident toolsets was built by no agent, which is the case
for an agent nothing delegates to unless every toolset it holds resident is one another agent built.

### What arrives mid-session

`"all"` means every toolset registered at startup, plus every skill written since. Skills arrive at the
next spawn; MCP servers arrive at the next restart:

| | Skill written (`author_skill`, `add_skill_script`, `update_skill`) | MCP server added, persisted (unauthenticated or OAuth) | MCP server added, bearer token |
| --- | --- | --- | --- |
| Entry agent | Immediately, through its `SkillAgent` | As a list or `"all"` agent, below | Never |
| Agent with a list | No new skill. A skill it already names reaches its next spawn as now written, new scripts included | Once named in `available`, after a restart | Never |
| Agent with `"all"` | Its next spawn | Next restart | Never |
| Composed worker | Its next compose | Next restart | Never |

The mechanism is one refresh. After each skill write, `LiveState.refresh_skills` rescans the skills
folder into the registry, keeping every other entry as startup registered it, then rebuilds every live
agent's `spawn_subagent`, the same path `add_mcp_server` already uses. A worker is built fresh per spawn
from its spec, and `compose_subagent` reads the registry on every call, so neither needs any running
agent's prompt touched. A conversation whose agent has not been built reads the new registry when it is.
(A revised description reaches the entry agent's own catalogue only in a fresh conversation, since its
prompt is not rewritten mid-run; a revised body applies the next time any agent activates the skill.)

The rule behind the two columns is whether the new capability opens a new trust path. **A skill does
not.** Its prose adds nothing the entry agent could not already write into a delegated task, and its
scripts are code a person read while approving `add_skill_script`: the code is that call's `content`
argument, the shipped config gates the tool, and `[security].never_auto_approve` keeps a reviewer from
answering for you. (A script put into the skills folder some other way, such as a `write_file` call,
arrives under that tool's gate instead, which for `fs_write` in the shipped config is `review`.) A gate
you wrote on the skill itself also covers the new script: the refresh reports each skill's tools as a
build before it rebuilds any delegate, so an entry naming the skill bare holds the script back for every
agent, the entry agent included, before any of them can call it. **A new MCP server does.** Its tools and their descriptions arrive from a remote host
after a person approved only its URL. Keeping servers at restart preserves `add_mcp_server`'s guarantee
that it cannot grant a capability in the session that added it, which is what blocks the attack where
injected text persuades you to approve a malicious server and the agent calls its tools the same turn.
A bearer-token server's token is never written to disk, so no restart brings it back, and nothing can
name it this session. The flip side is worth stating plainly: for an `"all"` agent, approving
`add_mcp_server` for a persisted server *is* the grant, effective at the next restart with no edit of
yours in between, so approve it as one.

Prior art agrees on both halves. Live skills are the norm: Claude Code hot-reloads skill directories and
has `/reload-skills`, OpenClaw refreshes when a `SKILL.md` changes, and Hermes treats a dropped-in skill
as live. And in every surveyed system a new MCP server becomes usable only through a human or host
action outside the model's loop (Claude Code's `/mcp`, Hermes' `/reload-mcp`, Managed Agents' host-side
`sessions.update()` while idle), never through the model's own tool call. No surveyed system documents
whether sub-agents see capabilities created at runtime, so that part is Kokua's own decision.

Refreshing at write time also moves a name collision to the call that causes it. `author_skill` refuses
a name already taken before writing anything: a toolset's, an MCP server's on file in `config.toml`
(including one added this session, which the registry has not seen yet), `core`, or `all`. Without
that, a skill named `web` would be written to disk and stop the next start. A collision the check cannot
see, such as a skill directory made by hand, comes back with the tool's result, so the model can act on
it.

### Exploring with `"all"`

An agent holding everything still pays context only for what it loads, and each load is a decision the
model makes. If you want to watch those decisions while you find out what an `"all"` agent reaches for,
put `core.load_tools` in the `confirm` list of the policy that agent uses: that is the control for "ask
me before you expand your context". Every load then stops for your answer and names the toolset, and the
`N tools` count on each model call in the web UI shows what the agent's context holds as the turn goes on.

Where you write it matters. In `[assistant.tools]`, every agent without its own pair asks before each
load. In the agent's own `[agents.<name>.tools]`, the entry declares that agent's whole policy, so the
pair must repeat every default entry you still want: a declared pair replaces `[assistant.tools]` whole,
and a `review` it leaves out is empty (see [the pair is inherited whole](#the-pair-is-inherited-whole)).
Writing only `confirm = ["core.load_tools"]` there would drop every other gate the default carries for
that agent, `run_command`, `execute_python`, and `fs_write` included.

`compose_subagent` changes meaning on an `"all"` agent too. Its guidance gives two reasons to compose: a
capability you do not hold, or tool output that would flood your context. An agent holding everything
has only the second, which is the clearest demonstration of what delegation is for once availability is
not the question.

## Approval: a per-call layer

Availability decides whether a call can happen at all. Approval decides, for a call that can, whether
it asks first. Kokua has two buckets beyond "just runs":

- **`confirm`** asks you.
- **`review`** asks a model reviewer first, and asks you if the reviewer does not approve. See
  [Auto-approval](auto-approval.md) for what that reviewer is and is not.

A tool in neither list runs. Entries use one vocabulary, the one `available` uses: `<toolset>` (or
`<toolset>.*`) for a whole capability, `<toolset>.<tool>` for one tool, `core.<tool>` for the reserved
two. The shipped default names `fs_write` bare so a writer AIMU adds to that group later is gated on
arrival, and names `compute.execute_python` explicitly because `compute` also holds `calculate`.

The same name is what you watch. Every tool call Kokua shows (a card, a terminal line, an approval
prompt, a reload, an export) reads `toolset.tool`, so the entry you would write to gate a call is printed
on the call itself. The toolset is the one that gave *the calling agent* that tool, which matters when
two toolsets offer one name and two agents hold different ones.

This is the layer other frameworks call permissions or authorization policy. Managed Agents has three
policies, `always_allow`, `always_ask`, and `auto`; OpenClaw's approval engine says allow, deny, or ask;
Claude Code has permission rules plus an auto mode. Kokua says "approval" because the code already did
(`HumanGate.approve`, AIMU's `tool_approval`, the approval card), and because "authorization" is already
taken in this codebase by MCP's OAuth flow.

Note what is missing: there is no deny bucket in the approval lists. A tool an agent must never call
belongs outside its `available` list, which is stronger than a refusal, since the tool does not exist
for that agent. And the reviewer in `review` cannot deny either, which is the whole of the claim in
[Auto-approval](auto-approval.md#the-security-claim-in-one-sentence), and a deliberate contrast with
Managed Agents' `auto`, where the model's verdict stands.

### The pair is inherited whole

`confirm` and `review` are one policy written as two lists, so they are declared or inherited together.
An agent whose `[agents.<name>.tools]` table declares neither inherits `[assistant.tools]`'s pair. An agent that
declares either one declares its whole policy, and a key it leaves out is empty:

```toml
[agents.coder.tools]
available = ["compute", "fs_write", "time"]
confirm = ["fs_write"]        # review is now empty, not inherited
```

The reason is that inheriting each key independently lets an inherited `review` entry overlap a declared
`confirm` entry, and neither "the declared one wins" nor "merge them" is something a reader could
predict. Taking the pair whole costs one foot-gun: writing `confirm = ["fs_write"]` to add a gate also
drops the default's other entries, because a declared policy *replaces* the default and never merges. It
has to replace, because [principle 6](design-principles.md#6-security-is-explicit-and-user-controlled)
allows loosening a control as well as tightening it. Rather than change the rule, Kokua shows you the
result: `kokua agents` prints each agent's resolved lists and where they came from, and startup logs a
line per agent to `kokua.log` (printing it too when started with `--debug`).

### Each agent's gate is bound to that agent

A gate entry is a tool name, and the policy that matters is the one belonging to whoever is making the
call. Kokua resolves one policy per agent after every agent is wired (a tool name exists only once its
toolset has been built, which is also why an entry for a deferred toolset waits for its
[build](#what-a-load-checks-approval-before-any-tool-is-callable)), and binds the human gate to the
agent's name. The entry agent's gate is its own.
A delegated worker's gate rides in its spawn spec's `"tool_approval"` key (AIMU 0.37.0), bound to that
worker's name, so a `coder` that declares `confirm = ["fs_write"]` is held to that and a `researcher`
inheriting the default is held to the default.

A composed worker (`compose_subagent`, which builds one for a single task) is not an agent the config
describes, so it is gated as `composed:<label>` and always gets the default tier. That is the safe
direction: a worker built from the whole registry for one call is never *less* governed than the
default, and no table has to exist for it. It is also why `:` and `[` are reserved in agent names; they
are how the gate tells a declared agent from a composed one and from the default tier's own label.

The approval prompt says which agent is asking ("Allow coder to run ..."), because with several agents
holding different policies, a bare tool name no longer tells you whose call you are approving.

### What startup checks

A gate that holds nothing back is the one mistake whose symptom is the absence of a symptom, so each of
these fails startup rather than warning:

- an entry matching no tool, or naming a toolset the agent does not list in `available`;
- a tool listed in both `confirm` and `review` of one policy;
- `[security.auto_approval]` enabled with no `review` entry anywhere, or a `review` entry naming a tool
  in `[security].never_auto_approve`, the floor beneath the reviewer. That second check only runs when
  auto-approval is enabled.

An entry naming a toolset the agent defers that is not built yet (no agent holds it resident, and it
is not a skill the entry agent already holds) is the one exception to "at startup": startup checks its shape, and the build checks the rest, refusing the toolset rather than letting it load ungated (see
[What a load checks](#what-a-load-checks-approval-before-any-tool-is-callable)).

With auto-approval off, `review` entries prompt you exactly as `confirm` ones do: turning it off never
ungates a call. Unattended turns still auto-deny every gated call, whichever list holds it.

## Where the old keys went

Before this split, one list per agent (`[agents.<name>].tools`) and two global lists
(`[security].confirm_tools` and `[security.auto_approval].tools`) carried all three questions at once.
Those keys now fail startup with a message naming their replacement; there is no migration code. The
table is in the [configuration reference](../reference/configuration.md#removed-keys).

## See also

- [Configuration reference](../reference/configuration.md#agentsnametools): every key, the pair rule, and
  the removed keys.
- [Auto-approval](auto-approval.md): the `review` bucket in full, and how other assistants build it.
- [Design principles](design-principles.md): principles 2 and 6, which this page is the long form of.
- [Architecture](architecture.md#how-an-agents-tools-resolve): where each axis is resolved in code.
- [`core/approval.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/approval.py): the module
  that resolves each agent's policy.
- [`core/agents.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/agents.py): `TRUST_ORDER`,
  `available_for`, and the shadow check.
