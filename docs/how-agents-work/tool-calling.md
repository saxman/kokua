# Tool calling

**The model never runs anything. It emits a request, and your code decides whether and how to run it.**

## The idea

"The model called a tool" is a convenient lie, and dropping it early makes everything downstream
easier to reason about. A model emits text. When it wants a tool, the text it emits is a *request*:
a name, and arguments as JSON. Nothing has happened yet. Some code of yours reads that request,
decides whether to honour it, runs something, and puts the outcome back into the conversation. Every
consequential decision in that sentence is yours, not the model's.

For the model to request a tool at all, it has to know the tool exists, which means you send it a
description of each one alongside the conversation. That description is a *schema*: the tool's name,
what it is for, and each parameter with its type and its meaning. Most frameworks build the schema from
the function itself, reading the signature for the parameters and their types and the docstring for the
prose, so that the thing the model reads and the thing that runs cannot drift apart. Nothing enforces
that the description is accurate. It is a promise you are making, in prose, to a reader who cannot check
it.

The request the model sends back names one of those tools and supplies arguments. Your code looks the
name up in the set of tools this agent actually holds, coerces the arguments to the declared types, and
calls the function. A name that is not in the set is not a tool call; it is a hallucination, and the
right answer is to tell the model so rather than to crash.

Then the result goes back as a *message*. This is the part that is easy to miss: a tool result is not a
return value handed to the model, because there is nothing to hand it to. It is another entry in the
conversation, in a role reserved for tool output, which the model reads on the next round exactly the
way it reads everything else. Tool output is therefore just more text in the [context
window](context-and-memory.md#the-idea), and it competes for space with everything else there.

Which is why a failing tool should usually return its error to the model rather than raise. If the
tool raises and the exception escapes the loop, the turn is over and the user gets a stack trace. If
instead the loop catches it and appends something like `Tool 'X' raised an error: unknown timezone
'Tokyo'`, the model reads that on the next round and gets to respond to it: fix the argument, try a
different tool, or tell the user it cannot do this. That single design choice is most of what people
mean when they say an agent "recovers". The failure becomes information rather than an exit.

The freedom in "whether and how to run it" is the other half. Between the request and the call, your
code can refuse, can rewrite the arguments, can ask a human first, can log it, or can run it somewhere
isolated. The model has no say in any of that, and cannot tell the difference from the inside: to it, a
denied tool is simply a tool that returned "denied".

## Watch it

The simplest possible case: one question, one tool the assistant holds directly, one round trip. Quoted
exactly as the terminal printed it ([`qwen3.5:9b` on Ollama](get-it-running.md), Kokua commit
`34831a1`):

```text
[notice] This assistant can author and run Python/shell scripts with full access to this machine (no sandbox), and can connect to remote MCP servers and run whatever tools they expose. Only use it with a model, inputs, and MCP servers you trust.
> What time is it in Tokyo right now?
> 
[thinking] The user is asking about the current time in Tokyo. I can use the get_current_date_and_time function to get this information, specifying the Tokyo timezone.

Tokyo's IANA timezone name is "Asia/Tokyo".
[tool] get_current_date_and_time(timezone='Asia/Tokyo')

[thinking] It's 09:29:26 JST (Japan Standard Time) in Tokyo on September 2, 2026.The user asked what time it is in Tokyo right now. I used the get_current_date_and_time tool with timezone Asia/Tokyo and got the result: 2026-09-02T09:29:26+09:00 (Asia/Tokyo, JST, UTC+09:00; 2026-09-02T00:29:26Z).

I have the answer and don't need to use any more tools. I should provide a clear, human-readable response.
It's currently **9:29 AM** in Tokyo on September 2, 2026 (JST, UTC+9).
```

**The request** is the `[tool]` line, and it is the whole of what the model produced at the end of round
one. `get_current_date_and_time(timezone='Asia/Tokyo')` is a rendering, not a wire format: the model
emitted a name and an arguments object, and the terminal channel formats it as a call for you to read.
No time has been looked up at this point.

Notice what the reasoning above it settles before the request goes out. The model is not asking for
"the time in Tokyo". It is filling in a parameter called `timezone`, and it commits to a value for it,
`"Asia/Tokyo"`, saying so explicitly: `Tokyo's IANA timezone name is "Asia/Tokyo"`. That parameter
exists, with that name and that type, only because the tool's signature and docstring said so. A model
cannot supply an argument the schema does not offer, and this transcript is where you can watch that
constraint doing its work. Whether the docstring's own `"Asia/Tokyo"` example is *why* it chose the IANA spelling
over the city name is not something one transcript can tell you: that is a spelling a model may well
reach for unprompted.

**The dispatch** is invisible in this transcript, and that is not a gap in the capture. The terminal
prints tool requests and never tool results. Between the two `[thinking]` blocks, code outside the model
matched the name against the tools this agent holds, coerced `timezone` to a string, called the
function, and appended what came back to the conversation.

**The result** shows up in round two's reasoning, quoted by the model out of the message it just read:
`got the result: 2026-09-02T09:29:26+09:00 (Asia/Tokyo, JST, UTC+09:00; 2026-09-02T00:29:26Z)`. That is
the tool's actual return string. Everything the final answer says that the question did not (the date,
the offset, "JST") came from that message and from nowhere else. The model then asks for nothing more,
and the turn ends after two model calls, which is [the turn loop](the-turn-loop.md) with a single round
of tools in it.

## In Kokua

A tool is a plain Python function with a docstring. Kokua's smallest complete toolset,
[`toolsets/image.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/image.py), is one
tool and the declaration that offers it, trimmed here to those two things:

```python
    @tool
    def generate_image(prompt: str):
        """Generate an image from a text prompt; the image is shown to the user and saved.

        This tool is offered only when AIMU_IMAGE_MODEL is configured, so seeing it means one is set.
        If the model it names still fails to load, this reports generation as unavailable instead of
        raising.

        Args:
            prompt: A description of the desired image.
        """
        ...
        return f"Generated image, shown to the user inline (/images/{name})."

    return [generate_image]


TOOLSET = Toolset(
    name="image",
    description="Generate images from a text prompt (needs the AIMU_IMAGE_MODEL environment variable set).",
    build=lambda ctx: build(ctx.config),
)
```

Read it as three separate contracts. The signature, `prompt: str`, becomes the parameter schema. The
docstring's first paragraph becomes the tool's description, and its `Args:` section becomes the
description of `prompt`; both are what the model reads when deciding whether this tool answers the
question in front of it. The `return` is a string because the result is going into the conversation as a
message, so it is written to be read: it tells the model the image was already shown, which stops it
from trying to show it again.

The second paragraph is worth reading twice on a page arguing that a docstring is a promise, because an
earlier version of it broke one. That version told the model that without `AIMU_IMAGE_MODEL` the tool
"reports that generation is unavailable". It never did: `build()` returns an empty list when that
variable is unset, so the tool is not in the model's list at all, and there is nothing there to report
anything. The sentence that does return "unavailable" fires only when the variable *is* set and the
client still cannot be built, the opposite condition from the one the docstring named. No test caught
it, because nothing mechanical can: the sentence was well-formed, accurate-sounding, and described a
state the model could never be in. The version above fixes it by naming the two conditions separately,
one per sentence: the tool's presence already implies the variable is set, and "unavailable" is confined
to the one path that can actually produce it. That is still a promise made in prose to a reader who
cannot check it against the code; this one just happens to hold.

The parsing is [AIMU's `@tool` decorator](https://saxman.info/aimu/how-to/add-custom-tool/), which
inspects the signature and the Google-style docstring at import time and attaches the resulting schema
to the function. Nothing in Kokua writes a schema by hand.

`TOOLSET` is how the function reaches an agent. Kokua's
[`registry/registry.py`](https://github.com/saxman/kokua/blob/main/src/kokua/registry/registry.py) keeps
one flat namespace of named capabilities; `build_tools` concatenates the functions from the toolsets an
agent declared, and `select` raises on a name no toolset provides rather than quietly handing the agent
a shorter list. So a tool exists for a given agent because a table in `config.toml` named the toolset
it lives in. Two of the entry agent's own tools arrive by another route, and both are named out loud
rather than glossed: `spawn_subagent` is earned by a non-empty `delegates_to` rather than by any
toolset, and `activate_skill` plus one tool per installed skill script are injected by AIMU's
`SkillAgent` whether the table asked for them or not. [Capability is
declared](capability-is-declared.md) is that rule, and those exceptions, in full.

One thing the transcript above makes unavoidable: `get_current_date_and_time` is not in this repository.
It is AIMU's. Kokua's entire [`time`
toolset](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/time.py) is one `Toolset`
declaration handing AIMU's clock group a name an agent can put in its `tools` list, and it defines no
tool of its own. The docstring behind that `timezone` parameter, the one that produced the schema the
model filled in, lives in `aimu.tools.builtin`:

```python
    """Returns the current date and time, with its UTC offset and timezone.

    Args:
        timezone: IANA timezone name (e.g. "Asia/Tokyo", "America/New_York") to report
            the time in. Omit for the local timezone.
    """
```

That is the general case, not an exception. Twelve of the 33 tools the shipped assistant holds are
AIMU's rather than Kokua's, more than a third, and more once skills are installed, since AIMU injects a
tool per skill script on top of that set. [How an agent's tools
resolve](../explanation/architecture.md#how-an-agents-tools-resolve) carries the full inventory, and a
test pins it as an exact set, so adding a tool to a declared toolset fails the suite until that table is
updated too.

## What it costs

**A vague docstring is a bug, and it does not look like one.** The docstring is the only thing the model
has when it decides whether your tool is the right one, so an inaccurate description produces an agent
that calls the wrong tool, or calls the right one with the wrong argument, and there is no exception and
no failing test. It surfaces as the model seeming stupid. The Tokyo transcript is the benign
case: the description was accurate, the model committed to `"Asia/Tokyo"` before the request went out,
and what came back was usable on the first try. All that bought it was one sentence in an `Args:`
block. The `generate_image` docstring's earlier version was the other kind: well-formed,
plausible, and describing a state the model could never be in. Nothing would have errored had a model
believed it. The only symptom either way is a choice the model made, which you notice only if you are
reading the reasoning that led to it.

**Every schema is re-sent every round.** The tool descriptions ride along with the conversation on each
model call, so a large tool list is a permanent tax on the context window and on every round of every
turn, whether or not any tool gets used. Granting an agent a toolset it will never call is not free, and
it also makes the choice harder for the model. This is the practical argument for
[principle 2's
corollary](../explanation/design-principles.md#corollary-a-capability-is-declared-never-defaulted), that
an agent holds exactly the capabilities its table declares.

**A returned error costs a round.** Handing the model `Tool 'X' raised an error: ...` buys recovery, and
it pays for it with a full extra round trip carrying the entire conversation. Under a cap of ten model
calls ([the turn loop](the-turn-loop.md)), a few self-corrections are the difference between an answer
and a forced wrap-up.

**Approval gates cost latency and buy control.** Kokua's `[security].confirm_tools` names the tools that
stop and ask before running, and the shipped default includes `execute_python`, `run_command`, and
`update_config`, since those run with full access to your machine. The cost is real and it is human
latency, not compute: the turn is blocked on you, and a turn nobody is watching, such as a scheduled
one, auto-denies rather than hanging forever. *Humans in the loop*, later in this catalogue, is that
mechanism in full.

## Go deeper

- [The turn loop](the-turn-loop.md): the loop this dispatch happens inside, and what the round budget
  buys.
- [Set up a toolset](../how-to/set-up-toolsets.md): writing one of these, end to end, including the
  `pyproject.toml` entry that registers it.
- [How an agent's tools resolve](../explanation/architecture.md#how-an-agents-tools-resolve): the full
  inventory of what the shipped assistant holds, and which twelve of the 33 tools come from AIMU.
- AIMU: [Add a custom tool](https://saxman.info/aimu/how-to/add-custom-tool/) for the `@tool` decorator,
  [the tools API reference](https://saxman.info/aimu/reference/api/tools/) for what the decorator
  supports, and [Use MCP tools](https://saxman.info/aimu/how-to/use-mcp-tools/) for tools that live in
  another process entirely.
