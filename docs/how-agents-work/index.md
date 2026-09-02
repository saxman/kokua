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
newcomer would want them, under the titles they will land with:

1. Agents and workflows
2. Humans in the loop
3. Watching the loop
4. Proactive work
5. Reaching outside
6. State you can read
7. When it goes wrong

Those titles are settled rather than provisional, because the written pages already name some of them in
prose, and a page that arrives under a different name leaves those mentions pointing at nothing.

## Writing a page for this catalogue

Three conventions hold thirteen pages together, and two of them are checked by `tests/test_docs.py`
rather than left to each author's memory.

- **A page that exists is a link; a page that does not is italics.** Write
  `[Delegation](delegation.md)` for one you can open and *Watching the loop* for one that is still on
  the list above, so a reader can tell the two apart without clicking. When your page lands, grep the
  catalogue for its italicised title and turn every one into a link, including the ones in a "Go
  deeper" list.
- **List your page above.** Every file in this directory has to appear in this index, and the test
  suite fails by name until it does. It needs an entry in `mkdocs.yml`'s `nav` too, or the strict build
  fails.
- **Keep the five sections, in order.** The idea, Watch it, In Kokua, What it costs, Go deeper, under a
  title and a one-sentence claim in bold. That shape is what makes the catalogue skimmable, and it is
  also enforced.
