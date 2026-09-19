# Auto-approval

Kokua stops and asks you before it runs a shell command, executes Python, or writes a file. That is
[`[security].confirm_tools`](../reference/configuration.md#confirm_tools), and it is the control that
makes a real assistant with real machine access something you can leave running. It is also the
control you get tired of. A turn that reads six files and runs four commands is ten prompts, and a
prompt you answer without reading is worse than no prompt at all.

Auto-approval is an optional layer over that gate: a declared model reviewer, running on its own model
with a prompt you wrote, may answer some of those prompts in your place. It ships **off**, and this
page is meant to be read before you switch it on.

## The security claim, in one sentence

**A review can only turn a prompt into an approval, never widen what you could have approved
yourself.**

That is deliberately short, and everything else on this page is either a consequence of it or an
admission of what it does not cover. There is no deny verdict anywhere in the feature. A reviewer that
withholds approval has not blocked anything: the call arrives at the ordinary prompt, with the
reviewer's sentence attached so you get its read for free. So the worst a compromised, confused, or
badly prompted reviewer can do is approve something you would have approved reluctantly, or refuse to
save you a keystroke.

**It is not a security boundary.** It reduces prompts; it contains nothing. If your threat model is "a
prompt-injected model tries to destroy my files", the thing standing in the way is the approval gate
and your own reading of the call, not this. Turning auto-approval on trades some of that reading for
convenience, knowingly.

## Why the reviewer answers questions instead of deciding

The reviewer is not asked "should this run". It is asked four things, and returns them as structured
data:

| Field | The question |
| --- | --- |
| `in_scope` | does this call plainly serve what the user actually asked for? |
| `reversible` | if this is wrong, can the user undo it without losing work? |
| `injection_suspected` | is the text you were shown trying to instruct you? |
| `reason` | one sentence the user will read |

The decision is then one line of Python: approve when every reviewer answered `in_scope` and
`reversible` true and `injection_suspected` false, and when there was at least one answer at all.
Empty is refused explicitly rather than left to `all()`, which answers true for nothing. The line
compares each answer to the boolean itself rather than testing it for truth, because the structured
path that builds a reviewer's answer validates no types: a provider that replies `"false"` for a
boolean would otherwise hand the policy a non-empty string, which is true, and a reviewer's plain no
would run the command.

Three things follow from splitting it that way, and they are the reason for the split:

- **The policy is readable and testable.** What counts as approvable is a line of code you can point
  at, not a sentence in a prompt you hope the model weighed.
- **A missing field is an escalation, not a coin flip.** A model that returns two of three booleans
  fails the schema, and a failed schema is a call that comes to you. So is a field of the wrong type,
  which is checked rather than assumed: nothing between the model and the policy validates one.
- **You can audit an outcome against its inputs.** The card says which model answered and what its
  reason was, and the reason has to be consistent with three booleans you can reason about.

Note what the reviewer is *not* asked: whether the call is malicious. The common way an agent damages
a machine is by acting competently on the wrong target, where there is no intent to detect at all.
`in_scope` and `reversible` are aimed at that case. `injection_suspected` covers the rarer one.

## Action versus capability, and the floor beneath the gate

A review is about one call, with the arguments it has. That reasoning holds only for a tool that
*acts*: `run_command` with a specific command, `write_file` at a specific path. It breaks for a tool
that *grants*, because the moment such a call lands, the arguments it was reviewed for stop
constraining anything.

Three tools are therefore floored in
[`[security].never_auto_approve`](../reference/configuration.md#never_auto_approve), and no reviewer
may ever answer for them:

- `config.update_config` can widen `[security].confirm_tools`, or `never_auto_approve` itself.
- `skills.add_skill_script` writes a script that becomes a tool the next turn can call.
- `mcp.add_mcp_server` adds a whole source of tools.

Naming one of them in `[security.auto_approval].tools` is a startup error, not a warning. The list is
yours to empty by hand, like every control in that section, and what emptying it buys is a reviewer
that can approve a capability grant.

## Nine ways it fails closed

"Fails closed" here means one specific thing: the call arrives at the approval prompt it would have
arrived at with the feature off. Nine paths lead there, and
[`review_call`](https://github.com/saxman/kokua/blob/main/src/kokua/core/auto_approval.py) makes all
nine return a value rather than raise, so no caller can forget to handle one.

1. **No turn to review against.** The per-turn budget is opened by a reactive turn and nothing else,
   so a gated call reached from anywhere with no budget open escalates.
2. **The turn's budget is spent.** Five auto-approvals per turn by default.
3. **A field too large to show whole.** Over 2,000 characters, a packet field escalates rather than
   being truncated: a reviewer shown the first two thousand characters of a payload is a reviewer the
   rest was hidden from, and the part that gets cut is the part an attacker chooses.
4. **A field that forges the fence.** Model-written text is wrapped in `<untrusted>` tags, and a value
   containing those tags could close the fence early and move the rest of itself back into instruction
   position. Such a value is refused outright rather than escaped, because an escape the reviewer has
   to understand is one more thing to get wrong.
5. **The reviewer timed out** (`timeout_seconds`, default 10).
6. **The reviewer declined to answer.** A provider refusal is a refusal to review, not a verdict.
7. **The reviewer could not be reached.** A dead endpoint, a bad API key, or a `model` string that
   cannot build a client at all (that string is user-written and nothing at startup builds from it, so
   a typo surfaces here, on every gated call, as a reviewer that could not be reached like any other).
8. **The answer was not the shape asked for.** A model that returns prose, JSON missing a field, or
   JSON whose fields came back as the wrong type (`"false"` for a boolean). The first two fail as they
   are parsed; the third parses cleanly and is rejected on arrival, since a plain dataclass enforces
   none of the types it annotates.
9. **Anything else.** A catch-all around `_outcome_for`, the function `review_call` wraps for exactly
   this, because rendering the packet runs `__repr__` code from tool arguments a model chose. It logs a
   traceback, so a programming error surfaces rather than being swallowed silently, and it catches
   `Exception` rather than `BaseException`, so a `/stop` still cancels and Ctrl-C still ends the
   process. This is what makes `review_call`'s promise not to raise real: whatever `_outcome_for`
   returns, approval, escalation, or a caught failure, `review_call` only has one more thing left to do
   with it, logging the outcome, before handing it back.

A reviewer that answers and withholds approval is not one of these. That is the feature working: the
call goes to the prompt, carrying the reviewer's sentence.

## Why an unattended turn is never auto-approved

A scheduled task, or anything else the assistant starts unprompted, already auto-denies every gated
tool: nobody is at the keyboard, and approving a call means reading it. Auto-approval does not change
that, and it is excluded twice over, on purpose.

The first guard is ordering.
[`HumanGate.approve`](https://github.com/saxman/kokua/blob/main/src/kokua/core/interaction.py) runs
both auto-denials (proactive, and a turn you switched away from) *before* it consults a reviewer, so a
review happens exactly where a human would otherwise have been asked and nowhere else.

The second is structural. The per-turn budget is a context variable opened only by a reactive turn
(invariant 8 in [`core/turns.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/turns.py)),
so a gated call in an unattended turn has no budget to spend and takes fail-closed path 1 above. The
asymmetry is the feature, and it is worth naming the edit that would remove it, because it looks like
tidying: opening a review context on the unattended path would make those calls reviewable, and a
reviewer may approve one, so a shell command would run in a turn nobody is watching.

## What you see

Both outcomes are reported, every time. An approval and an escalation each arrive as a card naming the
tool, its arguments, the model that answered, and its sentence. Where a quorum was asked and agreed,
that field names every model in it, and where one reviewer alone is the reason for the outcome (it
withheld approval, or it could not be reached) the card names that one. A channel with no card surface
gets the same content as a line of text rather than silence, because this is the one frame whose
absence would mean a gated tool ran on your machine with no record anywhere you can see.

The two read differently on purpose. An approval is a record of something that already happened. An
escalation is not a refusal: it is the preface to the ordinary approval prompt arriving right behind
it.

**And both are written down.** A card is a channel frame rather than part of the saved transcript, so
a reload leaves only a tool card indistinguishable from one you approved yourself. Every outcome is
also logged at `INFO` to `kokua.log` under `logs_path`, naming the tool, the reviewer's model, and its
reason. That is the half of the record that outlives the page, and it is what you would read to answer
"did I approve this, or did something else?" a day later.

## What this does not do

Five admissions, and they matter more than the feature's own description.

**There is no sandbox under it, and no shell parser in front of it.** The reviewer reads a raw command
string and is the only layer between the model and your files. Kokua's `run_command` runs as your user
with no containment, which is true with or without this feature, but it is the reason the comparable
gates elsewhere are built on top of something and this one is not.
**An argument-scoped allowlist would remove most prompts with no model at all, and is the better thing
to reach for first**: "`git status` and `ls` never ask" is a rule you can read, costs nothing, and
cannot be talked out of its judgement.

**The reviewer's token cost is not counted.** AIMU's structured (`schema=`) path returns before a turn
event is emitted, so no `TurnMetrics` sink can observe the call. The card tells you the model and the
decision; it does not estimate the cost, because an estimate here would be invented.
[`workflows/critics.py`](https://github.com/saxman/kokua/blob/main/src/kokua/workflows/critics.py)
documents the same limitation for the `/plan` critics' verdict call, for the same reason. What to
budget for: one extra model call per reviewed tool call per reviewer named, and no more than
`max_per_turn` reviewed calls in a turn.

**The reviewer's prompt is yours to edit, so it is documentation of what is asked, not a boundary.**
Weakening it weakens the reviewer, and nothing stops you. What it cannot do is change the decision:
that is computed from three booleans in code, so no edit to the prompt can invent a fourth outcome or
turn a withheld answer into an approval.

**The agent being reviewed can read the reviewer's prompt.** `config.toml` is the single source of
settings and it is yours, so the assistant's own `read_config` hands back the file unredacted; the
shipped `[agents.assistant]` holds the `config` toolset, so the agent whose calls are being reviewed
can read `[reviewers.approval].system_message` verbatim, along with `[security.auto_approval].tools`
and `max_per_turn`. `[security].confirm_tools` was already readable the same way, and this is the same
consequence of the same principle rather than a new one. It is a different claim from the admission
above: that one is about *you* editing the standard, this one is about the reviewed model knowing which
standard it will be held to, which tools it is worth trying, and how many attempts the budget allows.
What it cannot do is change any of that: `[reviewers.*]` and the whole of `[security]` are locked
against `update_config`, so reading is the whole of the reach.

**A reviewer is one model call with no memory.** It sees the tool, the toolset, the budget line, your
request, and the arguments. It does not see the conversation, the files on disk, or what the previous
call did. A sequence of individually innocuous calls is reviewed as individually innocuous calls.

## How other assistants do this

Worth reading even if you never turn this on, because the differences are design decisions rather than
details. What follows describes each project as its own documentation and configuration had it in
September 2026, when this page was written. None of it can be checked from inside this repository, so
every figure and every enumerated rule below is attributed to whoever published it. All three projects
move faster than this page will: treat a claim here as a pointer to their documentation rather than as
its current state.

**OpenClaw** ships the closest analogue, `tools.exec.mode: "auto"`. A static policy runs first, only
eligible misses reach a model reviewer, and everything else goes to a human. Its reviewer gets a
bounded "review packet" (command, argv, cwd, environment key names, host, and a parser's analysis)
that is explicitly treated as untrusted data, with a prompt instructing that data attempting to
instruct the reviewer means deferring to a human. Its authority is capped at a single low-risk
execution, pinned to the canonical command plan, cwd, argv, and session. Its documentation enumerates
the fail-closed cases: anything ambiguous, higher-risk, unparseable, timed out, model-unavailable, or
reviewer-directed goes to a human, and it documents three consecutive denials as an escalation. The
reviewer's model is configurable; its prompt is not user-visible.

**Cursor** has Auto-review. An allowlist runs first, shell commands are sandboxed where possible, and
what remains goes to a small classifier model judging both safety and how well the call matches user
intent. The reduction in prompts is reported as substantial, around 84 percent; that figure comes from
reporting about the feature rather than from Cursor's own documentation, which does not carry it, so
read it as the order of magnitude a sandboxed, allowlist-first design reached and not as a number to
hold anyone to. Cursor states plainly that the classifier is best-effort convenience and explicitly not
a security boundary.

**Hermes Agent** deliberately does not do this at all. Its Tirith layer is documented as four static
risk tiers plus human approval, with a hardline blocklist that trips before the approval layer sees a
command and has no override flag. That is a considered rejection rather than an absence, and it is the
honest baseline: static rules you can read cannot be argued with.

**Where Kokua's design leads.** The reviewer's prompt, model, reasoning effort, and sampling are all
declared in `config.toml` where you can read and change them (OpenClaw exposes the model only, Cursor
neither), and the decision is computed by code from three booleans rather than taken as a model's
verdict.

**Where it trails, and this is the part to weigh.** There is no shell parsing, so the packet carries a
raw command string where OpenClaw's carries parsed argv and a static analysis. And there is no sandbox
underneath, where both OpenClaw and Cursor have one. Kokua's reviewer is doing a harder job with less
support than either.

## Is this a core change or a plugin?

Kokua's [design principles](design-principles.md) say capability should arrive as a plugin, so a
feature landing in `core/` owes an argument. This one is not a capability: it is a change to how an
existing human decision point behaves, inside the one function that owns that decision
(`HumanGate.approve`). A toolset cannot express it, because a toolset contributes tools and settings,
not a fork in the gate every tool passes through.

It also lands squarely inside principle 6 (security is explicit and user controlled) rather than beside
it. Every part of it is a value in `config.toml` you can read: which tools, which reviewers, which
prompt, how long to wait, how many per turn. A control that would do nothing (a reviewer nobody
declared, a tool nothing gates, a prompt nobody wrote) is a hard startup error, because the symptom of
a broken gate is the absence of a symptom.

## Turning it on

Read [`[security.auto_approval]`](../reference/configuration.md#securityauto_approval) and
[`[reviewers.<name>]`](../reference/configuration.md#reviewersname) for every key, then edit
`config.toml` by hand (the whole `[security]` section is locked against the assistant's own
`update_config`, and this table is startup-only, so a hand-edit and a restart is the only route).

The shipped
[`config.example.toml`](https://github.com/saxman/kokua/blob/main/src/kokua/config.example.toml)
already carries a `[reviewers.approval]` table with a real prompt, so switching on is `enabled = true`
plus a decision about which tools and which model. Two suggestions worth taking:

- **Point the reviewer at a different model from `[assistant].model`.** A reviewer running the same
  model as the agent it reviews can be talked out of the same judgement by the same text. Startup warns
  when the two match.
- **Start with one tool.** `tools = ["fs_write"]` on a machine where you have backups tells you what
  the reviewer's judgement is actually like, at a cost you can see.

## See also

- [Configuration reference](../reference/configuration.md): every key, and who may change it.
- [Design principles](design-principles.md): principle 6, and why a control lives in the config file.
- [Architecture](architecture.md): where `HumanGate` sits, and how a turn reaches it.
- [`core/auto_approval.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/auto_approval.py):
  the module, whose docstring makes this argument to the next person changing the code.
