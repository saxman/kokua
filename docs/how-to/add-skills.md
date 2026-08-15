# Add a skill

A *skill* is a directory holding a `SKILL.md` file: YAML frontmatter plus markdown instructions for a
repeatable procedure. The assistant sees a catalogue of every skill's name and description, and loads a
skill's full instructions on demand. A skill can also ship runnable scripts, each of which becomes a
callable tool.

Skills are the one capability the assistant keeps for itself rather than delegating, so this guide is
shorter than it looks: there is one directory, and three ways to put something in it.

## Where Kokua looks

**Only `$KOKUA_HOME/data/skills/`** (by default `~/.kokua/data/skills/`, or `<data_dir>/skills` if you
set `[paths] data_dir`).

This is worth stating plainly because AIMU's `SkillManager` defaults to scanning four search paths
(`.agents/skills/`, `.claude/skills/`, and their `~` equivalents). Kokua passes `skill_dirs` explicitly
([`core/build.py`](../../src/kokua/core/build.py)), so those defaults **do not apply here**. A skill in
`~/.claude/skills/` is invisible to Kokua. This follows from the principle that all state lives under
one directory you own; nothing is read from your working directory.

## The `SKILL.md` format

```markdown
---
name: weekly-digest
description: Compile the week's notes into a digest and email it.
---

# Weekly digest

1. Read the documents saved this week.
2. Group them by project, newest first.
3. Draft the digest in Markdown, then delegate the send to the report-writer role.
```

`name` and `description` are required. `name` must be a kebab-case slug: lowercase words joined by
hyphens, never underscores or spaces. A malformed `SKILL.md` raises `SkillLoadError` rather than being
skipped silently, so a typo fails loudly at startup. See AIMU's
[use skills](https://saxman.info/aimu/how-to/use-skills/) for the optional frontmatter fields.

`description` carries more weight than its length suggests: it is the only part of a skill in the
catalogue, so it is the whole basis on which the model decides whether to load the skill at all. Write
it as a trigger ("when you need to ..."), not a title.

## Three ways to add one

### Write the directory by hand

```
~/.kokua/data/skills/
└── weekly-digest/
    ├── SKILL.md
    └── scripts/
        └── collect_notes.py
```

A skill manager is built per conversation agent, so a skill you add by hand is picked up the next time an
agent is built: a new conversation, or an existing one whose cached agent has been evicted from the LRU.
The conversation you are sitting in keeps the catalogue already injected into its system message. Start a
new conversation, or restart Kokua, to be certain the skill is live.

### Ask the assistant to write it

The supervisor has an `author_skill` tool, and its system prompt tells it to reach for that tool when you
teach it a repeatable procedure worth remembering. So the shortest path is a sentence:

> "That worked. Save it as a skill called `weekly-digest` so you can do it the same way next Friday."

`author_skill(name, description, body)` writes the `SKILL.md` and refreshes the manager, so the skill is
usable in the same conversation. It **will not overwrite** an existing skill; to revise one, either edit
the file by hand or delete the directory first. It is not gated for approval, since it only writes
markdown.

### Attach a script

`add_skill_script(skill_name, filename, content)` writes `scripts/<filename>` (a `.py` or `.sh` file)
into a skill that already exists, then reloads the agent's skills so the new tool is callable in the same
turn.

- The tool it creates is named `{skill_name}__{stem}`, with the skill's slug used verbatim:
  `collect_notes.py` inside `weekly-digest` becomes `weekly-digest__collect_notes`.
- `collect_notes.py` and `collect_notes.sh` map to the *same* tool name, and the `.py` file wins. Give
  two scripts in one skill two different stems.
- **To fix a broken script, reuse the exact same filename.** That overwrites in place and the tool keeps
  its name. A different filename creates a second script and leaves the broken one callable.
- The skill must exist first. Calling it for an unknown skill returns the list of skills that do exist.
- It is **gated** by default (`[security] confirm_tools`), so each call waits for your `y/N` in the
  terminal or Allow/Deny in the web UI.

Scripts run as real subprocesses with your user's privileges and no sandbox. Their stdout becomes the
tool result. The catalogue lists script tool names inline, so the model can call one directly without
loading the skill's instructions first.

## Who gets skills, and who does not

The **supervisor** is an AIMU `SkillAgent`; every sub-agent worker is a plain `Agent`. Workers get no
skill catalogue, no `activate_skill`, and no script tools. This has a practical consequence for how you
write a skill:

Write the procedure from the supervisor's point of view, as a plan it carries out by delegating. The
supervisor has almost no domain tools of its own, so a skill body that says "search the web for X, then
write the file" describes work the supervisor cannot do. "Delegate the lookup to the `researcher` role,
then have the `coder` role write the file" describes work it can. Naming the role you expect is the
single most useful thing a skill body can do.

Skill *scripts* are the exception, and a deliberate one: a `{skill}__{stem}` tool is mounted on the
supervisor, so a script is the one way to give the supervisor a concrete capability of its own without
touching the core.

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

- [Set up a toolset](set-up-toolsets.md): how tools reach a *worker*, which is the other half of the
  picture a skill body needs.
- [Add an MCP service](add-mcp-services.md): the third source of capability.
- [Architecture](../explanation/architecture.md#the-supervisors-tools): the supervisor's full toolset.
- AIMU: [use skills](https://saxman.info/aimu/how-to/use-skills/) for `SkillAgent`, the catalogue, and
  the full `SKILL.md` reference.
