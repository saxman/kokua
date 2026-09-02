# Get it running

The one page in this catalogue with no mechanism in it: everything else assumes you have reached the
point below, a running assistant answering you in the terminal with its reasoning visible. The
[README's Install section](https://github.com/saxman/kokua#install) has the full story, including a
sibling-checkout alternative for developing Kokua and AIMU together; this page is the one path a first
run needs.

## Requirements

Python 3.11+, and a model. Every capture behind these pages talked to a local model served by
[Ollama](https://ollama.com): free, runs on your own machine, nothing to sign up for.

```bash
ollama pull qwen3.5:9b
```

Any tool-capable model works, and a hosted provider's API key is a fine alternative to running one
locally; see [the configuration reference](../reference/configuration.md#model) for pointing Kokua at
one instead.

## Install

Kokua is not on PyPI. Clone the repository and install with `uv`:

```bash
git clone https://github.com/saxman/kokua && cd kokua
uv sync --all-extras --no-sources
```

## Scaffold the config

```bash
kokua config init
```

writes `~/.kokua/config.toml`: the shipped example, one line per key, every key at its documented
default. `[assistant].model` is commented out there, and that is deliberate: unset, Kokua asks for
whichever model a running local server reports, so pulling `qwen3.5:9b` with Ollama running is enough
by itself, with nothing to edit. Open the file in an editor if you are curious; nothing below requires
changing it.

## Say hello

```bash
kokua
```

starts the assistant in the terminal. The agent that answers you there is the **entry agent**: the one
agent a person talks to directly, as opposed to the short-lived workers it can spawn for a subtask, and
the agent every tool count in this catalogue is counted for. Say hello to it, and this is what came
back, quoted exactly as the terminal printed it (`qwen3.5:9b` on Ollama, Kokua commit `34831a1`):

```text
[notice] This assistant can author and run Python/shell scripts with full access to this machine (no sandbox), and can connect to remote MCP servers and run whatever tools they expose. Only use it with a model, inputs, and MCP servers you trust.
> Say hello and tell me in one sentence what you can do.
>
[thinking] The user is asking me to say hello and briefly explain what I can do in one sentence. This is a straightforward conversational request that doesn't require any tools - I should just respond directly with a friendly greeting and a concise summary of my capabilities.
Hello! I'm your personal assistant—I can help you manage tasks, organize information, run code, schedule work, store knowledge, read/write files, search documents and conversations, delegate specialized work to sub-agents, and automate routines with skills and scheduled tasks.
```

The `[thinking]` line is not decoration. It is the model's own reasoning, printed before it answers,
because watching the loop is the point: you see what it decided and why, not just the reply it settled
on. This particular reply needed no tool, so the loop ran once; later pages show what appears when it
needs several.

## Where it lives

Everything this run touched sits under `~/.kokua`: `config.toml`, the file you just scaffolded, and a
`data/` directory holding conversation history, memory, and documents. It is plain files, readable
while Kokua is running, not a hidden store behind an API. *State you can read*, later in this
catalogue, opens that directory up; for now it is enough to know the assistant you just ran has nothing
to show you that is not sitting in a file on your own disk.

That `[thinking]` block is your first look at the mechanism the next page in this catalogue takes
apart: [the turn loop](the-turn-loop.md). See the [section index](index.md) for what has landed so
far.
