# Delegation

**A sub-agent is a way to spend context on a subtask without spending the caller's.**

## The idea

Delegation is a tool call whose implementation is another agent's entire turn loop. The caller emits an
ordinary request, code outside the model builds or looks up a second agent, that agent runs its own
rounds against its own tools, and its final answer is appended to the caller's conversation as one tool
result. From the caller's side nothing is unusual: a tool was called and a string came back.

What makes it worth doing is what happens to the rounds in between. A worker can read ten pages, run
four searches, and get two things wrong before correcting them, and the caller pays only for the
paragraph it returns. Everything the worker looked at was in the worker's window and never entered the
caller's. That is the trade in one sentence: you buy isolation of context, and you pay for it in tokens
spent somewhere you cannot see.

Isolation cuts both ways, and the second direction is the one that bites. A worker starts with an empty
conversation. It does not know who you are, what was decided three turns ago, what "the file we were
looking at" refers to, or what the caller already tried. It has its own instructions, its own tools, and
the task string. Nothing else crosses.

So the interface is two pieces of text and there is no third. The task going in has to be
self-contained, because anything the caller knew and did not write down is simply absent. The answer
coming back is a message, which means it arrives with no provenance: the caller cannot tell a
well-grounded answer from a fluent invention, and neither can you unless the run itself is shown.

The other real gain is specialization. A worker with four tools chooses better than one with thirty,
and its system message can be written for one job rather than for every job. And because independent
subtasks have no shared state by construction, several can run concurrently, which a single agent's
sequential rounds cannot do.

Three cases where delegating is the wrong call. When the task depends on the caller's context and the
caller cannot write it all down, the worker guesses. When the task is small, the round trip (a spawn, a
worker's several model calls, a relay) costs more than answering would have. And when the caller cannot
verify the answer more cheaply than it could have produced it, delegation has not saved work, it has
only moved it somewhere unaudited and added a relay that sounds confident.

## Watch it

One prompt, one delegation, quoted exactly as the terminal printed it ([`qwen3.5:9b` on
Ollama](get-it-running.md), Kokua commit `34831a1`):

```text
[notice] This assistant can author and run Python/shell scripts with full access to this machine (no sandbox), and can connect to remote MCP servers and run whatever tools they expose. Only use it with a model, inputs, and MCP servers you trust.
> Delegate this to a sub-agent: summarise what a tool call is in three sentences.
> 
[thinking] The user wants me to delegate a task to a sub-agent. The task is to "summarise what a tool call is in three sentences". 

Looking at the available sub-agent types, I have:
- coder — Coding specialist: read/write files and run code to complete a task.
- researcher — Research specialist: gather and verify information from the web.

This task is about explaining a technical concept (tool calls) in a concise way. This seems like it could be handled by either sub-agent, but since it's more of an explanation/teaching task rather than coding or web research, I'll use the researcher agent as they might have better knowledge about tools and concepts.

Let me call spawn_subagent with:
- agent_type: "researcher"
- task: "summarise what a tool call is in three sentences"
[tool] spawn_subagent(agent_type='researcher', task='summarise what a tool call is in three sentences')

[thinking] The sub-agent has completed the task and provided a concise three-sentence summary of what a tool call is. I'll relay this answer back to the user.
Here's the summary from the sub-agent:

A tool call is when an AI assistant invokes a function from its available toolset to perform a specific action or retrieve information. It involves sending parameters such as location names, URLs, or search queries to execute functions like weather checks, web searches, or Wikipedia lookups. The results returned from the tool call are then used to construct the final response provided to the user.
```

**The whole delegation is one line.** `[tool] spawn_subagent(agent_type='researcher', task='summarise
what a tool call is in three sentences')`, and then a blank line, and then the caller reasoning about a
result that is not shown. The worker's own rounds happened between those two `[thinking]` blocks, and
none of them are in this transcript. That is what isolation looks like from outside: a whole agent's
work, rendered as a tool call.

Kokua *does* have a richer rendering, and the terminal is not it. A sub-agent card, which opens when
the worker starts and fills in with its reasoning, its tool calls, and its answer, is a frame the web UI
draws. It also fills in with the boundary where the loop restarted the worker on its own rather than the
worker's own model turn asking to continue: a nudge after an empty turn, or the forced wrap-up at the
round cap, each entry carrying which one it was (`reason`, `"continuation"` or `"final_answer"`) and the
exact words the worker was given (`text`), so a thinner answer near the cap reads as an explained outcome
rather than a mystery.
`ChannelUI.show_subagent` degrades to nothing where cards are not rendered, which is
[principle 1](../explanation/design-principles.md#1-a-small-transport-agnostic-core) working as
designed and is also why this page cannot show you one from a CLI capture.

**The choice was made from one line of prose per role.** The model's reasoning lists what it can
delegate to, and those two lines are verbatim the `description` fields of `[agents.coder]` and
`[agents.researcher]` in the config file. (This run was captured before the shipped config grew a third
delegate, `introspector`; your own run of the same prompt lists three roles rather than two, and the point
below holds for all of them.) Watch it reason honestly and still get stuck: `This seems
like it could be handled by either sub-agent`. Neither role fits a request to summarize a concept, and
there is no "none of these" option, so it picked one and justified it afterwards. A role menu always
returns a role.

**And the answer came back with nothing behind it but the model's own training.** The three sentences
are correct, and it is worth saying so plainly: a tool call really is invoking a function from an
available toolset, and weather checks and Wikipedia lookups really are examples of one. The worker held
`web`, `misc`, and `time` and used none of them. It was handed nine words of task and no context at
all, and it answered from the only thing it had, which for this question was enough.

Now notice what the caller could tell about that, which is nothing. `Here's the summary from the
sub-agent:` relays a string. Nothing in what came back says whether anything was looked up, whether a
tool ran, or what the answer rests on. This run is harmless because a question about a general concept
needs no grounding, so a model answering from training data is exactly right. The interface does not
know that. Ask a worker what a page says today, or what a file on this machine contains, and a model
with no context still returns three fluent sentences in the same register, and the relay looks
identical from here.

It is also a task that never needed a sub-agent. A three-sentence summary is the case where the round
trip costs more than the answer, and both of that trip's ends are visible above.

## In Kokua

There are two routes, and the difference between them is the whole design.

**`delegates_to` names declared agents.** A non-empty list earns the agent
`spawn_subagent(agent_type, task)` over exactly the agents it names, and each of those is a full
`[agents.<name>]` table with its own tools, model, reasoning effort, and system message:

```toml
[agents.assistant]
...
delegates_to = ["researcher", "coder", "introspector"]

[agents.researcher]
description = "Research specialist: gather and verify information from the web."
tools = ["web", "misc", "time"]
```

Nothing else grants that tool. The delegate is built in
[`core/agents.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/agents.py), and every
argument in it is a decision worth reading:

```python
    return make_async_subagent_tool(
        config.default_model,
        agent_types=build_agent_specs(config, state, delegator),
        tool_approval=state.tool_approval,
        observer=observer,
        events=record_event,
    )
```

`config.default_model`, not the caller's own client, so a worker declaring no model runs on the
configured default rather than inheriting whatever the delegator was pinned to. `tool_approval` is the
same human gate the caller's own tools pass through, carried across the boundary: a worker's
`execute_python` is routed to you, not silently allowed. `events=record_event` is what makes the
worker's model calls count into the delegating turn's cost record, which is newer than it sounds and is
one of the capabilities Kokua's AIMU floor exists to guarantee.

`build_agent_specs` builds one spec per target, and the first line of each is load-bearing:

```python
        specs[name] = {
            "system_message": f"{agent.description or name}\n\n{message}",
            "tools": tools,
        }
```

AIMU reads a spec's first line as that role's menu label, which is the line the transcript above shows
the model choosing between. Change a `description` in `config.toml` and you change how delegation is
routed. Nesting is Kokua's own rather than AIMU's: a target that itself delegates gets its own spawn
tool injected into its spec, and AIMU is called with `max_depth=1` at each level, which is why
`validate_agents` proving the graph acyclic is a startup error and not a style complaint.

**`capabilities` composes a worker instead of naming one.** An agent declaring that toolset holds
`list_capabilities` and `compose_subagent(name, task, tools, instructions)`
([`toolsets/capabilities.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/capabilities.py)),
which resolves a worker's tools from names the model picked out of the registry. This is the documented
exception to [a capability is declared, never
defaulted](../explanation/design-principles.md#corollary-a-capability-is-declared-never-defaulted), and
it is bounded on four sides. Only an agent whose own table names `capabilities` holds the tool, so the
exception is entered by declaration. The worker is constructed per call and dies with the call, so what
widens is one task's reach and never a persistent agent's. It may not be handed `capabilities` itself,
since a worker holding a fresh copy would read the depth budget from scratch instead of spending down
the caller's, so `[capabilities].max_depth` (default 3, `0` off) is what governs nesting. And its calls
route through `[security].confirm_tools` exactly as a declared worker's do.

That is the sense in which a composed sub-agent is not a config-described agent. There is no table to
read, no `wire_agent` call, and no entry in `[agents.*]`: its label is even prefixed `composed:` so it
can never collide with an agent name, and every per-agent setting it asks for answers with the
`[assistant]` defaults, because it names no table to answer from.

## What it costs

**A vague delegation buys a confident answer, grounded or not.** The worker cannot see what the caller
knows, so anything the caller failed to write into the task string is simply absent, and a model handed
an under-specified task does not stop and ask. It answers in the register you requested, from whatever
it has. When what it has is enough, as in the transcript above, the answer is right and the shortfall
costs nothing. When it is not, the reply does not change shape: same length, same fluency, same absence
of hedging, and the delegating model has no basis for doubt, so it relays. The failure mode's defining
property is that its good case and its bad case are indistinguishable at the interface, which is why
the fix is not to trust the relay harder but to add an independent check: Kokua's plan workflow uses a separate reviewer
agent ([`workflows/critics.py`](https://github.com/saxman/kokua/blob/main/src/kokua/workflows/critics.py))
rather than asking the delegator to grade what came back.

**Token spend multiplies, and the caller's transcript hides it.** The worker runs its own loop under
its own cap, resending its own growing conversation plus its own tool schemas on each of its rounds.
One `spawn_subagent` line can cost more than every round of the caller's turn put together. Kokua
records it (`events=record_event` above, feeding [what a turn
cost](../explanation/architecture.md#what-a-turn-cost)), which is worth knowing precisely because the
terminal gives you one line to look at.

**Concurrency is opt-in, and serial otherwise.** `[assistant].concurrent_tools = true` in the shipped
config is what lets several independent `spawn_subagent` calls overlap. Without it, three delegations
are three waits end to end.

**The approval gate crosses the boundary, including when nobody is there.** A worker's gated call is
routed to you, so delegation cannot launder capability past `confirm_tools`. The corollary is that an
unattended turn, such as a scheduled one, auto-denies those calls, so a delegation that works when you
are watching can come back empty-handed at 3am with no error.

**And the depth question is a real one.** A worker that delegates in turn multiplies all of the above
again, one level down, where you are two removes from the transcript. That is what
`[capabilities].max_depth` and the acyclicity check exist to bound.

## Go deeper

- [The turn loop](the-turn-loop.md): the loop the worker runs, and the cap it runs under.
- [Capability is declared](capability-is-declared.md): what `delegates_to` and `tools` each grant, and
  the exception `compose_subagent` is.
- [Architecture: agents and delegation](../explanation/architecture.md#agents-and-delegation): the
  build path, the recursion, and why a runtime MCP change rebuilds the delegate.
- [Configuration reference](../reference/configuration.md): `[agents.*]`, `delegates_to`, and
  `[capabilities].max_depth`.
- AIMU: [Spawn sub-agents](https://saxman.info/aimu/how-to/spawn-subagents/) for the machinery behind
  `make_async_subagent_tool`, [Agents versus
  workflows](https://saxman.info/aimu/explanation/agents-vs-workflows/) for when neither is the right
  shape, and [Gate tool calls](https://saxman.info/aimu/how-to/gate-tool-calls/) for the approval hook
  a worker's calls travel through.
