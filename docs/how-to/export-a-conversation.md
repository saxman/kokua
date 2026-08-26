# Export a conversation

`kokua export` writes one stored conversation to a Markdown file: what was said, what the model
reasoned, every tool call with its arguments and result, and what each turn cost. It exists because
watching a run stream by in the browser only works while the run is on screen. The export is the
artifact you keep afterwards, to diff against another run, paste into a review, or read once the
terminal is gone.

It reads the conversation straight out of the session store and writes the file; nothing else. No
model client, no agent, no front end, so it works even with the model server down.

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
fragment that matches more than one conversation is reported as ambiguous rather than opening
either one.

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
shape: its own reasoning, tool calls, answer, and cost. If a turn stopped short (a tool call was
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

## The store, mid-write

TinyDB (the session store's format) rewrites its whole file on every save, so an export that races
the assistant's own write can land on a partial file. Rather than parse whatever arrived, `kokua
export` reports that the store is busy and asks you to try again in a moment: presenting half a
conversation as the conversation would be worse than making you wait.

## See also

- [`transcript_export.py`](../../src/kokua/transcript_export.py): the renderer this command calls,
  pure and dependency-free, so both front ends could call it too.
- [Architecture](../explanation/architecture.md): where the conversation store and the CLI fit in
  the rest of Kokua.
