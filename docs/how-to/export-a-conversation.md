# Export a conversation

`kokua export` writes one stored conversation to a Markdown file: what was said, what the model
reasoned, every tool call with its arguments and result, and what each turn cost. It exists because
watching a run stream by in the browser only works while the run is on screen. The export is the
artifact you keep afterwards, to diff against another run, paste into a review, or read once the
terminal is gone.

It reads the conversation straight out of the session store and writes the file; nothing else. No
model client, no agent, no front end, so it works even with the model server down.

There are three routes to the same content, written to different files: the `kokua export` command
below, a download button in the web UI's sidebar (see [From the web UI](#from-the-web-ui)), and
asking the assistant to export one so it can analyze it (see [Have the assistant analyze a
conversation](#have-the-assistant-analyze-a-conversation)). All three call the same renderer, so the
files read identically; only their names differ (see each route's section below).

## The command

```bash
kokua export                       # the most recently updated conversation
kokua export abcdef01              # a full id, or a unique leading fragment of one
kokua export abcdef01 -o notes.md  # write somewhere specific
kokua export abcdef01 -o -         # write to stdout instead of a file
kokua export abcdef01 --full       # do not truncate long tool arguments and results
```

The conversation argument takes an id, a unique leading fragment of one (at least 6 characters,
the same rule the assistant's own `read_conversation` tool follows), or `latest` (the default). A
fragment that matches more than one conversation opens neither: it is reported as matching several
conversations, with a count, so you know to type a few more characters rather than go looking for a
typo.

Without `-o`, the file lands in `$KOKUA_HOME/data/downloads/<conversation id>.md` (by default
`~/.kokua/data/downloads/`), the same folder generated PDFs and other artifacts use, and the path
is printed on success so a script can pick it up. `-o -` writes the Markdown to stdout instead, for
piping into something else. `--full` lifts the truncation a plain export applies to long tool
payloads (see below).

## What a turn's block shows

The file opens with a header: the conversation's title (falling back to its first message, then to
"Untitled conversation"), its id, when it was created and last updated, and a running total of
model calls, tokens, and time, when any of those were recorded.

Each turn after that is one `## Turn N` section: the user's message, the model's reasoning (if the
run streamed a `thinking` block or a verbose trace's `reasoning` segments), its answer, and, for
each tool call, the tool's name, its arguments, and what it returned. A line under the turn heading
names the model and reasoning effort it ran at, when either was recorded, and a line beneath that
reports what the turn cost. A sub-agent the turn spawned gets its own nested card with the same
shape: its own reasoning, tool calls, and answer. If a turn stopped short (a tool call was
denied, a reviewer sent it back for revision, an error cut it off), the reason appears as a quoted
note where the turn stopped.

## Two honesty rules

**A figure nobody reported prints as "not reported", never as an invented zero.** Not every model
or provider reports token counts, and a stored absence rendered as `0` would be a false claim about
what a run cost. The same rule covers delegation: a turn that spawned a sub-agent but whose usage
record carries no per-agent breakdown says the delegated cost was not counted, rather than showing
the entry agent's own total as if it were the whole turn's.

**A truncated payload says so.** A tool call's arguments or result is arbitrary text a model or a
tool produced, and can be large enough to bury the turn being judged under it. Past
`kokua.transcript_export.DEFAULT_MAX_PAYLOAD_CHARS` (4000 characters), the export cuts a payload and
appends a note saying how many characters the full payload held, rather than silently showing an
incomplete result as if it were complete. Pass `--full` to lift the cap and see every payload in
full.

## What this does not include

**A workflow review's typed verdict is not counted, and nothing marks it.** A `/plan` review's
tool-calling assessment is counted like any other delegated work, but the call that turns it into a
structured verdict goes through AIMU's schema-only path, which returns before any turn event is
emitted, on any client. That one call per review round never enters a turn's recorded cost, and
because it never enters the count at all, no "not reported" note can point at it the way it does for a
missing token figure.

**No per-turn or per-model rollup in the header, and no per-spawn token breakdown on a sub-agent's own card.** The
header totals model calls, tokens, and time across the whole conversation; it does not also break that
total down by turn, by model, or by delegation depth, and a sub-agent's card shows its reasoning, tool
calls, and answer without a token figure of its own. Both are things a fuller export could add; this
one does not, so the total is what you have to work from until a later change adds them.

## From the web UI

Hover a conversation in the sidebar and a download arrow (↓) appears beside its delete button.
Clicking it does not switch you into that conversation or reload the page: the file is rendered and
written on the server, in the same `$KOKUA_HOME/data/downloads/` folder the command line writes to,
and the browser saves it the way any other file download is saved. The exported filename carries
today's date, a slug of the conversation's title, and a short fragment of its id, so several exports
land in that folder without overwriting each other or reading as anonymous.

There is no `GET /export/{id}` route: the web front end builds its assistant fresh per WebSocket
connection, so a plain HTTP handler would have no live session store to read from. The button sends
an `"export"` control over the same socket every other sidebar action uses, and the server answers by
writing the file and pointing the page at the existing `/download/{name}` route (the one that already
serves generated PDFs and other artifacts) rather than opening a second way to fetch a file.

**The download button cannot lift the truncation cap.** `--full` is a command-line flag, and the
sidebar's button always exports with `DEFAULT_MAX_PAYLOAD_CHARS` in effect. A browser-only user is not
stuck, though: asking the assistant to export the conversation runs the tool below, which does take
the flag. Or use `kokua export --full` from the command line.

**Data at rest.** A full transcript, once exported, persists indefinitely under `downloads_path` and
is served by `/download/{name}` over plain HTTP with no authentication. The path-traversal guards on
that route are sound and the default `[web] host` is loopback-only, but `host` is a setting a user can
change, and a conversation is often the most sensitive thing Kokua holds. Treat an exported file, and
the host it is served from, accordingly.

## Have the assistant analyze a conversation

The same export is a tool the assistant holds, `export_conversation`, which is how you get an answer
about a run rather than a file about it. Ask in plain words:

> Something went wrong in the conversation about the flight prices yesterday. Which tool call failed,
> and what did it return?

The assistant finds the conversation (`list_conversations` or `search_conversations`), exports it, and
gets back a path plus how long the file is. What happens next depends on the length, and that is the
part worth understanding.

**A path, not the transcript.** The tool answers with a file path on purpose. AIMU's `fs` group offers
`read_file(path, max_lines)` and no offset, so a file can be read from its first line and nowhere else,
and a tool that returned the Markdown directly would spend the asking conversation's whole context on
one run's tool output. So a long export is meant to be handed to a sub-agent whose fresh context
absorbs the file, returning only the findings, and the assistant's guidance tells it to delegate rather
than read once the file is past a few hundred lines. A short export it will usually just read itself,
which is the right call: a delegation costs a spawn.

**Or ask for an evaluation, and skip the export.** `config.example.toml` ships
`[agents.introspector]`, a sub-agent whose subject is a conversation and whose job is judging one
against criteria you give:

> Evaluate yesterday's conversation about the deployment. I care about whether it verified its claims
> before acting, and whether it delegated work it should have done itself.

That worker holds the conversation tools itself, so the whole job is one delegation: it finds the
conversation, exports it, reads the file, and reports per criterion, quoting the transcript lines each
judgment rests on. Nothing but its report enters your conversation. Three things it is instructed to be
strict about: a criterion the transcript cannot settle is reported as unassessable rather than guessed
at, a truncated read is reported as covering only part of the run, and if you give no criteria it
evaluates against whether the run reached what you asked for, what it spent, and where it went wrong,
saying up front that it chose them.

**Ask for `full` detail when the tool result is the thing you are debugging.** The tool takes the same
cap `--full` lifts, so "export it with the full tool output" gets you a file with nothing cut.

**What it cannot do yet.** If the export is longer than the introspector's own context, the read stops
part way and the analysis covers only the beginning of the run. It is instructed to say so rather than
answer as if it had read the whole thing, so you get "I saw the first N lines" instead of a confident
partial answer, but the underlying fix (paging into a file) is not there yet: it needs an
`offset` on AIMU's `read_file`, which is item 19 in `TODO.md`. For a very long run, `kokua export` plus
your own editor is still the more reliable read.

**The current conversation is a blind spot, and it says so.** The export renders the *stored*
transcript, and the turn you are in right now is not stored until it finishes. Exporting the
conversation you are talking in gets you everything up to this turn, and the answer says as much; the
same goes for a conversation whose reply is still streaming somewhere else.

**This grants a delegate two kinds of reach.** `[agents.introspector]` declares `fs`, which grants read
of any file on the machine and not only of an export, and `conversations`, which lets it read (and
rename) any saved conversation rather than only the one you asked about. The first is the same reach
`[agents.coder]` already ships with; the second matters because a transcript is untrusted text, so an
injection sitting in one conversation could steer a worker that was spawned from another. Like every
capability here both are lines in your `config.toml`: delete the agent and its name from
`[agents.assistant].delegates_to` if you would rather not have them, and asking the assistant to export
still works (it will read the file itself, or hand you the path).

## The store, mid-write

TinyDB (the session store's format) rewrites its whole file on every save, so an export that races
the assistant's own write can land on a partial file. Rather than parse whatever arrived, `kokua
export` reports that the store is busy and asks you to try again in a moment: presenting half a
conversation as the conversation would be worse than making you wait.

## See also

- [`transcript_export.py`](https://github.com/saxman/kokua/blob/main/src/kokua/transcript_export.py): the renderer this command calls,
  pure and dependency-free, so both front ends could call it too.
- [Architecture](../explanation/architecture.md): where the conversation store and the CLI fit in
  the rest of Kokua.
