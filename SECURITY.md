# Security Policy

## Reporting a vulnerability

Report privately through GitHub: open this repository's **Security** tab and choose **Report a
vulnerability**. That opens a private advisory visible only to you and the maintainer.

Please do not open a public issue, pull request, or discussion for a suspected vulnerability, and
please do not post a working exploit anywhere public.

Kokua is maintained by one person, as a project for learning how agentic systems work. Reports are read
and answered on a best-effort basis. No response time is promised, and none should be inferred from
this document.

## Supported versions

Only the `main` branch. There is no published release yet, so there are no maintained older versions
and no backports.

## What Kokua is, before deciding what counts as a bug

Kokua runs a language model on your own machine with real capability. It can author and run Python and
shell scripts as ordinary subprocesses with your user's privileges and no sandbox, connect to remote
MCP servers, and call whatever tools those servers expose. That is the point of the program rather than
an oversight, and [README.md](README.md#security) says so plainly.

So the interesting question is never "can the assistant do something dangerous". It can, by design. The
question is whether it can do so *around* one of the barriers below, which exist so that the dangerous
things stay your decision.

## In scope

A report is in scope if it shows one of these barriers failing.

**The tool-approval gate.** `[security] confirm_tools` names the tools that need your confirmation
before each call. A gated tool that runs without approval is a vulnerability. So is a proactive,
scheduled, or backgrounded turn reaching a gated tool at all, since nobody is watching those and they
are supposed to auto-deny. Gating is by tool name and binds a sub-agent's call as much as the entry
agent's, so a worker slipping a gated call past you is in scope.

**The config write policy.** `[security] locked_config_keys` decides which keys the assistant's own
`update_config` may write, and the key holding that list is locked unconditionally so the assistant
cannot unlock itself in a single call. In scope: a write that lands on a locked key, any route into
`config.toml` that skips `config/store.py`'s `locked_by`, and any way for the assistant to widen its own
capability without a hand-edit you made. See [Who may change which
key](docs/reference/configuration.md#who-may-change-which-key).

**Agent capability boundaries.** An agent holds exactly the toolsets its `[agents.<name>].tools` names,
plus the delegate a non-empty `delegates_to` earns it. An agent or worker obtaining a tool its table
never declared is in scope.

**Path traversal in the web front end.** `/download/{name}` and `/images/{name}` serve files from
`$KOKUA_HOME/data/downloads` and `$KOKUA_HOME/data/images`. Anything that makes either route serve a
file from outside its folder is in scope.

**The email recipient lock.** `[email].to` is the only address Kokua can send to, and that lock is what
makes ungated sending safe. Anything that mails a different address is in scope.

**Secret disclosure.** `KOKUA_EMAIL_PASSWORD`, an MCP server's bearer token named by its `token_env`,
and `$GITHUB_BACKUP_TOKEN` are read from the environment and are never meant to reach `config.toml`, a
saved conversation, a log file, or a tool result, with one named exception: a variable you have
deliberately listed in `[compute] command_env_passthrough` is meant to reach a `run_command` result,
because listing it there is how you asked for exactly that. A path that writes one of them into any of
those, for a variable you did not list there, is in scope.

## Out of scope

These are not vulnerabilities. They are the program working as documented.

- **A model you configured running code you approved.** Unsandboxed execution with your privileges is
  the feature. If you approved the call, the outcome is yours.
- **Prompt injection that leads to an action you then approved.** Injection is a real and stated risk,
  and [README.md](README.md#security) describes it crossing conversations. Hardening against it is
  welcome as an ordinary issue. It becomes a security report only when it *defeats* a barrier above,
  rather than persuading you to open one.
- **Anything requiring an attacker who can already edit `config.toml` or run code as your user.** That
  file is the trust root: it decides which agents exist and what each one may call. Someone who can
  write it has already won, and no check inside Kokua can change that.
- **The web front end having no authentication.** It binds to `127.0.0.1` by default and is documented
  as having no auth layer. Binding it to a reachable interface hands the assistant to anyone who can
  reach the port. That is your call to make, and the [configuration
  reference](docs/reference/configuration.md#web) says so.
- **An MCP server you chose to connect doing what its tools do.** Kokua does not vet remote tools.
- **Third-party front ends and toolsets** installed through the `kokua.frontends` and `kokua.toolsets`
  entry points. They run as first-class code by design; report those to their authors.
- **A dependency advisory with no demonstrated path through Kokua.** A version bump is an ordinary
  issue.

## Tightening your own install

Three settings do most of the work, and all three are in
[`config.toml`](docs/reference/configuration.md):

- Add to `[security] confirm_tools` rather than removing from it. Adding `read_conversation` and
  `search_conversations` is the usual next step if cross-conversation injection worries you, at the
  cost of unattended scheduled runs that read history.
- Leave `agents.*` in `[security] locked_config_keys`. Removing it hands the assistant its own
  capability table.
- Keep `[web] host` on loopback unless you have put something in front of it.
