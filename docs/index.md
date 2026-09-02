<div align="center" markdown>

![Kokua](assets/kokua-horizontal-light.svg#only-light){ width="320" }
![Kokua](assets/kokua-horizontal-dark.svg#only-dark){ width="320" }

</div>

# Kokua

**Kokua** (Hawaiian: *help, assistance*) exists so people can learn how agentic systems work. It is a
hackable, modular personal assistant built on the [AIMU](https://saxman.info/aimu/) library, and a real
one rather than a demo: an always-on assistant that chats with you, authors and runs its own skills,
connects to remote tool services, delegates independent subtasks to isolated sub-agents, schedules its
own proactive work, and remembers facts and documents across conversations.

A toy cannot teach what real work costs, so Kokua does real work. And the machinery is meant to be
followed rather than taken on faith, so every mechanism above is there to be read, run, and extended.

Kokua isn't on PyPI; clone the repository and install with `uv` (Python 3.11+ and
[AIMU](https://saxman.info/aimu/) 0.27.0 or newer):

```bash
git clone https://github.com/saxman/kokua && cd kokua
uv sync --all-extras --no-sources  # AIMU from PyPI; see the README for the sibling-checkout alternative
kokua config init                  # scaffold ~/.kokua/config.toml, every key documented
kokua                              # chat in the terminal
kokua --frontend web               # or a browser UI at http://127.0.0.1:8000
```

See the [README's Install section](https://github.com/saxman/kokua#install) for the full walkthrough,
including why `--no-sources` is there.

Kokua runs as a single user in a single process, and can run code and reach remote services with your
privileges. See [security](https://github.com/saxman/kokua#security).

## Three ways in

### Read it

The core is small enough to hold in your head, and each module opens by saying why it is shaped the way
it is rather than restating what it does. A reading order:

| Start here | What it teaches |
| --- | --- |
| [core/assistant.py](https://github.com/saxman/kokua/blob/main/src/kokua/core/assistant.py) | The composition root and the serve loop: which AIMU primitives an assistant is made of, and how they are wired. |
| [core/turns.py](https://github.com/saxman/kokua/blob/main/src/kokua/core/turns.py) | What one turn is, reactive and proactive. It opens with seven concurrency invariants, each naming the bug it prevents. |
| [registry/registry.py](https://github.com/saxman/kokua/blob/main/src/kokua/registry/registry.py), then [toolsets/image.py](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/image.py) | How a capability becomes a tool an agent holds: one flat namespace, then the smallest complete toolset in the repository. |
| [channels/ui.py](https://github.com/saxman/kokua/blob/main/src/kokua/channels/ui.py) | How a core that knows no transport still renders richly. |
| [workflows/planning/runner.py](https://github.com/saxman/kokua/blob/main/src/kokua/workflows/planning/runner.py) | An agentic loop with more structure than chat: draft a plan, review it, execute it, review the result. |

### Run it

Reasoning, tool calls, tool results, sub-agent cards, and plan phases are visible by default. You watch
the loop instead of inferring it: what the model was thinking, which tool it chose, what arguments it
passed, what came back, and what it did next. The entire state of a running assistant is plain files
under `~/.kokua`, readable while it runs.

### Extend it

Capability arrives through the same seam Kokua's own capabilities use, so the code you read is the code
you would write. Start with [set up a toolset](how-to/set-up-toolsets.md).

## Where to go

- **[How agents work](how-agents-work/index.md)**: new to agentic systems? Start here. A catalogue of
  the mechanisms one is made of, each shown happening in a real run against Kokua before the code
  behind it.
- **[How-to guides](how-to/index.md)**: task-oriented recipes for giving Kokua new capability.
- **[Reference](reference/index.md)**: every `config.toml` key, exhaustively.
- **[Explanation](explanation/index.md)**: the architecture, and the six principles that decide what
  belongs in the core.
- **[AIMU's documentation](https://saxman.info/aimu/)**: Kokua is a thin application over AIMU, so most
  capability questions are really AIMU questions.
