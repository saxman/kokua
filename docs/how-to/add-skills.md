# Add a skill

A *skill* is a directory holding a `SKILL.md` file: YAML frontmatter plus markdown instructions for a
repeatable procedure. The assistant sees a catalogue of every skill's name and description, and loads a
skill's full instructions on demand. A skill can also ship runnable scripts, each of which becomes a
callable tool.

Skills are the one capability the assistant keeps for itself rather than delegating, so this guide is
shorter than it looks: there is one directory, and three ways to put something in it.

## Two things named `skills`, and one named per skill

**`skills` is the authoring toolset.** It carries `author_skill` and `add_skill_script`, and only the
**entry agent** (the one `[assistant].agent` names) can hold it:

```toml
[agents.assistant]
tools = ["skills", "memory", "time"]      # `kokua config init` writes this and more
```

Declaring `skills` on any other agent is a startup error, because a spawned worker is a plain AIMU
`Agent` rather than a `SkillAgent`, so there is nothing for skill injection to hook. The registry marks
the toolset `entry_point_only`, so you find that out at startup rather than through a worker that quietly
has no skills.

Drop `skills` and the assistant can no longer *write* skills, but it can still use the ones already in
the directory: the entry agent is a `SkillAgent` either way, so the catalogue and `activate_skill` come
from AIMU rather than from this toolset.

**Each individual skill is also a name in the toolset namespace**, so you can give one skill to one
agent:

```toml
[agents.reporter]
description = "Builds and mails reports."
tools = ["markdown-to-pdf", "email-report", "fs", "compute", "time"]
```

A skill name sits beside `web` and an MCP server's name and does not say which kind it is. `kokua
--list-toolsets` shows every skill on disk under a `skill:` group, and a name that is not there fails at
startup listing the ones that are.

**Give a script-carrying skill an agent that can run scripts.** A worker declaring a skill gets that
skill's `{skill}__{stem}` script tools and `activate_skill` through the registry, and the skill's own
instructions usually tell it to run something -- so `fs` and `compute` belong in the same `tools` list.

**One rule worth knowing:** an agent holding the `skills` authoring toolset sees *every* skill on disk,
not just the ones it declares. Scoping an author's catalogue would hide the skill it just wrote, and
`add_skill_script` promises the script is callable in the same turn. A non-authoring agent's catalogue is
scoped to its declaration.

## Where Kokua looks

**Only `$KOKUA_HOME/data/skills/`** (by default `~/.kokua/data/skills/`, or `<data_dir>/skills` if you
set `[paths] data_dir`).

This is worth stating plainly because AIMU's `SkillManager` defaults to scanning four search paths
(`.agents/skills/`, `.claude/skills/`, and their `~` equivalents). Kokua passes `skill_dirs` explicitly
([`registry/context.py`](../../src/kokua/registry/context.py)), so those defaults **do not apply here**. A
skill in `~/.claude/skills/` is invisible to Kokua. This follows from the principle that all state lives
under one directory you own; nothing is read from your working directory.

## The `SKILL.md` format

```markdown
---
name: weekly-digest
description: Compile the week's notes into a digest and email it.
---

# Weekly digest

1. Read the documents saved this week.
2. Group them by project, newest first.
3. Draft the digest in Markdown, then delegate the send to the report-writer agent.
```

`name` and `description` are required. `name` must be a kebab-case slug: lowercase words joined by
hyphens, never underscores or spaces. A malformed `SKILL.md` raises `SkillLoadError` rather than being
skipped silently, so a typo fails loudly at startup. See AIMU's
[use skills](https://saxman.info/aimu/how-to/use-skills/) for the optional frontmatter fields.

`description` carries more weight than its length suggests: it is the only part of a skill in the
catalogue, so it is the whole basis on which the model decides whether to load the skill at all. Write
it as a trigger ("when you need to ..."), not a title.

## Four ways to add one

### Install one Kokua ships

The repository carries a few skills in its own `skills/` directory, outside the package:

```bash
kokua skills list                          # what is bundled, with each description
kokua skills install markdown-to-pdf       # copy it into your skills folder
kokua skills install                       # all of them
```

They land in the skills folder your config resolves, so `[paths].data_dir` and `$KOKUA_HOME` are
honoured. An existing skill of the same name is left alone unless you pass `--force`, so a local edit
survives a reinstall. `dice-roller` is the smallest complete example: copy it as a starting point.

These ship with the repository rather than the wheel, so a `pip install kokua` has no copy of them and
the command says where to get one.

### Write the directory by hand

```
~/.kokua/data/skills/
└── weekly-digest/
    ├── SKILL.md
    └── scripts/
        └── collect_notes.py
```

One `SkillManager` is shared by every conversation, so a skill authored in one conversation is usable in
all of them. The flip side is that the manager caches its catalogue and only re-scans the directory when
something refreshes it, which `author_skill` and `add_skill_script` do and a hand-written directory cannot:
**restart Kokua after adding a skill by hand.** Starting a new conversation is not enough, since the new
agent reads the same cached catalogue.

### Ask the assistant to write it

The entry agent has an `author_skill` tool, and the guidance the `skills` toolset carries into its prompt
tells it to reach for that tool when you teach it a repeatable procedure worth remembering. So the
shortest path is a sentence:

> "That worked. Save it as a skill called `weekly-digest` so you can do it the same way next Friday."

`author_skill(name, description, body)` writes the `SKILL.md` and refreshes the manager, so the skill is
usable in the same conversation. It **will not overwrite** an existing skill; to revise one, either edit
the file by hand or delete the directory first. It is not gated for approval, since it only writes
markdown.

### Attach a script

`add_skill_script(skill_name, filename, content)` writes `scripts/<filename>` (a `.py` or `.sh` file)
into a skill that already exists, then reloads the agent's skills so the new tool is callable in the same
turn.

- The tool it creates is named `{skill_name}__{stem}`, with both halves lowercased and every run of
  other characters collapsed to `_`, so `collect_notes.py` inside `weekly-digest` becomes
  `weekly_digest__collect_notes`. Note the hyphen becomes an underscore; the `__` stays the only
  separator.
- Because names collapse, two scripts in one skill can collide and only the first is registered:
  `collect_notes.py` with `collect_notes.sh` (same stem), and `collect-notes.py` with
  `collect_notes.py` (same slug). Give each script a distinct stem.
- **To fix a broken script, reuse the exact same filename.** That overwrites in place and the tool keeps
  its name. A different filename creates a second script and leaves the broken one callable.
- The skill must exist first. Calling it for an unknown skill returns the list of skills that do exist.
- It is **gated** by default (`[security] confirm_tools`), so each call waits for your `y/N` in the
  terminal or Allow/Deny in the web UI.

Scripts run as real subprocesses with your user's privileges and no sandbox. Their stdout becomes the
tool result. The catalogue lists script tool names inline, so the model can call one directly without
loading the skill's instructions first.

## Who gets skills, and who does not

The entry agent is an AIMU `SkillAgent`; every agent it spawns is a plain `Agent`, which is why the
`skills` *authoring* toolset cannot be declared on one. A worker can still hold an individual skill: its
script tools and `activate_skill` arrive through the registry rather than from `SkillAgent`. This
has a practical consequence for how you write a skill:

Write the procedure from the entry agent's point of view, as a plan it carries out with the tools it
declares plus delegation. In the shipped config that agent holds only cross-cutting toolsets, so a skill
body saying "search the web for X, then write the file" describes work it cannot do. "Delegate the lookup
to `researcher`, then have `coder` write the file" describes work it can. Naming the agent you expect is
the single most useful thing a skill body can do. If you have edited `[agents.*]` to give the entry agent
domain toolsets of its own, write to what you gave it.

Skill *scripts* are the exception, and a deliberate one: a `{skill}__{stem}` tool is mounted on the entry
agent, so a script is one way to give it a concrete capability of its own without touching the core or
installing a toolset.

## Security

Skills are executable capability you accumulate over time, in a directory the assistant can write to.
Two things follow:

- `add_skill_script` is gated because it is arbitrary code execution with a delay: the script is written
  in one turn and may be invoked in any later one, including an unattended scheduled turn. Approving it
  approves every future call.
- Proactive and backgrounded turns auto-deny every gated tool, so the assistant cannot author and run a
  new script while you are not watching. It can still call scripts that already exist.

Review `~/.kokua/data/skills/*/scripts/` the way you would review anything else on your `PATH`.

## See also

- [Set up a toolset](set-up-toolsets.md): how any capability reaches any agent, which is the other half of
  the picture a skill body needs.
- [Add an MCP service](add-mcp-services.md): the third source of capability.
- [Architecture](../explanation/architecture.md#how-an-agents-tools-resolve): how a declaration becomes
  tools, and the shipped entry agent's full inventory.
- AIMU: [use skills](https://saxman.info/aimu/how-to/use-skills/) for `SkillAgent`, the catalogue, and
  the full `SKILL.md` reference.
