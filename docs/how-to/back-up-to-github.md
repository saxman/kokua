# Back up to GitHub

Kokua accumulates state you would not want to lose: the memory store it writes facts into, the
documents it saves, the skills you taught it, and every conversation transcript. All of it lives under
one directory you own (`$KOKUA_HOME`, by default `~/.kokua`), which makes it easy to copy and easy to
forget to copy.

The `github_backup` toolset gives an agent one tool, `backup_kokua_state`, that copies that state into a
**private** GitHub repository as a git commit. You can ask for a backup in chat, or let a scheduled task
run one at 3am. When nothing has changed since the last run it makes no commit at all, so a daily task
does not fill the history with identical entries.

This guide sets that up end to end. By the end you will have a private repository holding a real commit
of your state, and a task that adds to it unattended.

## What is copied, and what is not

The list is an explicit allowlist in
[`toolsets/github_backup.py`](../../src/kokua/toolsets/github_backup.py), not a walk of the state
directory. The repository layout mirrors `$KOKUA_HOME`, so restoring is copying the files back with
nothing to interpret.

| In `$KOKUA_HOME` | In the repository | What it is |
| --- | --- | --- |
| `config.toml` | `config.toml` | your whole configuration, including `[agents.*]` and your scheduled tasks |
| `data/sessions.json` | `data/sessions.json` | every conversation transcript, task conversations included |
| `data/memory/` | `data/memory/` | the memory store (a Chroma database) |
| `data/documents/` | `data/documents/` | the document store |
| `data/skills/` | `data/skills/` | your skills, both authored and installed |

Everything else under `data/` is left out on purpose:

| Not copied | Why |
| --- | --- |
| `data/logs/` | rotating, noisy, and rewritten on every run |
| `data/downloads/` | generated artifacts such as rendered PDFs |
| `data/images/` | generated and attached images, which are binary and bulky |
| `data/mcp-oauth/` | the OAuth tokens Kokua holds for your MCP servers, which are credentials |
| `data/backup/` | the working tree the backup itself pushes from |
| `.git/` and `__pycache__/` inside any copied directory | tooling artifacts, not state to restore |

No credential is copied. Kokua keeps none in `config.toml` to begin with (your model API keys, your
mail password, and the backup token itself are all environment variables, and an MCP server's
`token_env` names a variable rather than holding its value), and the one place it does persist a
credential, `data/mcp-oauth/`, is excluded above. `config.toml` does still describe you (your address,
the servers you connect to, what your scheduled tasks say), and in any case that is not the reason the
repository has to be private. Your memory, your documents, and your transcripts are.

A copy that only ever grows is not a copy, so each run *replaces* the destinations above rather than
merging into them: delete a document and it disappears from the next backup too. Anything else you put
at the repository root, a `README.md` of your own for instance, is never touched.

Two things to know about the memory store in particular. Nothing pauses it for the copy, so a memory
written while the backup is running can land in the repository as an internally inconsistent snapshot of
the database; a restore from that commit may lose the most recent facts, or in the worst case need the
store rebuilt. Run the task at an hour when you are not using Kokua, and treat any single commit as
likely-good rather than guaranteed-good. It is also a binary file: git cannot store a small delta of it,
so every day the memory changes recommits the whole database. A store of 50 MB backed up daily is
roughly 50 MB of repository growth per changed day, which is worth a glance at your repository size
after a few months.

## 1. Create a private repository

On GitHub, create a new repository and set its visibility to **Private**. Initializing it with a README
is fine, and so is leaving it empty; Kokua fetches first and fast-forwards either way.

**Kokua checks the visibility before it pushes anything, and refuses a public repository.** It asks the
GitHub API and requires the answer to be literally private: a repository it cannot confirm is treated as
one it must not write to. This is not advice you can override with a flag, because a backup of your
memory, your documents, and every conversation you have had is not something a typo should be able to
publish.

Note the default branch GitHub gave you. If it is not `main`, you will set `[github_backup].branch` in
step 4.

## 2. Create a token scoped to that repository alone

Under **Settings > Developer settings > Personal access tokens > Fine-grained tokens**, create a token
with:

- **Repository access:** Only select repositories, and select **only** the backup repository.
- **Permissions:** `Contents: Read and write`. Nothing else is needed.

**The scope of this token is the blast radius of the whole capability.** `[github_backup].repo` is an
ordinary config key, which means the assistant can change it with `update_config` (unlike `[agents.*]`,
a toolset's settings section cannot declare itself hand-edit-only). What makes that acceptable is that
the token's name is fixed in code rather than configurable: a repointed `repo` can still only reach a
repository the one token already writes. Scope the token narrowly and you have bounded where a backup
can ever go.

Copy the token now; GitHub will not show it again.

## 3. Export `GITHUB_BACKUP_TOKEN`

```bash
export GITHUB_BACKUP_TOKEN="github_pat_..."
```

Put that line in your shell profile, or in the launcher script you start Kokua from. **Never put the
token in `config.toml`.** It is not a config key at all, and there is nowhere in the file to write it.

It never reaches a command line either. Kokua hands git a credential helper that *names* the variable
rather than carrying its value, so the token appears in no `ps` output and is never written into
`data/backup/.git/config`. If you want to confirm that after your first run, read that file: it holds a
remote URL and no credential.

## 4. Point Kokua at the repository

In `config.toml`:

```toml
[github_backup]
repo = "you/kokua-backup"
# branch = "main"        # the default; set it if your repository's default branch differs
```

`repo` is required. With it blank the toolset offers no tool at all, the same gate the `image` toolset
applies to its model environment variable, so a default install never shows the model a backup tool it
has nowhere to send. The assistant is told as much: the toolset's guidance names this key, so an agent
holding the toolset with `repo` blank asks you to set it rather than inventing another way to copy the
files.

**Neither key applies live**, so restart Kokua after editing either. Neither is declared a hot
setting, which means an `update_config` write reaches the file but never the `AssistantConfig` the
running process is holding; `repo` is read once on top of that, when an agent's tools are assembled.

## 5. Give an agent the toolset

A capability is declared, never defaulted, so installing the toolset is not enough. Add
`"github_backup"` to the `tools` list of the agent you talk to. Add, not replace: every name in that
list is valid on its own, so dropping one loses a capability silently rather than failing at startup.

```toml
[agents.assistant]
# what `kokua config init` writes, with one name appended
tools = ["memory", "documents", "skills", "config", "mcp-admin", "scheduling", "conversations",
         "planning", "capabilities", "time", "github_backup"]
```

`[agents.*]` is **hand-edit only** by design: `update_config` refuses the whole section by prefix,
because the assistant holds that tool and a writable agent table would let it widen its own reach. So
this edit is yours to make in the file, and it needs the same restart step 4 does.

Confirm the name reached your install's namespace:

```bash
uv run kokua --list-toolsets | grep github_backup
```

It appears there whether or not `repo` is set, since what `repo` gates is the *tool*, not the toolset.
The direct check that the tool exists is the next step.

## 6. Ask for a backup

Start Kokua and ask:

> "Back up your state."

You should get back one sentence naming the repository, the branch, the number of files changed, and the
short commit SHA:

```
Backed up to you/kokua-backup@main: 47 files changed, commit 3f2a1bc.
```

Refresh the repository on GitHub and the commit is there, authored as "Kokua", with your `config.toml`
and `data/` laid out exactly as they are on disk.

Ask a second time without doing anything in between and you get:

```
Nothing to back up: Kokua's state is unchanged since the last backup (3f2a1bc).
```

That is the empty-diff check working. A history where every entry is identical cannot answer the one
question a backup history exists to answer, which is when something actually changed.

Occasionally you will see a third answer instead:

```
Kokua's state is unchanged, but commit 3f2a1bc had not reached you/kokua-backup@main; pushed it now.
```

That means an earlier run committed but its push did not land (an expired token, or no network at 3am),
so the commit sat on this machine. Kokua compares what it has against what the remote is known to have
before it tells you a backup is current, and pushes the difference rather than reporting a local-only
commit as a completed backup. If the push fails again you get the failure, not this sentence.

## 7. Run it on a schedule

Backups you have to remember are backups you will forget. Add a task to `config.toml`:

```toml
[scheduling.task.kokua-backup]
prompt = "Back up Kokua's state with the backup_kokua_state tool, and tell me the result in one line."
schedule = { type = "daily", at = "03:00" }
```

Restart, and the task arms itself. Each firing opens its own conversation, nested under the task in the
web sidebar, so the one-line result is there to read whenever you look. (Asking the assistant to
schedule it works too, if its table names `scheduling`: `schedule_task` writes the same block and arms
it there and then, with no restart.)

This works because **`backup_kokua_state` takes no arguments**, and that is the whole design. A
scheduled task fires a *proactive* turn, and a proactive turn auto-denies every approval-gated tool,
since there is nobody watching to answer the prompt. A backup tool that took a `repo` or a `path`
argument would have to be gated (it could be pointed anywhere), and a gated backup tool cannot be
scheduled. Because the repository, the branch, and the file list all come from `config.toml`, there is
nothing a per-call approval would be protecting, which is what earns this tool a place outside
`[security].confirm_tools`.

Two things follow that are worth knowing before you leave it running:

- **It will never hang waiting for a credential.** Kokua sets `GIT_TERMINAL_PROMPT=0`, so a git command
  that cannot authenticate fails and reports instead of blocking on a prompt nobody will answer. Failing
  is the required behavior here; hanging is not.
- **It will never force-push.** See the diverged-remote entry under troubleshooting.

Prefer `{ type = "interval", seconds = 3600 }` or a weekly schedule if a day feels wrong; see
[the configuration reference](../reference/configuration.md#schedulingtaskname) for the accepted forms.

## Excluding anything further

Commit a `.gitignore` at the **root of the backup repository**. Kokua stages with `git add -A`, which
honours it, and the mirror only ever replaces the five destinations it copies, so your
`.gitignore` survives every run. Add it before your first backup where you can: git keeps tracking a
file it is already tracking, ignore rule or not, so excluding something after the fact also takes a
`git rm --cached` in `data/backup`.

The exclusions you write in the repository are honoured. The ones you wrote years ago for your own
development machine are not, and that is deliberate: Kokua clears `core.excludesFile` for its own git
calls, so a pattern in your *global* ignore file (a stray `*.sqlite3`, say) cannot silently drop part
of the memory store from a backup that still reports success with a plausible-looking file count.
Git's own per-repository `.git/info/exclude` is untouched and still applies, if you use it.

## Restoring

Restore is **manual, and deliberately so.** Overwriting your live state is not something an assistant
should be able to do to you by misreading a sentence, so there is no restore tool. Clone the
repository somewhere scratch:

```bash
git clone https://github.com/you/kokua-backup.git /tmp/kokua-restore
```

Then, **with Kokua stopped**, copy the two things back:

```bash
cp /tmp/kokua-restore/config.toml ~/.kokua/config.toml
cp -R /tmp/kokua-restore/data/. ~/.kokua/data/
```

Start Kokua and the documents, skills, and conversations are as they were at that commit. The memory
store is too, with the caveat above: it was copied live, so a commit written during a memory write can
hold a database missing its most recent writes. If Kokua reports a memory problem after a restore, try
the previous commit.

Three notes:

- **Stop Kokua first.** The memory store is a live database; copying over it in place while a process
  has it open is how you corrupt it.
- **`cp -R` merges.** Your `data/logs/`, `data/images/`, and `data/downloads/` are untouched, since the
  backup has no copy of them to write over yours. If you want an exact restore, move the old `data/`
  aside first instead.
- **Restoring an older commit** is `git checkout <sha>` in the clone before you copy. Every backup is an
  ordinary commit, so `git log` is your list of restore points.
- **Your MCP servers will need re-authenticating.** Their OAuth tokens live in `data/mcp-oauth/`, which
  is not backed up (they are credentials, and the whole point of the token discipline here is that
  credentials do not go into the repository). Their `[[mcp.server]]` entries come back with
  `config.toml`, so you reconnect and authorize once each, rather than reconfiguring anything.

## Troubleshooting

Every failure comes back as one sentence beginning `Backup failed:`, never a traceback, because the turn
that hits it is often an unattended one with nobody to read a stack. The messages below are what those
sentences say.

| Message | What it means |
| --- | --- |
| `git is not installed, or not on PATH` | Kokua shells out to the real `git` binary. Install it, and make sure it is on the `PATH` of the process Kokua runs in (a launcher script's environment can differ from your interactive shell's). |
| `the $GITHUB_BACKUP_TOKEN environment variable is not set` | Step 3, or the export did not reach Kokua's process. Restart it from a shell where `echo $GITHUB_BACKUP_TOKEN` prints something. |
| `no repository is configured. Set [github_backup].repo` | `repo` is blank. What you are more likely to hit is no backup tool at all, since a blank `repo` at startup means the toolset builds nothing and the assistant will simply say it has no such tool. Either way, step 4, and restart: the key is not hot. |
| `[github_backup].repo is '...', not a valid 'owner/name' repository` | The value is not a plain `owner/name` pair, or it contains whitespace or a control character. It is checked for shape before any URL or header is built, so a bad value fails here rather than deeper in. |
| `repository '...' is not confirmed private` | GitHub did not answer that this repository is private. Change the repository's visibility to Private, or point `repo` at one that is **and delete `data/backup`** (see the row below). Kokua fails closed here: an answer it cannot read as private is treated as not private. |
| `GitHub rejected $GITHUB_BACKUP_TOKEN` | HTTP 401. The token is wrong, revoked, or expired. Fine-grained tokens expire; generate a new one. |
| `repository '...' was not found. Either it does not exist, or $GITHUB_BACKUP_TOKEN does not grant access to it` | HTTP 403 or 404, and GitHub answers identically for both, which is why the message names both. The usual cause is a token scoped to a different repository, or one missing `Contents: Read and write`. |
| `GitHub redirected '...' instead of answering` | **Your repository was probably renamed** (or transferred), and GitHub is answering with a redirect to its new location. Update `[github_backup].repo` to the new `owner/name`, delete `data/backup`, and restart. Kokua refuses to follow the redirect rather than following it: the default redirect handling replays every header, `Authorization` included, at whatever host the response names, which is a credential leak waiting for a bad `Location`. |
| `the backup working tree ... pushes to ..., but [github_backup].repo now names ...` | **You repointed `repo`, and `data/backup` still tracks the old repository.** Kokua refuses rather than pushing to one repository while checking that a different one is private, which would report the new name, push to the old one, and leave the move silently undone. Delete `data/backup` and the next backup clones the new repository from scratch. It will not carry the old repository's history across; if you want that, push it yourself before you delete the tree. |
| `GitHub's response for '...' could not be read` | The response was truncated, stalled past 30 seconds, or was not valid JSON. Nearly always transient; try again. Kokua treats an unreadable answer as a refusal rather than reading it charitably, since this is the check standing between your transcripts and a public repository. |
| `could not reach api.github.com: ...` | Network or DNS. The reason from the socket layer is included. |
| `could not prepare the backup working tree: ...` | A filesystem error while cloning or mirroring. Only one cause is transient: a live Chroma write-ahead file that vanished between being listed and being copied, where the next run usually succeeds. A permission problem, a full disk, or a stray file where `data/backup` should be will fail every run until you fix it, so if you see this twice, read the rest of the message rather than waiting. (A dangling symlink under `data/documents` or `data/skills` used to belong on this list; it is now skipped, since one stale link should not stop your backups.) |
| `git push failed: ! [rejected] ...` | **The remote has commits your backup tree does not.** Kokua never passes `--force`: a mirror that can overwrite remote history is not a backup, and reconciling a divergence is your call, not a tool's. Sort it out by hand in `data/backup` (`git pull --rebase`, or inspect and reset), or, if you would rather start clean, delete `data/backup` and let the next run re-clone. |
| `git <step> timed out after 300 seconds` | A very large first push, or a stalled network. The step is named so you can tell a fetch from a push. |
| `the backup working tree ... does not exist` | Something removed `data/backup` mid-run. The next run re-creates it. |

Three more things that are working as intended rather than broken:

- **`Nothing to back up`** is a success. Nothing changed, so nothing was committed.
- **Your machine's git configuration is ignored** for these commands, by design. Beyond
  `core.excludesFile`, Kokua also sets `commit.gpgsign=false` (a machine that signs every commit would
  otherwise fail every unattended backup, with no tty to enter a passphrase into) and pins the locale to
  `LC_ALL=C`. The locale pin is why git's own lines in the messages above are always in English: Kokua
  matches on those strings to tell an empty remote from a real failure, and a translated build would
  break the match silently.
- **The commit identity is Kokua's own** (`Kokua <kokua@localhost>`), passed per command. The machine
  running the backup needs no git identity configured at all.

## See also

- [Set up a toolset](set-up-toolsets.md): how any capability reaches any agent, and why declaring it is
  always the last step.
- [Configuration reference](../reference/configuration.md#github_backup): the `[github_backup]` keys, and
  [`[scheduling.task.*]`](../reference/configuration.md#schedulingtaskname) for the task block.
- [`toolsets/github_backup.py`](../../src/kokua/toolsets/github_backup.py): one module, with the
  reasoning behind each refusal above written into its docstrings.
