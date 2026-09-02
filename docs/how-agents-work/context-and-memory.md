# Context and memory

**A model remembers nothing between calls, so everything it appears to remember is something your code
sent it again.**

## The idea

A model call is a function over text. Nothing about the previous call survives it: no variables, no
notes, no sense of who it was talking to. Continuity is entirely an illusion produced by resending, and
the resending is done by code you wrote. This is the most useful thing to internalize about agents,
because every question about memory turns into a question about what your code chose to put in the next
request.

What goes into that request is the **context window**: a fixed budget, measured in tokens, shared by
the system message, the description of every tool the agent holds, every message of the conversation so
far, and every tool result appended along the way. It is also shared with the answer, since generated
tokens come out of the same budget. The window is the whole of what the model can see, and it is the
same size on the tenth round as on the first.

That fixed size against a conversation that only grows is the tension the rest of this page is about.
There are two different mechanisms for dealing with it, and conflating them is where most confusion
starts.

**Context is what you resend, verbatim, every round.** Fidelity is perfect and there is no retrieval
step to get wrong: if it is in the messages, the model is looking at it. It costs the full token price
of everything it contains on every single round, and it ends when the conversation ends.

**Storage outside the conversation is written by one tool call and read back by another.** It is
unbounded, it survives the conversation that created it, and it costs almost nothing when unused,
because only what a search returns enters the window. What it costs instead is a retrieval step that
can miss, and a decision about what was worth writing down in the first place.

Their failure modes are different in kind, which is the practical reason to keep them separate in your
head. Context fails by arithmetic: it fills, and then something must be dropped, summarized, or
refused. That failure is predictable and, in principle, calculable in advance. Storage fails by
judgment: nothing wrote the fact down, or the search did not surface it. A fact that was never stored
and a fact the search missed look identical from the outside, and neither raises anything.

Notice who is making the judgment. In most systems, storing is a tool the model calls when it decides
something is worth keeping, prompted by an instruction in its system message. "Remember this" is
therefore a request, not a command. And the retrieval is usually a similarity search, so recall depends
on the words the model happens to search with resembling the words it happened to store.

## Watch it

Two runs. In the first, a fact is stored. In the second, a different conversation with no history at
all recalls it. Quoted exactly as the terminal printed them ([`qwen3.5:9b` on
Ollama](get-it-running.md), Kokua commit `34831a1`).

```text
[notice] This assistant can author and run Python/shell scripts with full access to this machine (no sandbox), and can connect to remote MCP servers and run whatever tools they expose. Only use it with a model, inputs, and MCP servers you trust.
> Remember that my favourite colour is teal.
> 
[thinking] The user is sharing a personal preference - their favorite colour is teal. This is something worth remembering in memory for later retrieval when it might be helpful. I should call store_memory to save this fact.
[tool] store_memory(content="User's favourite colour is teal.")

[thinking] I've stored that information about the user's favorite color being teal.The user asked me to remember their favourite colour is teal, and I've successfully stored that information in memory. The task is complete - I should confirm this to the user.
I've remembered that your favourite colour is teal.
```

**Between the two runs, plainly.** This is not a sequence you can type. Kokua's terminal front end has
no `/new` command: starting a fresh conversation is an action in the web UI's sidebar, and a `/new`
typed at the prompt is sent to the model as an ordinary chat message in the same conversation.
Restarting the process does not help either, because Kokua adopts the most recently updated
conversation on launch and resumes it. So the capture was made by running two separate `kokua`
processes and moving `$KOKUA_HOME/data/sessions.json` aside in between, leaving
`$KOKUA_HOME/data/memory` untouched. The second process found no conversation to adopt and started
genuinely empty.

That intervention is not a workaround around the mechanism. It *is* the mechanism, in the crudest
possible form: the conversation and the fact store are different files with different lifetimes, and
deleting one leaves the other standing.

```text
[notice] This assistant can author and run Python/shell scripts with full access to this machine (no sandbox), and can connect to remote MCP servers and run whatever tools they expose. Only use it with a model, inputs, and MCP servers you trust.
> What is my favourite colour?
> 
[thinking] The user is asking about their favourite colour. This would be personal information that should be stored in memory if they've shared it before, or I should search my memories to see if it's been mentioned. Let me search for this information first.
[tool] search_memories(query='favourite colour', n_results=5)

[thinking] The memory shows that the user's favourite colour is teal.
Your favourite colour is teal.
```

The second run's reasoning is the evidence that this is real rather than a transcript trick. The model
does not answer from what it can see; it says it should check, calls `search_memories(query='favourite
colour', n_results=5)`, and reads the answer out of the result. Its context contained one user message
and nothing else. Everything else came back through a tool.

Where the fact actually went, in a third run of the same first prompt against a fresh `$KOKUA_HOME`
(hence the differently spelled `favorite`), followed by a plain shell session against that same
directory. The bracketed paragraph in the middle is the capture author's annotation, not terminal
output; everything else is:

```text
[notice] This assistant can author and run Python/shell scripts with full access to this machine (no sandbox), and can connect to remote MCP servers and run whatever tools they expose. Only use it with a model, inputs, and MCP servers you trust.
> Remember that my favourite colour is teal.
>
[thinking] The user wants me to remember that their favorite color is teal. This is a personal preference worth remembering using store_memory.
[tool] store_memory(content="User's favorite colour is teal.")
Got it! I've remembered that your favourite colour is teal. 🟦

$ find "$KOKUA_HOME/data/memory" -maxdepth 2
/tmp/kokua-docs-home/data/memory
/tmp/kokua-docs-home/data/memory/chroma.sqlite3
/tmp/kokua-docs-home/data/memory/3402d950-ecf0-466c-9dea-6241cc59388b
/tmp/kokua-docs-home/data/memory/3402d950-ecf0-466c-9dea-6241cc59388b/data_level0.bin
/tmp/kokua-docs-home/data/memory/3402d950-ecf0-466c-9dea-6241cc59388b/length.bin
/tmp/kokua-docs-home/data/memory/3402d950-ecf0-466c-9dea-6241cc59388b/link_lists.bin
/tmp/kokua-docs-home/data/memory/3402d950-ecf0-466c-9dea-6241cc59388b/header.bin

$ cat "$KOKUA_HOME/data/memory/chroma.sqlite3"
SQLite format 3
(binary; not readable with cat -- this is a Chroma vector store, not a plain text file: one
SQLite database of metadata plus an HNSW index directory of raw float vectors, per collection.
The store_memory call above created the numbered directory and wrote a new row for the fact.)

$ sqlite3 "$KOKUA_HOME/data/memory/chroma.sqlite3" \
    "SELECT * FROM embedding_metadata WHERE string_value LIKE '%teal%';"
1|chroma:document|User's favorite colour is teal.|||

$ sqlite3 "$KOKUA_HOME/data/memory/chroma.sqlite3" \
    "SELECT * FROM embedding_fulltext_search_content;"
1|User's favorite colour is teal.
```

`cat` fails because the fact is not in a text file. `$KOKUA_HOME/data/memory` is a Chroma vector store:
one SQLite database holding the text and its metadata, plus a directory per collection of raw
HNSW index binaries, which is what makes a similarity search fast. The `store_memory` call created that
numbered directory; the two `sqlite3` queries pull the stored string back out, byte for byte as the
model wrote it.

## In Kokua

**The conversation.** Each conversation owns its own agent and its own model client, and the client
holds the message list. Persisting a conversation is a snapshot of exactly that list, in
[`core/conversations.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/conversations.py):

```python
        session.messages = compact_message_images(
            [dict(message) for message in agent.model_client.messages], self._config.images_path
        )
```

And rebuilding one, in
[`core/build.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/build.py), is the same move
in reverse:

```python
        agent = wire_agent(config, state, config.entry_agent, client=client_factory(conversation_id))
        session = store.get(conversation_id)
        if session is not None and session.messages:
            agent.restore(expand_message_images(session.messages, images_path))
```

That is the claim at the top of this page as running code. The agent cache evicts least-recently-used,
and a restart discards everything in memory, so an agent you come back to is routinely a *fresh* client
handed a list of messages read off disk. Nothing in it remembers the conversation. It is being told the
conversation again.

The two `compact` / `expand` calls are the same point at a smaller scale. AIMU inlines an attached image
into message content as a base64 data URL, which would bloat `sessions.json`, so
[`core/messages.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/messages.py) rewrites those
to short `/images/<name>` references on the way to disk and re-inlines the actual bytes before every
restore, because a localhost URL is not something the provider can fetch. What the model sees is
assembled for each request, never simply recalled.

**The stores.** Memory and documents are two toolsets that define no tools of their own. Each names an
AIMU store and binds it to the one instance this process opened:

```python
TOOLSET = Toolset(
    name="memory",
    description="Facts about the user, remembered across conversations.",
    build=lambda ctx: make_memory_tools(ctx.state.memory_store),
    guidance=GUIDANCE,
    cross_cutting=True,
)
```

Three things travel with that declaration.
[`toolsets/memory.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/memory.py)'s
`GUIDANCE` is the sentence appended to the system message of any agent that declares it, telling the
model to call `store_memory` for a durable fact and `search_memories` to recall one: the transcript
above is that sentence working. `ctx.state.memory_store` is a lazy property, so declaring the toolset is
what opens the store at all, and no agent declaring `memory` means no store on disk, with no flag able
to disagree ([capability is declared](capability-is-declared.md)). And `cross_cutting=True` marks it as
something an agent holds to manage itself rather than to do domain work.

[`toolsets/documents.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/documents.py) is
the same five lines over AIMU's document store, and it is worth knowing why there are two. Memory holds
short facts, retrieved by similarity. Documents hold whole texts under paths, and the folder is one you
can drop files into yourself, which is why that toolset's guidance runs to four sentences telling the
model to call `list_documents` before assuming the store is empty: asked about papers the user had just
copied in, an earlier version reported an empty store while the files sat there.

**On disk.** Everything lives under `$KOKUA_HOME` (default `~/.kokua`), and every leaf is a derived
property on the config rather than a new path function: `data/sessions.json`, `data/memory/`,
`data/documents/`, `data/images/`. [Principle
4](../explanation/design-principles.md#4-all-state-under-one-directory-the-user-owns) is why.

Kokua's README says that state is "plain files under `~/.kokua`, so you can read what it remembers while
it is still running", and the transcript above is the honest reading of that. `sessions.json` is JSON.
`documents/` is a folder of UTF-8 text files. `data/memory/` is not a plain file: it is a SQLite
database plus binary index directories, and `cat` on it prints `SQLite format 3` and then garbage.
Local, inspectable, and yours, all three of which are the point, and `sqlite3` is the tool rather than
`cat`. It does not mean readable in a text editor, and this page would rather say so than repeat the
friendlier sentence.

## What it costs

**The arithmetic is not abstract, and the numbers are in this repository.** A locally served model is
commonly given a 32768-token window. Set `[assistant.generation] max_tokens = 4096` and roughly 28k
remains for the system prompt, the tool block, and the entire conversation. The shipped entry agent
resolves to 31 tools, each contributing its name, description, and a described parameter list on
*every* round of *every* turn ([Tool calling](tool-calling.md)), before a single word of conversation.
That block is a fixed subtraction from the budget, and a long working session on a real task is how you
spend what is left.

**What overflow looks like depends on the backend, which is the nasty part.** `context_length` is
applied per request only on Ollama's native API; everywhere else the window is fixed at model load, at
server launch (`--ctx-size`, `--max-model-len`), or by the vendor, and the key is dropped with a warning
that goes to the rotating log at `data/logs/kokua.log` and nowhere else. So the symptom is not
reliably an exception. A server may silently drop the oldest messages, in which case the assistant
simply stops knowing how the conversation started, mid-task, with no marker; a provider may reject the
request, in which case the turn fails outright; and a turn may be cut short with the answer half
written. That last one only became visible everywhere recently: AIMU 0.27.0 made every provider report
how a turn ended, so a truncation raises outside Ollama for the first time rather than returning a
plausible-looking short answer.

**The store has its own failure, and it is quieter.** `search_memories` is a similarity search over
stored strings. "What is my favourite colour" found "User's favourite colour is teal" because the words
line up; "what should I paint the shed" has no such guarantee. A miss and a fact that was never stored
produce the same output, `No relevant memories found.`, so you cannot tell from the answer which one
happened. And storing is a judgment the model makes: it decided the colour was durable, and it decides
the same way about everything else you say.

**There is no delete.** The three memory tools the shipped assistant holds are `store_memory`,
`search_memories`, and `list_memories`. A fact stored wrongly is shared across every conversation and
every agent that declares the toolset, forever, and the assistant has no tool to remove it. Fixing one
means opening the store yourself, which is the moment the `sqlite3` line above stops being trivia.

## Go deeper

- [The turn loop](the-turn-loop.md): why the whole conversation is resent on every round rather than
  once per turn.
- [Tool calling](tool-calling.md): tool results are messages, so tool output competes for the same
  window.
- [Capability is declared](capability-is-declared.md): why declaring `memory` is what opens the store,
  and why the tool block is the size it is.
- [Architecture: state](../explanation/architecture.md#state) and [Design principles: all state under
  one directory you own](../explanation/design-principles.md#4-all-state-under-one-directory-the-user-owns).
- [Configuration reference](../reference/configuration.md): `context_length`, `max_tokens`, and which
  backends honour them.
- AIMU: [Use semantic memory](https://saxman.info/aimu/how-to/use-semantic-memory/), [Use document
  memory](https://saxman.info/aimu/how-to/use-document-memory/), [Manage
  context](https://saxman.info/aimu/how-to/manage-context/), and [Set context
  length](https://saxman.info/aimu/how-to/set-context-length/).
