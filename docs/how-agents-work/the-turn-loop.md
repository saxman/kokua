# The turn loop

**An agent is a model, some tools, and a loop that keeps calling the model until it stops asking for
things.**

## The idea

A model call is a function over text: messages in, one message out. It looks nothing up and it runs
nothing. Whatever an agent can do that a chatbot cannot comes from the loop wrapped around that call,
not from the model inside it.

The loop is short enough to state in full. Send the conversation so far. Read what comes back. If the
reply is an answer, the turn is over. If the reply instead requests one or more tools, run them, append
each result to the conversation as a new message, and send the whole conversation again. Repeat.

So one turn is not one model call. It is as many calls as the model asks for, and the model decides how
many simply by continuing to ask. A person sees one question and one answer; underneath, that
answer may have cost four round trips, each one carrying everything that came before it.

What ends a turn, naturally, is the model not asking for anything more. That is the only stopping
condition the loop gets for free, and it depends entirely on the model choosing to stop. Models do not
always choose to stop. One can request the same search five times in a row, alternate between two tools
indefinitely, or return a reply that neither answers nor asks for anything. None of those is detectable
from a single round. They are patterns, and by the time the pattern is clear you have already paid for
it.

That is why the caller imposes a cap of its own, and why the cap is usually a count of model calls
rather than a clock. A loop like this is never hung: it is making progress it cannot use, as fast as
the hardware allows. A timeout would cut off a slow, honest, four-round answer and let a fast,
useless one run longer.

The subtle part is what happens when the cap is hit, because "stop and return nothing" converts a slow
answer into no answer at all, which is the worse of the two failures. The better move is to make one
more call with the tools taken away, so the model has no option but to write an answer out of whatever
it has already gathered. A partial answer that says what it found beats silence, and having that
fallback is also what makes it safe to set the cap low.

Two things to carry to the next page. Every round resends the entire conversation, tool results
included, so what a turn costs tracks the number of rounds far more than the length of the answer. And
nothing in this loop is the model executing anything: it emits requests, and other code decides what to
do with them. That is [Tool calling](tool-calling.md).

## Watch it

One question, asked at the terminal, that took three visible rounds of the loop to answer. Quoted
exactly as it was printed ([`qwen3.5:9b` on Ollama](get-it-running.md), Kokua commit `34831a1`):

```text
[notice] This assistant can author and run Python/shell scripts with full access to this machine (no sandbox), and can connect to remote MCP servers and run whatever tools they expose. Only use it with a model, inputs, and MCP servers you trust.
> What is 4817 times 293? Use a tool.
> 
[thinking] The user wants me to calculate 4817 × 293 using a tool. I should use the benchmark_model tool or I could use compute capability if available. Let me check what capabilities are available first by calling list_capabilities to see if there's a compute capability.

Actually, looking at the tools available, I don't see a direct math calculator tool in the tool list provided. The benchmark_model tool is for measuring speed. Let me check list_capabilities to see what compute-related capabilities exist.
[tool] list_capabilities(filter='compute')

[thinking] I'll use the compute capability to calculate 4817 times 293.
[tool] compose_subagent(name='multiply-task', task='Calculate 4817 * 293 and return the result.', tools=['compute'], instructions='Perform the calculation and provide the answer.')

[thinking] **4817 × 293 = 1,411,381**The user asked me to calculate 4817 times 293, and I already got the answer from the subagent: 1,411,381. This is a complete answer and I don't need any more tools. Let me just provide the final response.
The product of 4817 × 293 is **1,411,381**.
```

There are three `[thinking]` blocks, and that is the count that matters: each one begins a model call.
Three is a floor rather than an exact total, since a call that emitted neither reasoning nor a tool
request would leave no trace here at all.

1. **Round one.** The model is handed the question and the list of tools it holds. It reasons about
   which of them could do arithmetic, decides it cannot tell from the list alone, and asks for
   `list_capabilities(filter='compute')`. That request ends the model's part of round one. The loop runs
   the tool, appends the result, and calls again.
2. **Round two.** Now reasoning over a conversation that contains the first tool's output, the model
   asks for a second tool, `compose_subagent`. Note that it never re-derives what it learned in round
   one: the result is in the messages, so it reads rather than recalls. The loop runs this tool too, and
   calls a third time.
3. **Round three.** The model asks for nothing. It writes an answer, and the turn ends there, on the
   natural stopping condition rather than on a cap.

Two honest observations about the transcript rather than about the loop. The terminal prints the tool
*request* and never the tool *result*: the result goes into the conversation, and you see it only as the
next block's reasoning ("I already got the answer from the subagent: 1,411,381"). And the third
`[thinking]` block runs its conclusion straight into the next sentence with no space
(`**4817 × 293 = 1,411,381**The user asked me`), which is the model's own token stream printed as it
arrived, not a formatting slip in this page.

`compose_subagent` in round two is itself a whole loop of this shape running inside one tool call, on a
second agent built for the occasion. [Delegation](delegation.md) is that mechanism in full; here it is
just a tool that took a while to return.

## In Kokua

The loop itself is not in this repository. It belongs to
[AIMU's `Agent`](https://saxman.info/aimu/how-to/build-personal-assistant/), which Kokua constructs in
[`core/build.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/build.py) and drives with a
single `await agent.run(...)`. What Kokua owns is everything around that call: which conversation the
turn belongs to, what happens if two turns overlap, and what survives if one is cancelled halfway.

That code is [`core/turns.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/turns.py), and
it opens with seven concurrency invariants, each one naming the bug that taught it. Two of them:

```text
1. **One gate hold per task, taken exactly once.** ``TurnGate`` is writer-preferring: a waiting
   exclusive() blocks new readers. A task that holds a reader and then tries to take a *second*
   reader deadlocks against a concurrent exclusive(), which is waiting for the reader count to reach
   zero that the outer hold will never release. Every path below takes exactly one
   ``gate.turn(...)``, and no path calls another path that takes one. Do not wrap a call to
   ...

2. **Pin for the whole turn.** The agent registry evicts LRU. Without a pin, another conversation's
   turn can evict this one's agent mid-run, and persisting afterwards would rebuild a stale agent
   from the store and silently lose this turn's output. Pin before the gate, unpin in a ``finally``.
```

Both exist because a turn is *long*. Three model calls is seconds at best and minutes on a local model,
and during that whole window the rest of the process keeps running: another conversation can start a
turn, a scheduled task can fire, the user can hit `/stop`. Every one of those is a chance to corrupt a
turn that is not finished yet, which is why the rules are written at the top of the file rather than
inferred from the code. [Design principle 5](../explanation/design-principles.md#5-a-single-user-one-process-with-concurrency-rules-written-down)
is the commitment to keep them there.

The turn path itself, with the workflow branch and the error handling trimmed away:

```python
    async def reactive(
        self, msg: ChannelMessage, *, conversation_id: str, workflow=None, tid: Optional[int] = None
    ) -> None:
        """Run a user-initiated turn and send its reply. See the module's concurrency invariants."""
        started = time.monotonic()
        agent = self._book.agent_for(conversation_id)
        ...
        self._book.pin(conversation_id)  # invariant 2
        token = streaming_conversation.set(conversation_id)  # invariant 3
        ...
        try:
            async with self._gate.turn(conversation_id):  # invariant 1
                ...
                            stream = await agent.run(msg.text, stream=True, images=msg.images, thinking=thinking)
                            await self._ui.send(stream, reply_to=msg)
```

Every round in the transcript above happened inside that one `agent.run(...)` line, and the chunks the
three `[thinking]` blocks were printed from arrived through the stream it returns, as the loop produced
them. Nothing waits for the turn to finish before you can watch it.

The turn does not block the front end either. `_serve_channel` in
[`core/assistant.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/assistant.py) starts each
one as a background task and immediately goes back to reading input:

```python
                handle = RunHandle.start(self._handle(msg, conversation_id=conversation_id, workflow=workflow, tid=tid))
```

That single line is what makes `/stop` possible. The channel is still reading while the loop runs, so
`/stop` is read, matched, and turned into `handle.cancel()` on a turn that is mid-round, which is
[AIMU's `RunHandle`](https://saxman.info/aimu/how-to/cancel-a-run/) doing the work. What you already
read is not lost when that lands: the agent snapshots the partial turn in a `finally` of its own, and
`turns.py`'s cancelled branch persists that snapshot before returning, so stopping a turn costs you the
rest of the answer and not the part that had already arrived. (Invariant 5, which the same branch also
obeys, is about a different thing: recording the turn's sub-agent events *before* the "(stopped)"
notice, since a second cancellation racing that send would propagate straight past a record placed
after it.)

## What it costs

**Rounds, not words.** Every round is a full request carrying the entire conversation so far: the system
message, every previous message, every tool result, and the schema of every tool the agent holds. The
turn above sent three of those and got back one sentence. Doubling the length of an answer is close to
free; adding one more round is not. It is why the same question costs more of an agent than of a
chatbot, and it is also why the [next page's](tool-calling.md) point about tool schemas matters: they
are re-sent every round too.

**The degenerate turn.** The named failure is a model that keeps calling tools without converging.
Kokua does not implement the defence itself; AIMU's `Agent` does, in three graded steps. A turn that
comes back with neither content nor a tool call gets a continuation nudge rather than being treated
as an answer, and gets one for each such turn, bounded by the same cap. Exhausting the cap with a
tool call still pending triggers a single forced wrap-up call with tools disabled, which is the
"answer from what you have" move described above, and it is deliberately not counted against the
cap. If even the wrap-up produces nothing, AIMU raises `DegenerateTurnError` instead of returning an
empty string, so the failure reaches you as a failure.

**The cap you are running.** AIMU's default is ten model calls per turn, and Kokua does not override it
for the entry agent, so ten is what the transcript above was running under (it used three). The one
place Kokua sets it deliberately is the plan workflow's independent reviewer,
[`workflows/critics.py`](https://github.com/saxman/kokua/blob/main/src/kokua/workflows/critics.py), at
`max_iterations=6` to bound what verification can spend.

**What hitting it feels like.** Not an error. A search-heavy sub-agent that spends all ten rounds
gathering hands back the wrap-up call's summary, which is thinner than the answer it was building
toward. That seam is marked now, though where it shows up depends on whose round it was. When the
*entry* agent's own turn hits the cap, its chunks go to the channel directly, and AIMU's own
`CLIChannel` prints the injected round (`[continuing: final_answer] <prompt>`), which Kokua's terminal
channel inherits with no code of its own. A *worker's* rounds never reach that method: they arrive as
`subagent` card entries by way of `ChannelUI.show_subagent`, and the terminal offers no frame for those,
so the call is a documented no-op there. A worker's boundary is drawn in the web UI's sub-agent card
instead, carrying which injection it was and the exact words the worker was given (see
[Delegation](delegation.md)). Either way the record says why the answer is thinner rather than reading
as one that simply ran short.
That edge is real enough to have moved this project's AIMU floor: before AIMU 0.26.0, hitting the cap
with a call still pending produced a provider rejection instead of a wrap-up, and Kokua saw it as
sub-agents failing rather than answering. The whole story is in [the architecture doc's account of the
AIMU version floor](../explanation/architecture.md#generation-parameters).

**Knowing what it actually cost.** Kokua accumulates each turn's model calls, seconds, and tokens into a
record stored with the conversation
([`core/metrics.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/metrics.py)), described in
[What a turn cost](../explanation/architecture.md#what-a-turn-cost). Rounds are the thing to watch,
and Kokua counts them for you rather than asking you to estimate.

## Go deeper

- [Tool calling](tool-calling.md): what one of those `[tool]` lines actually is, and who runs it.
- [Delegation](delegation.md): what the `compose_subagent` call in round two started, and why a subtask
  gets a loop and a context of its own rather than more rounds of this one.
- [Architecture: the core](../explanation/architecture.md#the-core), for how `Assistant`, `TurnRunner`,
  and `ConversationBook` divide this work, and
  [What a turn cost](../explanation/architecture.md#what-a-turn-cost) for the metrics record.
- [Design principles: a single user, one process, with concurrency rules written
  down](../explanation/design-principles.md#5-a-single-user-one-process-with-concurrency-rules-written-down),
  on why the invariants are written down at all.
- AIMU: [Build a personal assistant](https://saxman.info/aimu/how-to/build-personal-assistant/) for the
  loop itself, [Cancel a run](https://saxman.info/aimu/how-to/cancel-a-run/) for what `/stop` reaches,
  and [Observe a run](https://saxman.info/aimu/how-to/observe-a-run/) for the events the metrics record
  is built from.
