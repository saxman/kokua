# Configuration reference

Every `config.toml` key Kokua reads, what it accepts, and what it does. This is the long form of
[`config.example.toml`](https://github.com/saxman/kokua/blob/main/src/kokua/config.example.toml), which is the same set of keys with a line
of description each; read that file to see the shape, and this one when a key's behavior is not obvious
from its name.

Two rules run through the whole file and explain most of what follows:

- **A capability is declared, never defaulted.** An agent holds exactly the toolsets its
  `[agents.<name>.tools].available` list names, or every installed toolset when it declares `"all"`. No
  code path adds one it did not declare, and no flag can disagree with the declaration. See [set up a toolset](../how-to/set-up-toolsets.md).
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
  [`[assistant.tools]`](#assistanttools) · [`[security]`](#security) · [`[security.auto_approval]`](#securityauto_approval) ·
  [`[agents.<name>]`](#agentsname) ·
  [`[agents.<name>.tools]`](#agentsnametools) · [`[reviewers.<name>]`](#reviewersname) ·
  [`[[mcp.server]]`](#mcpserver) · [`[paths]`](#paths) · [`[frontend]`](#frontend) · [`[web]`](#web) ·
  [`[logging]`](#logging) · [`[email]`](#email) · [`[scheduling]`](#scheduling) ·
  [`[scheduling.task.<name>]`](#schedulingtaskname) · [`[planning]`](#planning) ·
  [`[capabilities]`](#capabilities) · [`[github_backup]`](#github_backup) · [`[compute]`](#compute) ·
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
locked_config_keys = [
    "security.*",
    "email.to",
    "paths.data_dir",
    "agents.*",
    "assistant.tools.*",
    "reviewers.*",
    "scheduling.task.*",
    "compute.command_env_passthrough",
]
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
`Agents.*`, and `security.never_auto_approv` are all refused. What no check can catch is a name only you
bring into being. `agents.resercher.*` and `scheduling.task.mornin-brief.*` name an agent and a task
that could be created tomorrow, so there is no closed set to test them against, and each one loads while
locking nothing. See [`locked_config_keys`](#locked_config_keys) for the full list of what is refused.

**One key is always locked:** `[security].locked_config_keys` itself, whatever the list says, including
when it is empty. Otherwise an assistant holding `update_config` would need a single call to disable
every other lock. Note how narrow that is: only this key, not the `[security]` section around it. Set
`locked_config_keys = ["email.to"]` and `[security].never_auto_approve` becomes writable by the
assistant, because the shipped `security.*` pattern was the only thing that was locking it.

Everything else is yours to remove, and here is what removing each shipped pattern actually permits:

| Pattern | Removing it |
| --- | --- |
| `security.*` | lets the assistant change `never_auto_approve` and the `[security.auto_approval]` sub-table, and so widen what a reviewer may answer for |
| `assistant.tools.*` | lets the assistant change `[assistant.tools].confirm` and `review`, and so remove its own approval gate |
| `email.to` | lets the assistant mail someone other than you |
| `paths.data_dir` | lets the assistant move its own state out from under you |
| `agents.*` | lets the assistant rewrite any agent's `[agents.<name>.tools]` table (its `available` list and its own `confirm` and `review`), `model`, `thinking`, `max_iterations`, `system_message`, `description`, and `delegates_to`, set its `[agents.<name>.generation]` parameters, and create new agents (under a name of letters, digits, hyphens, or underscores). It can widen its own reach, effective on the next restart. |
| `reviewers.*` | changes the error message only. `update_config` refuses `[reviewers.*]` whatever the list says, for the reason it is listed here: a reviewer's prompt and model decide how the assistant's own work is judged, including (with [`[security.auto_approval]`](#securityauto_approval) on) whether a gated call reaches you at all. Hand-edit to change one. |
| `scheduling.task.*` | changes the error message only. `update_config` still cannot write a task: the scheduling tools are the write path, because a task write has to be paired with the scheduler arming or disarming to match, and a bare config write would leave the running scheduler firing the old schedule. |
| `compute.command_env_passthrough` | lets the assistant name its own credentials for a `run_command` child to see; because the key is cold, that exposure is persistent, surviving into every later session rather than one command |

A flat agent key (`model`, `thinking`, `system_message`, `description`, `delegates_to`) is
checked before it is saved by the same `validate_agents` that runs at startup, so an unknown toolset
name, an unresolvable model, an unknown delegate, or a delegation cycle is refused at the tool rather
than breaking your next launch. That check reads `config.toml` itself, not the copy this session started
from, and reads both halves of it that startup reads: the agent tables and the `[assistant].agent`
naming the entry agent. So two writes that each looked fine alone cannot add up to a file your next
launch refuses. A `[agents.<name>.generation]` parameter gets a different check instead:
the same type-and-range check `[assistant.generation]` gets, not a `validate_agents` dry run, since a bad
`temperature` cannot break startup the way a bad delegate can. A flat agent key that carries a check of
its own keeps it here too, on top of the dry run rather than instead of it, because the two answer
different questions: the dry run asks whether the whole file would still start, and a per-key check asks
whether this one value is one the key accepts at all. So `thinking = "bogus"` and `max_iterations = 0`
are both refused at the tool, where each used to be written and then refused by the next launch. Every
way in, the write is checked before it is saved; no check tells you the result is one you wanted, only
that it starts.

`[assistant].max_iterations` is not locked, so the assistant can raise or lower its own tool-loop cap
there. That is deliberate, for the reason `[assistant].model` is unlocked: the key is startup-only, so
no write escalates the session doing the writing, and what it moves is cost rather than reach.

`[[mcp.server]]` is not in the list and is not writable by `update_config` either way; it is appended by
`add_mcp_server`, which connects the server as well as recording it.

## Which keys apply live

A **hot** key takes effect on the next turn, with no restart, and is written back to `config.toml`
immediately. Everything else is **startup-only**: change it, then restart.

| Hot | Startup-only |
| --- | --- |
| `[assistant].generate_titles`, `[assistant].capture_requests` | the rest of `[assistant]`, including `[assistant.generation]`, and `[planning].review_rounds` |
| `[planning].plan_review`, `plan_review_agent`, `result_review`, `show_reasoning` | |
| `[capabilities].max_depth` | all of `[agents.*]` (including each `[agents.<name>.tools]`), `[assistant.tools]`, and `[reviewers.*]`, `[mcp]` (including `[[mcp.server]]`), `[security]` (including `[security.auto_approval]`), `[paths]`, `[frontend]`, `[web]`, `[logging]`, `[email]` |
| `[scheduling].max_task_conversations` | |

The model, the reasoning effort, and the tool-loop cap read like runtime settings and are not. Nothing
rebinds a live model client, nothing re-caps a live agent's loop, and no runtime writer can reach
`[agents.*]`, so a hot `[assistant].model` or `[assistant].max_iterations` could only ever report a
change it had not made, or disagree with an agent's own declaration. Generation parameters are
startup-only for a second reason as well: a live control always holds *some* value, so it would write
that tier whether or not you asked for anything, shadowing every model card's tuned profile. A key absent
from the file has to stay absent from the request.

A toolset declares its own settings and says which of them are hot. See [toolset
sections](#toolset-sections).

---

## `[assistant]`

Process-wide defaults. Everything here is read at startup, except `generate_titles` and `capture_requests`, which are
hot.

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

Both suffixes survive the `$AIMU_LANGUAGE_MODEL` route as well, which is worth stating because it is the
natural way to share one `config.toml` across machines that serve different models:

```bash
export AIMU_LANGUAGE_MODEL="ollama:qwen3.8:27b@http://gpu-box:11434"
```

Whichever route the default arrives by, it is resolved once and everything Kokua builds gets the same
string: sub-agents spawned through `spawn_subagent`, sub-agents built by `compose_subagent`, the
prebuilt orchestrators in the `aimu_agents` toolset, and every reviewer (`/plan`'s two critics, and the
approval reviewer if you declare one). An endpoint set here reaches all of them. (Before Kokua 0.1.0 it
did not: with `model` unset, the endpoint was dropped for everything except the entry agent, so a remote
default sent every sub-agent to the *local* server instead. It failed loudly only when nothing was
listening there.)

One thing to know about that last group, because it is the way an endpoint pinned here stops reaching
one of them. A reviewer resolves [`[reviewers.<name>].model`](#reviewersname) first and falls back to
this key, exactly as an agent resolves `[agents.<name>].model`. So a `[reviewers.plan]` table naming a
bare `provider:model` drops the endpoint, and that one reviewer talks to the default server while
everything else talks to the one you pinned. Pin the endpoint in the reviewer's table too, or leave its
`model` unset.

Note this pins the client, not discovery. The probe that picks a default when nothing is set at all
looks at default endpoints only, so export `OLLAMA_HOST` as well when a remote server's models should be
considered there too.

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

### `agent_cache_cap`

Maximum per-conversation agents kept live in memory. Default `8`. An evicted agent rebuilds from
persisted state on next access, so the cap bounds memory, not correctness.

### `max_iterations`

The tool-loop cap every agent runs under unless its own `[agents.<name>].max_iterations` overrides it:
the most model calls one turn may make before AIMU stops the loop. Default `10`, which is also AIMU's
own default, so leaving this unset changes nothing.

```toml
[assistant]
max_iterations = 10
```

The count is model calls, not tool calls: the initial turn, plus every tool-follow-up turn, plus any
continuation nudge the loop injects after an empty turn. The one call it does not count is the forced
wrap-up, which runs *after* the cap is reached with tools disabled, so an agent that hits the cap still
gets to answer from what it gathered. That is why hitting the cap is not an error and nothing in the
transcript is marked truncated: you get a thinner answer, not a failure. [The turn
loop](../how-agents-work/the-turn-loop.md) shows what that looks like in a real run.

Must be an integer of 1 or more. A `0` would be a loop that makes no model call at all, so it is a
startup error rather than a silently useless setting.

Raise it for work that legitimately needs more rounds, which in practice means search: a worker that
spends every round calling tools is the one still mid-investigation when the cap lands. Prefer raising it
on the one agent that needs it, via the per-agent key below, over raising it here for everyone, since
every agent's worst-case turn cost scales with this number.

This key is not locked, so the assistant can change it too: see [who may change which
key](#who-may-change-which-key).

### `generate_titles`

Have the model write each new conversation's title from the message that opened it. Default `true`.
**Hot**: the title is written per conversation, so a change
applies to the next one with no restart.

The first message truncated to 40 characters is the title the instant a conversation has one; the
written title replaces it a moment later, in the background, on the model that conversation runs on
(no separate model to configure). `false` stops at the truncation, which is also what stands whenever
the call fails: an endpoint that is down, or an answer that cleans up to nothing, is silent and leaves
the placeholder. A conversation a scheduled task minted keeps the task's name either way.

### `capture_requests`

Keep each model call's exact request and response as files under `$KOKUA_HOME/data/payloads/`, so a turn
can be examined afterwards as the model saw it. Default `true`. **Hot**: it is read as each turn opens, so
a change applies from the next turn with no restart.

The cost is disk, and it grows with the square of a conversation's length, because every call resends the
whole context: a 10-round turn at about 30k tokens of context is roughly 1.2 MB. A payload carrying images
holds their base64 data, repeated on each call. Nothing collects old files yet
([#15](https://github.com/saxman/kokua/issues/15)). `false` stops writing the files only: how each agent
loop ended (answered, wrapped up, or failed) is recorded either way, under the turn's entry in the
conversation's metadata.

The key is not locked, so the assistant can change it through `update_config`, like `generate_titles`; lock
it via `[security].locked_config_keys` to keep it fixed.

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
- **`context_length` has a second job, and it applies on every backend.** A spawned sub-agent trims its
  own messages before each model turn to fit the window declared for it, so a long delegation bounds its
  own growth instead of failing once the window fills. Three quarters of the declared window is left for
  messages, since the same window has to hold that worker's system message, its tool block, and the
  reply. Declaring no `context_length` leaves the trimming off: nothing else states a window (no client
  reports the one it is talking to), and guessing one would mean rewriting a worker's messages against a
  number nobody wrote down. A worker with no declared window instead reports, when it fills, that *its*
  window filled and that yours is neither the cause nor something it ever saw.
  **The conversation you are in is never trimmed**, whatever you declare here: it is yours to read and
  Kokua stores it, so dropping from it would delete history still on screen. A worker's messages are
  built per spawn and discarded with it, which is what makes an automatic rewrite of them safe at all.

Because this is a TOML sub-table, it must come **last** in `[assistant]`: any plain `[assistant]` key
written after the `[assistant.generation]` header would belong to the sub-table instead. The same is true
of an agent's own generation table.

## `[assistant.tools]`

The default approval policy: which tool calls ask before they run, and who answers. Every agent whose own
[`[agents.<name>.tools]`](#agentsnametools) table declares neither key inherits this pair, and a composed
worker (see [`[capabilities]`](#capabilities)) always uses it. Startup-only, and locked by default under
`assistant.tools.*`, so only a hand-edit changes it. For why approval is its own axis, see
[Tool access](../explanation/tool-access.md).

```toml
[assistant.tools]
confirm = ["skills.add_skill_script", "mcp.add_mcp_server", "config.update_config"]
review = ["compute.run_command", "compute.execute_python", "fs_write"]
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `confirm` | list of strings | six entries, see below | calls that ask you |
| `review` | list of strings | `[]` | calls that ask a reviewer first, then you |

The block above is what `config.example.toml` ships. A config that sets neither key gets the schema
default instead: all six tools the old `confirm_tools` gated (those three plus `compute.execute_python`,
`compute.run_command`, and `fs_write`) in `confirm`, and an empty `review`, so it gates exactly what it
always did. Enabling auto-approval on that default is a startup error until you move entries into
`review`, since a reviewer with nothing to answer for would do nothing.

This table holds only those two keys. `available` or `resident` here is a startup error, because
availability is declared per agent and never defaulted. The two lists must not name the same tool.

### `confirm`

Tools that require interactive confirmation before each call: a terminal `y/N`, or Allow/Deny in the web
UI. Set to `[]` to clear this list; calls in `review` still prompt, so turning approval off entirely means emptying both `confirm` and `review`. Proactive turns (scheduled tasks, anything the assistant
starts unprompted) **auto-deny** a gated call regardless, since there is no one at the keyboard to ask.
`--confirm-tools` overrides this key for one run.

The default entries each reach past the model: `config.update_config` rewrites this file (except the
locked keys it can never change, and this table is one of them by default), and `skills.add_skill_script`
and `mcp.add_mcp_server` each install something that will run later.

### `review`

Tools that ask a model reviewer first, and you when the reviewer does not approve. With
[`[security.auto_approval]`](#securityauto_approval) off (the shipped state) a `review` entry prompts
you exactly as a `confirm` entry does, so turning auto-approval off never ungates a call, and a dead
entry fails startup either way. Turning it on lets a declared reviewer answer for you on these calls
alone. The reviewer can only approve; there is no deny verdict (see
[Auto-approval](../explanation/auto-approval.md)).

The default entries are the tools that act with this process's privileges: `fs_write`'s tools name the
path, and `compute.execute_python` and `compute.run_command` name their own target. The writers are
gated for a reason worth stating, because leaving them out would have been the quiet mistake:
`[agents.coder]` holds `run_command`, which is gated because a shell can write a file. An ungated
`write_file` beside it is the same outcome by a shorter route, so the gate would have been approving the
long way round and waving the short one through.

Note `fs_write` is named bare where the rest name a tool. That toolset is
`select(builtin.fs, include=builtin.unscoped)`, so it is every writer AIMU puts in that group and
nothing else, and the bare name therefore gates a writer a later AIMU adds as well. Spelling out
`fs_write.write_file` and `fs_write.edit_file` would gate today's two and let tomorrow's third through
in silence, which is the drift [`toolsets/fs.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/fs.py)
selects by reach to avoid, reappearing one layer up. `compute` cannot be named bare for the reverse
reason: it carries `calculate`, which no gate should stop.

### Every entry names its toolset

An entry is a tool's name with the toolset that provides it in front, and it takes one of three forms:

| Form | Example | Gates |
|---|---|---|
| `<toolset>` | `compute` | every tool that capability provides |
| `<toolset>.*` | `compute.*` | the same thing, said with a wildcard |
| `<toolset>.<tool>` | `compute.execute_python` | that one tool |

The wildcard is optional and says nothing the bare toolset does not; it is accepted because a reader may
expect a glob to be required. It is legal only as the whole tool. `compute.exec*`, `comp*.run_command`,
and a bare `*` are all rejected, because a pattern quietly matching fewer tools than its author meant is
the same silent gap as a name matching none.

Two tools have no toolset, so a reserved `core` prefix holds them: `core.spawn_subagent`, the delegation
tool, attached to the entry agent after its toolsets are built, and `core.activate_skill`, which AIMU
gives any skill-holding agent. `core` on its own gates both. A skill's own scripts stay under the skill,
which is also its toolset name: `weekly-digest` gates every script that skill ships, and
`weekly-digest.weekly_digest__collect_notes` gates one. `activate_skill` is deliberately *not* one of
them, since every skill's tool list carries a reference to it and attributing it per skill would let a
gate on one skill hold back the entry point to all of them.

Naming a toolset is a broader statement than naming its members, and it means what it says:
`compute` gates `calculate` as well as the two execution tools, which the shipped default leaves alone.

Gating is by tool name at call time, resolved per agent: the gate an agent's calls pass through is bound
to that agent's own policy, so a worker's gated call is routed to you under the worker's policy, not the
delegator's. The prefix is a declaration vocabulary that startup resolves down to tool names; it is not
a discriminator at the moment of the call. Note the flip side, which is easy to misread: there is no
privilege tier among agents. An agent whose table declares `config` really does get `update_config`, and
one declaring `compute` really does get `execute_python`. Your hand-edit is the consent, and the policy is
the gate at call time.

### An entry that would gate nothing fails startup

Every fault is refused at startup, naming each bad entry and suggesting the close names. An entry that
gates nothing holds nothing back, and the only sign of it is a prompt that never comes, which is not a
thing anyone notices: a gate entry misspelled `execute_pythn` once loaded clean and then let
`execute_python` run unattended for as long as the file stood. Five ways an entry can be that entry:

- **A bare tool name.** `execute_python` with no prefix is refused, and the error names the form to
  write instead (`compute.execute_python`).
- **The wrong toolset.** `web.execute_python` is spelled correctly and filed wrong; the error says which
  toolset does provide it.
- **A toolset the policy's agent cannot reach.** A declared pair is checked against that agent's own
  `available` list, so `confirm = ["compute"]` on an agent that does not list `compute` fails naming
  the agent. The default tier is checked against every tool any agent builds, since an inheriting agent
  may be any of them.
- **A toolset no agent declares.** `github_backup` is a real toolset the shipped config gives to nobody,
  so nothing under it can be called. Name it in an `[agents.*]` table first.
- **A toolset that provides no tools.** `planning` contributes a workflow, so there is nothing under it
  to hold back.

A tool in both `confirm` and `review` of one policy is refused too: pick one.

The limit is anything that does not exist when startup ends. A tool from an MCP server the assistant
connects later with `add_mcp_server` has no name to match yet, and neither does a tool only
`compose_subagent` would build, out of a toolset no `[agents.*]` table names. Listing either one is
refused, which will look wrong if you were deliberately gating ahead of the connection. To gate an MCP
tool, give its server a `[[mcp.server]]` table here and name that server in an agent's `available`; to
gate a tool from a toolset nothing declares, name that toolset in an agent's `available`. Both are known
from the next start, and both are named by their server or toolset like anything else:
`stocks.get_quote`, or `stocks` for all of them.

Four names worth considering adding, all ungated by default: `conversations.read_conversation`,
`conversations.search_conversations`, `conversations.rename_conversation`, and
`conversations.export_conversation` (or `conversations` for the lot). A saved transcript is
untrusted text, since a worker may have pasted web content into it, so an injection landing in one
conversation can influence another. The two reads are ungated by default because gating a read would
make an unattended scheduled run that reads history fail silently. The rename writes, but it writes one
field: the worst an injection gets from it is a conversation misleadingly named in the sidebar, with no
message touched and nothing lost. The export is the one whose reason to gate is not about the
conversation at all: it changes nothing and can only write inside `downloads_path` under a name taken
from the conversation's own id, but what it writes there is a whole transcript in the clear, in a folder
`/download/{name}` serves unauthenticated. Gate it if that folder is the part you are protecting.

## `[security]`

The controls that are not a per-agent policy: the floor beneath the reviewer, and which keys the assistant
may not write. The approval policies themselves live in [`[assistant.tools]`](#assistanttools) and
[`[agents.<name>.tools]`](#agentsnametools).

### `never_auto_approve`

Tools no reviewer may ever approve on your behalf, whatever
[`[security.auto_approval]`](#securityauto_approval) names. Same vocabulary as [`[assistant.tools]`](#assistanttools).
Default:

```toml
never_auto_approve = [
    "config.update_config",
    "skills.add_skill_script",
    "mcp.add_mcp_server",
]
```

The three are the tools that change what may act *later* rather than acting once, so the arguments a
review was about stop constraining anything the moment the write lands. A waved `update_config` can
widen this very list, a waved `add_skill_script` writes a script that becomes a tool the next turn can
call, and a waved `add_mcp_server` adds a whole source of tools. Reviewing the arguments of a call like
that answers a question about one action, when what the call actually grants is a capability.

Naming one of these in any policy's `review` list, by the tool or by its whole toolset, is a startup
error saying so, and so is an entry here that matches no tool at all: a floor holding nothing back is
the same silent gap a gate matching nothing is. (An entry on a toolset nobody has built yet, such as
`mcp.add_mcp_server` while `mcp` waits to be loaded, has only its shape checked at startup; a misnamed
tool there refuses that toolset's build instead, which fails just as closed.) Both checks run only when the gate is enabled, which is worth knowing, because it means a
typo in this list sits unreported until the day you switch the gate on (nothing reads the list before
then, so there is nothing for it to fail to hold back). Startup-only, and locked by default under
`security.*`, so only a hand-edit changes it. Emptying it is a hand-edit you may make, and what it buys
is a reviewer that can approve a capability grant.

This list is not a second approval gate: `confirm` and `review` decide what stops and asks, and this
decides what a reviewer may never answer for.

### `locked_config_keys`

A list of patterns naming which keys `update_config` refuses. Default:
`["security.*", "email.to", "paths.data_dir", "agents.*", "assistant.tools.*", "reviewers.*",
"scheduling.task.*", "compute.command_env_passthrough"]`. Startup-only, and
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
name one of them, which is what refuses `security.never_auto_approv` while accepting
`security.never_auto_approve`.

Neither check can see a name that does not exist yet, and neither tries to. The `[agents.<name>]` and
`[scheduling.task.<name>]` sections are yours to create, so `agents.resercher.*` is accepted and locks
nothing until an agent by that name exists. Locking a section you are about to add is a legitimate thing
to write; a misspelling of one is indistinguishable from it.

## `[security.auto_approval]`

A declared model reviewer may approve a `review` call (see [`[assistant.tools]`](#assistanttools)) in your
place, so you are prompted less. Which calls it may answer for is not a key here: it is every tool in the
policy's `review` list. Off by default, and the whole sub-table is startup-only and locked under `security.*`.

```toml
[security.auto_approval]
enabled = false
reviewers = ["approval"]
timeout_seconds = 10
max_per_turn = 5
```

**This is a convenience layer, not a security boundary.** It reduces prompts; it contains nothing. The
one property it does guarantee is narrow and structural: a review can only turn a prompt into an
approval. There is no deny verdict anywhere in it, so nothing here can change what you would have been
able to decide, and every way a review can fall short arrives at the ordinary approval prompt with the
reviewer's sentence attached. Read
[Auto-approval](../explanation/auto-approval.md) before switching it on: it says what the reviewer is
asked, what it cannot see, and what this design does not do (no sandbox, no shell parsing, and a
reviewer whose token cost is not counted).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | whether any `review` call is reviewed at all |
| `reviewers` | list of strings | `[]` | which [`[reviewers.<name>]`](#reviewersname) tables are asked |
| `timeout_seconds` | number > 0 | `10` | past this, the review escalates rather than being waited on |
| `max_per_turn` | integer >= 1 | `5` | most auto-approvals one turn may collect |

Name more than one reviewer and **all of them must agree**: they are asked in order, and the first
withheld answer ends the round, so a quorum costs every reviewer only when the ones before it agreed.

`review` entries are checked at startup, and a fault in either check is a hard error rather than a
warning, for the reason every control in `[security]` is: the symptom of a broken gate is the absence of
a symptom. A `review` entry must name something that exists (a dead entry fails startup whether
auto-approval is on or off), and, when `enabled = true`, it must not name anything
[`never_auto_approve`](#never_auto_approve) holds back. Enabling the gate with no reviewer, or with no
`review` entry in any policy, is refused the same way, each with the config fix in the message.
With `enabled = false` a `review` entry simply prompts you, so switching this table off never ungates a
call.

The shipped `review` list names both `compute` execution tools, and leaving `execute_python` out would
have been the incoherent choice rather than the cautious one. `run_command` is a shell, so `python3 -c`
from there runs Python with no restrictions at all; excluding the Python tool withholds nothing and only
routes the same capability through the less contained of the two. `execute_python` runs in a subprocess
under a timeout, with an import allowlist and restricted builtins that put `os`, `subprocess`, and even
`open` out of reach, and the process environment (API keys included) is invisible to it. If you want to
narrow that list, `run_command` is the entry to drop.

`max_per_turn` is per *turn*, counted across every agent in it, and the budget is spent when a review
is **attempted**, not when one approves. A model retrying the same rejected call cannot collect one approval per attempt, and a loop
that keeps being escalated stops costing reviews after the fifth. Each reactive turn gets its own
budget, so one conversation's loop cannot spend another's.

An unattended turn (a scheduled task, anything the assistant starts unprompted) is never auto-approved.
Its gated calls are denied before a reviewer is consulted at all, which is the behavior
the gate already had and is unchanged by this table.

Both outcomes are reported. An approval and an escalation each arrive as a card naming the tool, its
arguments, the model that answered (every model in a quorum, or the one reviewer that is the reason for
the outcome), and its sentence, because an auto-approval nobody saw is a decision made on your behalf
in silence.

## `[agents.<name>]`

Every agent, declared whole. The table name is the agent's name: it is what `[assistant].agent` selects,
what another agent's `delegates_to` names, and what `spawn_subagent` takes as its `agent_type`.

A name you write by hand is any TOML key, quoted where it has to be. A name `update_config` writes is
narrower, letters, digits, hyphens, and underscores, so the section the tool reports is the section that
reaches the file: anything else would be quoted on the way in and land under a name you could not name
again. `agents.*` is refused for that reason and one more, being the wildcard a lock pattern covers the
whole section with rather than an agent.

**Two names are refused outright: `user` and `everyone`.** An agent's name is also the address a message
typed mid-turn is sent to, and those two already mean something there: `user` is the user's own address,
and `everyone` reaches every run. An agent declared under either would collide rather than merely
confuse, so it is refused twice over: `update_config` refuses the write when it is attempted, because it
dry-runs the same startup check, and a config that reaches the file some other way (a hand edit) is
refused at startup, naming the agent to rename.

**This whole section is locked by default.** See [who may change which key](#who-may-change-which-key)
for what removing `agents.*` from `[security].locked_config_keys` actually permits.

| Key | Type | Meaning |
| --- | --- | --- |
| `description` | string | the label a delegator sees in its worker menu |
| `system_message` | string | this agent's opener; falls back to `[assistant].system_message` |
| `tools` | sub-table | **this agent's capability and approval policy**: see [`[agents.<name>.tools]`](#agentsnametools) |
| `delegates_to` | list of strings | agent names this agent may spawn as workers |
| `model` | string | overrides `[assistant].model` for this agent alone |
| `thinking` | bool or level | overrides `[assistant].thinking` for this agent alone |
| `generation` | sub-table | overrides `[assistant.generation]`, **per key** |
| `max_iterations` | integer >= 1 | overrides `[assistant].max_iterations` for this agent alone |

### `delegates_to`

A non-empty `delegates_to` is what makes an agent a delegator. There is no separate switch that could
disagree with it. The agent gets a `spawn_subagent(agent_type, task)` tool offering exactly the agents it
names, each built from its own table. The graph must be acyclic.

A worker's gated tool calls are judged by the worker's own approval policy (see
[`[agents.<name>.tools]`](#agentsnametools)) and routed to you, not run unattended.

### `model`, `thinking`, `generation`, `max_iterations`

All four are resolved per agent and **never inherited down the delegation graph**: a delegator that pins
a big model, reasons hard, runs cold, or loops longer does not drag its workers along. A worker declaring
nothing runs on the `[assistant]` defaults like every other undeclared agent.

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
system_message = "You are a research sub-agent. ..."

[agents.researcher.tools]
available = ["web", "misc", "time"]

[agents.researcher.generation]  # sub-tables after the plain keys, like [assistant.generation]
temperature = 0.2
context_length = 131072
```

A model string AIMU cannot resolve fails startup naming the table it came from.

`max_iterations` follows that rule too, and it is worth spelling out. An assistant that declares
`max_iterations = 40` does not hand 40 to the workers it delegates to; each of them gets its own
declaration if it has one, and `[assistant].max_iterations` otherwise. So raising a delegator's cap
buys the delegator more rounds of *delegating*, not more rounds inside each worker. To give a worker a
longer leash, declare it on that worker.

```toml
[agents.researcher]
max_iterations = 25    # a search-heavy worker: every round spends a tool call
```

## `[agents.<name>.tools]`

An agent's tool access, one subtable per agent. Startup-only, and locked by default under `agents.*`.
The page that explains the shape is [Tool access](../explanation/tool-access.md); this is the reference.

```toml
[agents.researcher.tools]
available = ["web", "misc", "time", "messaging"]

[agents.coder.tools]
available = ["compute", "fs_write", "time"]
confirm = ["fs_write"]

[agents.assistant.tools]
available = ["memory", "documents", "skills", "config", "mcp", "scheduling", "conversations", "planning",
             "capabilities", "time", "benchmark", "messaging"]
resident = ["memory", "config", "conversations", "planning", "capabilities", "time", "messaging"]
```

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `available` | list of strings, or `"all"` | yes | **this agent's capability**, named from the one toolset namespace |
| `resident` | list of strings | no (yes with `"all"`) | the part of `available` in the agent's context from its first turn; the rest loads on demand. Unset: all of it |
| `confirm` | list of strings | no | calls that ask you; see [`[assistant.tools]`](#assistanttools) for the vocabulary |
| `review` | list of strings | no | calls that ask a reviewer first, then you |

### `available`

An agent's `available` list *is* its capability. Nothing is added in code, so a toolset you delete from a
list is gone, and one you add is there on the next start. Whether a toolset's tools are in the agent's
context from its first turn or loaded when the model asks for them is [`resident`](#resident), a separate
key; either way the toolset is the agent's to call. The one alternative to writing the list out is
[`"all"`](#available-all), below.

There is one namespace for every capability, so a name may be an AIMU built-in tool group (`web`, `fs`,
`fs_write`, `compute`, `time`, `misc`, `audio`, `speech`, `transcription`), one of Kokua's own (`memory`,
`documents`, `skills`, `capabilities`, `config`, `conversations`, `mcp`, `messaging`, `planning`,
`scheduling`),
one of Kokua's optional toolsets (`aimu_agents`, `benchmark`, `github_backup`, `image`, which
`--list-toolsets` groups with the rest of Kokua's as built-in toolsets), a third-party plugin toolset, a
skill in your skills folder
named by its own name, or an MCP server configured under `[[mcp.server]]`, named by its `name`. The list
does not say which kind a name is. Run `kokua --list-toolsets` for every name this install accepts,
grouped by what provides it, and `kokua agents` for what each declared agent holds.

An unknown name is a startup error listing the valid ones, so a typo can never quietly leave an agent
with a smaller toolset than you wrote. Nothing is implicit, including the clock: `time` is added to no
agent in code, which is why every shipped agent lists it. An installed plugin toolset and a configured
MCP server likewise do nothing until some agent names them (or declares `"all"`), and nothing warns
about one that nothing names. The one warning is narrower: a toolset whose own `[<name>]` section you
wrote, with no agent declaring the toolset (an `"all"` agent declares every one), gets a line in the log
at startup, since those settings are read by nobody.

The full inventory of what each toolset contains is in [set up a
toolset](../how-to/set-up-toolsets.md#one-namespace).

### `available = "all"`

The string `"all"` in place of the list: every toolset this install registers. Why it is still a
declaration, and what it costs, is [Tool
access](../explanation/tool-access.md#available-all-every-installed-toolset-still-declared).

```toml
[agents.researcher.tools]
available = "all"
resident = ["web", "misc", "time", "messaging"]
```

- **It expands in trust order:** Kokua's own toolsets, then third-party plugins, then skills, then MCP
  servers, each kind in the order it registered. Where two resident toolsets offer one tool name, the
  earlier one's is kept, and for an agent startup builds (the entry agent and every worker it reaches by
  `delegates_to`) startup raises an alert naming both. A deferred toolset loaded later keeps whichever
  tool the agent already holds, and the load result names the one it skipped and its holder: a group's
  name when the holder was itself loaded, `a tool given to this agent directly` when it is resident.
- **It leaves out `skills` on any agent but the entry agent,** since a spawned worker cannot host it,
  and it never covers the `core` tools: `spawn_subagent` is still earned by `delegates_to`.
- **`resident` is required,** and each entry must be within the expansion: an entry nothing installs, or
  `skills` on a worker, is a startup error saying which.
- **Write it exactly.** `["all"]`, `"All"`, and `"all"` in any casing inside a list fail startup with the fix, and
  `all` is a reserved toolset name, so no toolset can take it. `update_config` writes the same string
  when `[agents.*]` is unlocked, and refuses the same near misses.
- **Skills written mid-session reach it; servers wait for a restart.** A skill written with
  `author_skill`, `add_skill_script`, or `update_skill` reaches an `"all"` worker on its next spawn. An
  MCP server added with `add_mcp_server` reaches it after the next restart if the server was written to
  this file, and never if it was added with a bearer token.

`kokua agents` prints the expansion with its count (`available: all (22): aimu_agents, audio, ...`), and
the startup summary reads `researcher: all (22) available, 4 resident, ...`. The shipped config keeps
every agent on a list and carries `# available = "all"` as a commented line in `[agents.assistant.tools]`.

### `resident`

The toolsets from `available` whose tool schemas are sent from the agent's first turn. The rest of
`available` is *deferred*: the agent gets AIMU's `available_tools` and `load_tools` tools, its prompt names
the deferred toolsets in one line (`Also available, load before use: ...`), and a toolset's tools and
guidance join the agent when the model loads it, for the rest of the conversation. Leave the key out and
every available toolset is resident. Why the axis exists, and what a load costs and checks, is
[Tool access](../explanation/tool-access.md#residency-what-is-in-context-and-what-loads-on-demand).

- **Every entry must be in `available`.** One that is not is a startup error, and `update_config` refuses
  the same write (and an `available` write that would strand a resident entry) when `[agents.*]` is
  unlocked. Order does not matter: tools are presented in `available` order (trust order, for `"all"`).
- **An empty list is legal** and defers everything `available` names.
- **A skill name on the entry agent is unaffected.** The entry agent is a `SkillAgent`, which advertises
  every skill in its catalogue whatever this list says, so a skill there is never deferred. A skill
  named by a worker defers like any other toolset.
- **An approval entry naming a deferred toolset is checked when the toolset is built,** not at startup,
  since its tool names do not exist until then. Two cases are built at startup and so resolve there:
  a toolset some other agent holds resident, and a skill the entry agent's `SkillAgent` holds (every skill when it declares `skills`, otherwise the skills it names),
  even when a worker could also load that skill later. An entry that matches nothing then refuses the build, so the toolset will not load
  until you fix the entry and restart.
- **A deferred MCP server that is not connected refuses to load**, and stays loadable for a retry once it
  is, where a resident one builds empty.

The shipped `[agents.assistant]` keeps `memory`, `config`, `conversations`, `planning`, `capabilities`,
`time`, and `messaging` resident and defers `documents`, `skills`, `mcp`, `scheduling`, and `benchmark`;
the shipped workers declare no `resident`, so every toolset they name is resident.

### `confirm` and `review`

An agent's own pair. **They are declared or inherited together:** a table that declares either key
declares its whole approval policy, and a key it leaves out is empty. A table that declares neither
inherits [`[assistant.tools]`](#assistanttools)'s pair. A declared policy *replaces* the default, never
merges with it, because loosening a control is allowed as well as tightening one. The foot-gun is that
writing `confirm = ["fs_write"]` to add a gate also drops the default's other entries and empties
`review`; `kokua agents` prints each agent's resolved lists and which table they came from, and startup
logs one summary line per agent (printed too when the run logs at DEBUG), so the result is visible rather
than inferred.

Entries are checked against this agent's own `available` list, so an agent cannot gate (or claim to
gate) a capability it does not hold. A composed worker has no table and always uses the
[`[assistant.tools]`](#assistanttools) pair. Agent names may not contain `:` or `[`, which the gate
reserves for `composed:<label>` and for the default tier's own label.

### Removed keys

These fail startup with a message naming the replacement; there is no migration code.

| Old | New |
|---|---|
| `[agents.<name>].tools = [...]` | `[agents.<name>.tools].available = [...]` |
| `[security].confirm_tools` | `[assistant.tools].confirm`, minus any entry that moves to `review` |
| `[security.auto_approval].tools` | `[assistant.tools].review`, and remove those entries from `confirm` |

The `--confirm-tools` flag now overrides `[assistant.tools].confirm` for one run. A tool it names that
the file's `review` list also names is refused as being in both.

If `[security].locked_config_keys` is written out in your `config.toml` (a scaffolded one is), add
`"assistant.tools.*"` to it: the old `"security.*"` entry is what locked `confirm_tools`, so without the
new one `update_config` can rewrite `[assistant.tools]`, the default policy. A missing lock is not an
error, since you may loosen it, and nothing else reports it.

## `[reviewers.<name>]`

A reviewer is one context-free model call whose answer code consumes, declared here so you can read the
standard it is held to. Three names are consumed today, and each is optional:

| Name | Asked by | Undeclared |
| --- | --- | --- |
| `plan` | `/plan`'s plan critic, when [`[planning].plan_review_agent`](#planning) is on | planning's own shipped prompt applies |
| `result` | `/plan`'s result critic, when [`[planning].result_review`](#planning) is on | planning's own shipped prompt applies |
| `approval` | [`[security.auto_approval]`](#securityauto_approval), when it is on | the gate refuses to start |

So a table here is an override, not something you must restate to keep a default. `[reviewers.approval]`
is the exception and says why in its own error: a security reviewer with no stated standard reviews
nothing, so its `system_message` is required rather than defaulted. The shipped `config.example.toml`
declares that one, with the gate off.

| Key | Type | Meaning |
| --- | --- | --- |
| `description` | string | what this reviewer is for. A note to yourself: nothing reads it |
| `system_message` | string | the standard the reviewer is held to |
| `model` | string | overrides `[assistant].model` for this reviewer alone |
| `thinking` | bool or level | overrides `[assistant].thinking` for this reviewer alone |
| `generation` | sub-table | overrides `[assistant.generation]`, **per key** |

Three of those resolve against the `[assistant]` tiers exactly as an agent's do: `model` on "is it
set", `thinking` on "is it unset" (so `thinking = false` is a real declaration rather than one an `or`
would swallow), and `generation` merged key by key, so a reviewer that wants only a colder temperature
keeps the default's context length. See [`[assistant].model`](#model) for what a model string may
carry, including the endpoint suffix and the way a bare `provider:model` here drops an endpoint pinned
there.

A declared `model` is validated at startup, exactly as an agent's is: a name AIMU's catalogue does not
know fails there, naming the table (`[reviewers.<name>].model`), rather than surfacing later as "the
reviewer could not be reached" on the first gated call or plan review it is asked to do. This is a trap
worth naming concretely, because it is easy to hit: a provider's model catalogue here is AIMU's own
short id, not the dated id its API uses, so `anthropic:claude-haiku-4-5-20251001` (the API's id) fails
where `anthropic:claude-haiku-4-5` (AIMU's) resolves. An undeclared `model` is never checked here, since
it inherits `[assistant].model`, which is validated on its own path when it is set.

`system_message` is the one that does **not** fall back to `[assistant].system_message`, and the
difference is deliberate. An undeclared prompt leaves it empty, and what empty means is the consumer's
to decide rather than this file's: the `/plan` critics substitute their own shipped standard, and the
approval gate refuses to start. An agent's opener describes a persona, where a reviewer's describes a
standard, and there is no sensible standard to inherit from an assistant's own greeting.

`description` is read by nobody: it is a label for whoever opens this file, which is why a reviewer's
is worth writing even though no model ever sees it.

**Three keys an `[agents.<name>]` table takes are refused by name**, each with its reason, because a
reader who wrote one was reasoning by analogy from the agents table:

| Refused | Why |
| --- | --- |
| `tools` | whatever consumes a reviewer fixes its tools in code, and this table has no key to change them: the approval gate asks with tools off, and a `/plan` critic gets a curated verification toolset. |
| `delegates_to` | it is one model call, so there is nothing for a worker to be part of. |
| `max_iterations` | it runs no tool loop, so there is no cap to set. |

Declare an agent if you want any of those. The approval reviewer's own call being tool-less is also why
that gate cannot recurse into itself: a reviewer making no tool call has no call for the gate to be
asked about while it is reviewing one. (The `/plan` critics are the one place a reviewer's answer is
produced by a tool-using agent, over a curated read-only toolset the workflow fixes in code; this table
gives it the persona, never the tools. See [Architecture](../explanation/architecture.md).)

`[reviewers.approval]` additionally refuses a declared `thinking`, and the error says why: its answer
comes back through a structured call, which returns JSON and no reasoning on every provider, so a
reasoning request there could never take effect. An effort inherited from `[assistant].thinking` is
inert rather than refused, since that key makes no claim about this reviewer and refusing it would turn
one global setting into an install-wide bar on enabling the gate.

**This whole section is locked by default**, like `[agents.*]` and for the same reason: `update_config`
is a tool the assistant holds, and a reviewer's prompt decides how the assistant's own work is judged.
See [who may change which key](#who-may-change-which-key).

```toml
[reviewers.plan]
description = "Reviews a plan against the request."
thinking = "high"
model = "ollama:qwen3:32b"
system_message = """\
Reject a plan that solves a different problem than the one asked, that cannot be checked when it is \
done, or that spends a tool call it does not need."""
```

Startup-only: a reviewer's client is built per call from the values this file had at startup.

## `[[mcp.server]]`

Remote MCP servers to connect at startup. An array of tables: one `[[mcp.server]]` header per server.

| Key | Required | Meaning |
| --- | --- | --- |
| `url` | yes | the server's endpoint |
| `name` | yes | how the server enters the toolset namespace |
| `token_env` | no | environment variable holding a bearer token, read at startup |
| `description` | no | one line saying what the server's tools do |

`name` is what an agent lists in its `available`, so a server no agent names (and no agent declaring
`"all"` holds) reaches no agent. Omit
`token_env` for an unauthenticated server, or one using the OAuth flow, which triggers automatically on
an auth challenge. The token stays in the environment, never in this file.

`description` is the server's line in the catalogue: what an agent deferring the server reads in
`available_tools` when deciding whether to load it (see [`resident`](#resident)), and what
`list_capabilities` and `kokua --list-toolsets` print. Unset, it reads `Tools from the MCP server at
<url>.`, which says where the server is and nothing about what it does, so it is worth writing for any
server an agent defers. It must be a string.

```toml
[[mcp.server]]
url = "https://api.githubcopilot.com/mcp/"
name = "github"
token_env = "GITHUB_MCP_TOKEN"

[[mcp.server]]
url = "https://broker.example.com/mcp"
name = "stocks"
description = "Stock quotes and fundamentals, and placing trades."
```

Servers added at runtime with the `add_mcp_server` tool are appended here automatically, with a `name`
derived from the host and a numeric suffix if that name is taken, so the file always loads. Nothing can
use that server in the session that added it, since the toolset namespace and the agent tables are both
read only at startup. **After a restart** it reaches every agent declaring `available = "all"`, and any
other agent once you add its name to that agent's `available` list. A server added with a bearer token is
not appended (its token is never written to disk), so it lasts the session and nothing can name it. See
[add an MCP service](../how-to/add-mcp-services.md).

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
    payloads/            oversized sub-agent tool responses, served at /payloads
    downloads/           generated artifacts (PDFs, exported conversation Markdown), served at /download
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
`DEBUG`, `INFO`, `WARNING`, `ERROR`. Default `"INFO"`. `--debug` sets `DEBUG` for one run. At `DEBUG`,
startup also prints each agent's access summary (the lines `kokua agents` expands on) to stderr; at any
level it is written to the log.

The log records the turn lifecycle (submitted, lock acquired, done, error), so a hung turn is visible
after the fact. It is also where a dropped generation parameter reports itself, and where the MCP SDK's
own failures land: a server whose OAuth client registration fails used to print an unattributed
traceback to the terminal instead, carrying no timestamp and no server URL to say which server it came
from. At `DEBUG` that same capture includes the SDK's protocol-level chatter. For live state, the
`/diag` chat command reports turn and lock state and dumps a wedged turn's async stack, and
`kill -USR1 <pid>` dumps all thread stacks.

## `[email]`

Lets the assistant email information to you (digests, summaries, reports) over SMTP, through the
`email-report` skill. Install it with `kokua skills install email-report`, then name it in an agent's
`available` list alongside `fs` and `compute` so that agent can run its script.

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

Deep planning is a toolset: an agent's `available` must list `planning` for the `/plan <task>` command to
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

**Which model each critic runs on, and the standard it holds a plan to, are yours to set.** Both flags
above turn on a reviewer whose persona comes from [`[reviewers.<name>]`](#reviewersname):
`[reviewers.plan]` for `plan_review_agent`, `[reviewers.result]` for `result_review`. Leave either
table undeclared and planning's own shipped prompt applies, so a table there is an override rather than
something you must write to keep the default. This is the place to give a critic a stronger (or
cheaper) model than the agent it is reviewing, and the place to read the standard your plan was
rejected against.

## `[capabilities]`

Capability discovery is a toolset: an agent's `available` must list `capabilities` for the agent to see what
is installed beyond its own tools. It gets two tools. `list_capabilities` reads the registry, every
installed capability except this one, which the agent reading it already holds. `compose_subagent` builds a
sub-agent holding exactly the capabilities one task needs, and runs it.

Unlike the sub-agents in `[agents.*]`, a composed one is not declared anywhere: its capabilities are
chosen per task from everything installed, except two names it can never be given. `skills` works only
on the agent Kokua constructs directly, and `capabilities` itself would hand it a fresh
composition budget. A capability that is installed but builds no tools (`image` with no
`AIMU_IMAGE_MODEL` set, for one) is refused by name rather than given as nothing. It runs on `[assistant].model` with the `[assistant]` thinking
and generation defaults, and its tools go through the default `[assistant.tools]` policy (a composed worker is gated as
`composed:<label>`), so `execute_python` still goes to a reviewer or you and `add_mcp_server` still asks
you first. Composing itself is in neither shipped list, so it does not ask; add
`capabilities.compose_subagent` to `confirm` to gate that too.

### `max_depth`

How far composition may nest. Default `3`. Hot.

At `3` a chain reaches three sub-agents, and the last of them holds neither `compose_subagent` nor
`list_capabilities`, since a `compose_subagent` with no way to look up capability names is useless to
whatever holds it. `0` switches composing off entirely. The cap is read when `compose_subagent` is called,
so an `update_config` change applies to the next composition with no restart, though a chain already
running keeps the count it started with.

## `[github_backup]`

Declared by the `github_backup` toolset: an agent's `available` must list `github_backup` for
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

## `[compute]`

Declared by the `compute` toolset: an agent's `available` must list `compute` to get `calculate`,
`execute_python`, and `run_command`. The shipped `[agents.coder]` is the only agent that declares it.

| Key | Type | Default | Hot | Meaning |
| --- | --- | --- | --- | --- |
| `command_env_passthrough` | string | `""` | no | comma-separated environment variable names a `run_command` child may see, on top of the allowlist |

`run_command` runs a command line through `/bin/sh -c` and returns its exit code with stdout and stderr
labelled separately. A nonzero exit is reported rather than treated as a failure, since `pytest` exits 1
with the answer on stdout. The timeout is per call, defaulting to 30 seconds and clamped to 600.
Combined stdout and stderr are capped at 20,000 bytes; past that, the larger of the two streams is
trimmed first, so a talkative command loses output before a quiet one does. `run_command` also takes a
`cwd` argument; left unset, it defaults to Kokua's own process working directory, which for a
launcher-started service is wherever the launcher happened to be, not anything under `$KOKUA_HOME`.

**The child's environment is an allowlist, and this key is the opt-in half of it.** A command sees
`PATH`, `HOME`, the locale and temp variables, and `SHELL`, `TERM`, `TZ`, `USER`, `LOGNAME`. Nothing
else, API keys included, which is what stops `run_command("env")` lifting a credential into the model's
context on a machine whose launcher sources a `.env`. That default also makes `gh`, `ssh`, and
`git push` over ssh fail, which is a policy rather than a bug, so naming `SSH_AUTH_SOCK` or `GH_TOKEN`
here is how you grant one back. Whitespace around a name is ignored and empty segments are dropped, so a
trailing comma is harmless. A name that is unset in Kokua's own environment produces no variable at all
rather than an empty one.

Not hot: the value is read when an agent is built, so a change needs a restart. `run_command` is in the
shipped `[assistant.tools].review` as `compute.run_command`, so a command reaches you for approval
(or a reviewer, if auto-approval is on) before it runs, including one a sub-agent asked for, since a
worker's gated calls are judged by its own policy rather than running unattended.

**Neither execution tool is a sandbox.** They buy a hard timeout, crash isolation, and no parent
environment variables. The process-group kill only fires on timeout or on interruption: a command that
returns normally is not waited on beyond its own exit, so `run_command("./build.sh > log 2>&1 &")` can
report `exit 0` in milliseconds while the build keeps running untouched, its captured output going to a
temp directory already deleted by the time you read the answer. `run_command` also carries no memory
cap, unlike `execute_python`'s 512 MB address-space limit: that limit would break compilers and test
suites, and applying one to a shell child needs `preexec_fn`, which is neither portable nor safe
alongside threads. Neither tool confines the filesystem, the network, or process signalling: a command
runs as you, reads what you can read, and can signal Kokua's own process. The approval gate is the
control; reach for a container if you need containment.

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
| `--confirm-tools NAMES` | `[assistant.tools].confirm` only, comma-separated `toolset.tool` entries; must not name anything in `review`; an empty string clears `confirm` but leaves `review` prompting |
| `--mcp URL` | adds a server for this run, unauthenticated or OAuth |
| `--host`, `--port` | `[web].host`, `[web].port` |
| `--debug` | `[logging].level`, set to `DEBUG`; also prints the per-agent startup summary to stderr |

Introspection, which exits without running the assistant:

```bash
kokua --list-frontends    # installed front ends
kokua --list-toolsets     # every name an available list may use, grouped by provider
kokua agents              # each declared agent's availability, residency, and approval lists, from the config alone
```

## See also

- [`config.example.toml`](https://github.com/saxman/kokua/blob/main/src/kokua/config.example.toml): the same keys, one line each, and what
  `kokua config init` writes.
- [Set up a toolset](../how-to/set-up-toolsets.md): declaring `available` and `delegates_to`, and writing a
  toolset of your own.
- [Add a skill](../how-to/add-skills.md), [add an MCP service](../how-to/add-mcp-services.md).
- [Design principles](../explanation/design-principles.md): why the config layer is shaped this way,
  particularly "`config.toml` is the single source of settings" and "a capability is declared, never
  defaulted".
- [Architecture](../explanation/architecture.md): how the file is layered, parsed, and written.
