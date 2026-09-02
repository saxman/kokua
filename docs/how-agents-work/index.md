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

The mechanism pages land one at a time rather than all at once, so this list says plainly which of the
thirteen planned pages exist today. None yet: this is the first page in the section. Still to come, in
the order a newcomer would want them:

1. The turn loop
2. Tool calling
3. Capability is declared
4. Context and memory
5. Delegation
6. Agents and workflows
7. Humans in the loop
8. Watching the loop
9. Proactive work
10. Reaching outside
11. State you can read
12. When it goes wrong
