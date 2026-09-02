# How agents work

A catalogue of the mechanisms an agentic system is made of: the loop that keeps a turn going, how a
model's tool request becomes code that runs, what persists across turns, what happens when work is
handed to another agent, and the rest. Each page explains one mechanism in general terms first, with
no Kokua in it, then shows that same mechanism happening in a real captured run against Kokua, then
shows the code that did it.

The pages are readable in any order. Each assumes only [Get it running](get-it-running.md): a working
assistant and one transcript you have already seen. This section teaches the field, the mechanisms any
agentic system has; [Explanation](../explanation/index.md) is where to go for why *this* codebase made
the choices it did about them.

## Pages

- [Get it running](get-it-running.md): from nothing to a running assistant you can watch. Read this
  first, whatever order you take the rest in.
- [The turn loop](the-turn-loop.md): one turn is not one model call. What keeps it going, and what
  stops it.
- [Tool calling](tool-calling.md): the model emits a request; your code decides whether and how to run
  it.
- [Capability is declared](capability-is-declared.md): an agent holds what someone wrote down, and
  nothing a code path decided to grant it.
- [Context and memory](context-and-memory.md): the model remembers nothing, so everything it appears to
  remember was sent again.
- [Delegation](delegation.md): spending context on a subtask without spending the caller's.

The mechanism pages land one at a time rather than all at once, so this list says plainly which of the
thirteen planned pages exist today. Six of them, all listed above. The remaining seven, in the order a
newcomer would want them:

1. Agents and workflows
2. Humans in the loop
3. Watching the loop
4. Proactive work
5. Reaching outside
6. State you can read
7. When it goes wrong
