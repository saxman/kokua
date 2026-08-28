# Install a third-party toolset

Every toolset Kokua ships arrives through the same seam a third party's package uses: the
`kokua.toolsets` entry-point group. [jobme](https://github.com/saxman/jobme), a separate project that
tailors a resume and cover letter to a job posting and produces send-ready PDFs, is the first package
outside this repository to register one, and it exercises this guide's every step: install, declare,
configure, and, for its one expensive tool, gate. None of this is jobme-specific machinery; the same
four steps apply to any third-party toolset, and jobme is simply carried through them as a worked
example.

> A toolset that is installed, even one that is connected or running, does nothing until an
> `[agents.<name>].tools` list names it. Installing it is never the last step.

## What Kokua sees when you install one

Kokua discovers toolsets at startup with `kokua.plugins.discover_toolsets`, which reads every entry
registered under the `kokua.toolsets` group through Python's own `importlib.metadata.entry_points`, the
same call that finds Kokua's own 21 built-in toolsets listed in its `pyproject.toml`. There is no second
registry and no code change on Kokua's side to add a plugin: a package that publishes an entry in that
group is found the moment it is installed into the environment Kokua runs in.

jobme's `pyproject.toml` carries one line for this:

```toml
[project.entry-points."kokua.toolsets"]
jobme = "jobme.kokua_toolset:TOOLSET"
```

The key on the left, `jobme`, is the name that lands in Kokua's one toolset namespace, alongside `web`,
`memory`, an MCP server's own name, and every built-in. It is also the exact name you write into
`[agents.<name>].tools` below, and it must equal the `Toolset.name` the entry points at: Kokua's own
built-ins are pinned against that agreement by `tests/toolsets/test_registration.py`, but a third-party
package has no such test running against it, so a mismatched name here would register a plugin silently
under the wrong word. An entry point is inert until something loads its group, which is why jobme's own
package imports nothing of Kokua's and runs standalone with no idea Kokua exists; `jobme/kokua_toolset.py`
is the one module in it that does.

## Install the package

If you are developing jobme alongside your Kokua checkout, install it as an editable sibling from
inside Kokua's own checkout. There are two ways to do it, and the tradeoff between them is worth
knowing before you pick one:

```bash
cd kokua
uv add --editable ../jobme
```

`uv add` (recommended) edits this checkout's `pyproject.toml` to record jobme as one of its
dependencies, so a later `git pull` in Kokua can conflict with that edit. What it buys you is
durability: `uv sync` performs an exact sync (`uv help sync`) that removes any installed package
not declared as a dependency, and `uv sync` is the very first command in Kokua's own setup and what
you run after every pull. Without a declared dependency, the next plain `uv sync` silently
uninstalls jobme again, and Kokua's next start fails with `agent 'assistant' declares unknown
toolset 'jobme'`, an error that says nothing about the sync that caused it. This does not make
Kokua-the-project depend on jobme in any deeper sense: the plugin still arrives purely through the
entry point described above, so "no code change on Kokua's side" stays true of the code; it is only
this checkout's dependency list that changed.

`uv add --editable ../jobme` writes one more thing alongside that dependency line: a
`[tool.uv.sources]` path entry pointing at `../jobme`. That collides with this repo's own primary
install command, `uv sync --all-extras --no-sources` (see the top-level README): `--no-sources`
exists to make AIMU resolve from PyPI instead of the sibling `../aimu` checkout most contributors
don't have, and it does that by ignoring the entire `[tool.uv.sources]` table, jobme's new entry
included. Run that sync after adding jobme and the path source Kokua needs to find it by is gone,
so jobme resolves from PyPI instead, where it does not exist, and the sync fails outright. Once
jobme is added, sync this checkout with plain `uv sync --all-extras` instead. And because `uv add`
rewrites `uv.lock` as well as `pyproject.toml`, a later `git pull` here can conflict on two files
rather than one.

The alternative writes nothing to `pyproject.toml` at all:

```bash
cd kokua
uv pip install --editable ../jobme
```

`uv pip install` puts the package into Kokua's environment and stops there, which is attractive if
you would rather not touch `pyproject.toml`. The cost is that jobme then exists only in the
environment, not in anything `uv sync` reads, so the very next plain `uv sync` removes it. If you
use this form, either reinstall after every sync or run `uv sync --inexact`, which keeps packages
that are not declared as project dependencies.

Once it is published, an ordinary install is enough instead:

```bash
pip install jobme
```

Either way, confirm the name reached the namespace before touching anything else:

```bash
uv run kokua --list-toolsets | grep jobme
```

It should print under the `plugin` group, distinct from `built-in toolset`, `skill`, and `MCP server`,
which is how `--list-toolsets` answers "where did this name come from." Nothing printing means the
package is not installed into the environment Kokua is actually running from, a different mistake from
anything covered further down this page.

## Declare it on an agent

Installing jobme grants nothing by itself. Add its name to the `tools` list of the agent that should
hold it, most often the entry agent:

```toml
[agents.assistant]
tools = ["memory", "documents", "skills", "config", "mcp", "scheduling", "conversations",
         "planning", "capabilities", "time", "jobme"]
```

Restart Kokua. Agent tables are read at startup, so the edit takes effect on the next process, not the
next message. Once it does, that agent holds jobme's two tools, `tailor_application` and
`check_application_setup`, and its system message picks up the paragraph jobme's toolset carries as
guidance: confirm the posting and the job title with the user before spending several minutes and real
provider cost on a run, and call `check_application_setup` first whenever one fails.

## Give it its settings

A toolset that wants its own configuration owns a `[<name>]` section, seeded from `Setting` declarations
on the `Toolset` object itself; nothing else in `config.toml` has to know the keys exist for them to be
valid. jobme declares four, all strings:

```toml
[jobme]
input_dir = ""             # default: $KOKUA_HOME/data/jobme/input
output_dir = ""            # default: $KOKUA_HOME/data/jobme/output
model = ""                 # default: $JOBME_MODEL, then jobme's own built-in default
pdf_backend = "playwright"
```

The empty string is not itself the default value; it is jobme's own convention for "derive one from
`AssistantConfig` at read time," the same pattern `[github_backup].repo` uses, because a `Setting`'s
declared default is a static value with no view of `$KOKUA_HOME` at the point the toolset module is
written. None of jobme's four keys are marked hot, so, like the declaration above, an edit here needs a
restart before the running assistant reads it.

## Gate the expensive tool

`tailor_application` runs the whole pipeline: several minutes, and real provider spend on every call.
That is exactly what `[security].confirm_tools` exists for, so add it there:

```toml
[security]
confirm_tools = ["execute_python", "run_command", "update_config", "tailor_application"]
```

Make the two edits in this order. `confirm_tools` is checked against every tool this config actually
builds, so a name with no toolset behind it yet fails startup, listing the near misses; jobme has to be
declared on some agent first, whether in the same file or an earlier run, before `tailor_application`
exists for this list to name at all.

The consequence worth knowing before you lean on it: a proactive turn, meaning a scheduled task or
anything else Kokua starts unprompted, auto-denies every gated tool outright, with no prompt raised
anywhere. So a scheduled task that asks the assistant to tailor an application has that call denied
every time; there is nobody at the keyboard for the approval to reach. That is not a limitation to route
around: `tailor_application` takes an argument and spends money on every call, exactly the shape a gate
exists to keep out of an unattended turn. Contrast `github_backup`'s `backup_kokua_state`, built
deliberately to take no arguments so it can stay ungated and still run on a schedule; jobme's tool was
never a candidate for that shape.

## Confirm it loaded

Two checks, in order of directness:

- `uv run kokua --list-toolsets | grep jobme` confirms the entry point resolved, and shows which group
  claimed the name.
- Start Kokua and ask the assistant what it can do, or, on an agent that delegates, ask it to delegate to
  whichever worker holds jobme and list its tools. The guidance paragraph jobme's toolset carries should
  show up in the answer too, since it is appended to that agent's system message.

One failure mode is silent by design and worth knowing before you meet it: a `[jobme]` section with no
agent declaring the toolset produces a startup warning, not an error, logged to
`$KOKUA_HOME/data/logs/kokua.log` (there is no console handler for it), saying its settings are read by
nobody. That case assumes jobme is actually installed. If it is not, and a `[jobme]` section is still in
the file, that is the ordinary unknown-section error instead: a hard startup failure, because no
installed toolset owns the section at all. The two look alike on the page and land very differently: one
is a quiet log line you could miss for weeks, and the other stops Kokua from starting.

## jobme as a worked example

Read jobme's own `kokua_toolset.py` for the far side of this seam: a `build(ctx)` that returns both
tools unconditionally, `check_application_setup` reporting exactly what is missing rather than letting
the model guess, and a `Toolset` declaration whose four `Setting`s are the ones shown above.

It also ships a skill, `job-application`, that teaches the model the procedure around
`tailor_application`: get the whole posting verbatim, run the setup check first, and confirm with the
user before spending. `kokua skills install` will not find it, because that command only ever reads
Kokua's own bundled `skills/` directory inside this repository; it has no idea a third party's package
carries one at all. Copy it into your skills folder by hand instead, naming the destination for the
skill rather than for the package it came from:

```bash
cp -R ../jobme/jobme/skill ~/.kokua/data/skills/job-application
```

(the skills directory is always `<data_dir>/skills`, and `data_dir` defaults to
`$KOKUA_HOME/data`; if you have set `[paths].data_dir` to something else, that path can be anywhere,
not necessarily under `$KOKUA_HOME`, so copy there instead).
Restart Kokua afterward: the skill catalogue is scanned once and cached, and only `author_skill` and
`add_skill_script` refresh it, neither of which runs for a directory copied in by hand. If the agent you
declared jobme on already holds the `skills` authoring toolset, as the shipped `[agents.assistant]` does,
it sees the new skill without any further declaration, since an authoring agent's catalogue is every
skill on disk rather than a scoped list.

## See also

- [Set up a toolset](set-up-toolsets.md): the one namespace every toolset joins, and how to write one of
  your own rather than install someone else's.
- [Add a skill](add-skills.md): the `SKILL.md` format and the one directory Kokua scans, in full.
- [Add an MCP service](add-mcp-services.md): the third source of capability, gated and declared the same
  way.
- [Architecture](../explanation/architecture.md#plugins): where `discover_toolsets` and
  `discover_frontends` sit in the startup sequence.
- [Design principles](../explanation/design-principles.md#corollary-a-capability-is-declared-never-defaulted):
  why installing, or even connecting, is never the last step.
- [Configuration reference](../reference/configuration.md#toolset-sections): how any toolset's `[<name>]`
  section is validated, and what a name collision with a core section does.
- [Configuration reference](../reference/configuration.md#confirm_tools): the full behavior of
  `[security].confirm_tools`, including what a misspelled entry does and does not protect against.
