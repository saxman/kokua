# TODO / Backlog

Captured 2026-07-14; pruned and renumbered 2026-08-13 (three resolved items removed, three
release-hygiene items added). Backlog only, not yet scheduled. File references point at current code.

## 1. Make session-level config overrides visible
When the web settings panel (`config/table.py`'s `RUNTIME_SETTINGS`) or a mid-session `/model` switch
overrides the baseline `AssistantConfig` (TOML / built-in), indicate to the user that the active session
differs from that baseline (which model, generation kwargs, or toggles are overridden). Tie into
`Assistant.current_settings` and the web history/approval frames.

## 2. Document SearXNG config and usage; consider promoting to config
SearXNG is currently only the `SEARXNG_BASE_URL` env var (default `http://localhost:8080`) read by
`aimu.tools.builtin.web_search`, and is undocumented in Kokua's README / CLAUDE.md /
`config.example.toml`. Document how the `web` tool group depends on it, and decide whether to add a
first-class key to `AssistantConfig` + `config.example.toml` instead of a bare env var.

Note: fix may belong upstream in the editable `../aimu` sibling rather than Kokua.

## 3. Add strictness and max-iterations controls to reviewers
`planning/reviewers.py` hardcodes `max_iterations=6`; `AssistantConfig.review_rounds` defaults to 2 and
there is no strictness dial. Expose reviewer `max_iterations` and a `strictness` setting (adjusting the
reviewer prompt / verdict threshold) through `AssistantConfig` + `config.example.toml`, and thread them
into `planning.reviewers._reviewer_agent` and the plan/result review loops.

Related: `REVIEWER_TOOLS` is now web + `calculate` + the clock, so a reviewer that needs more than
arithmetic to verify a claim will note it as unverified. If that turns out to be too weak in practice,
the fix is a sandboxed executor, not re-adding `execute_python` -- a reviewer cannot be approval-gated.

## 4. Add "run a named skill script" as a scheduled-task action
The scheduled-task system fires a natural-language prompt at the agent when a task is due (via
`core.turns.TurnRunner.proactive`). Add a second action type that runs an existing `SkillAgent` skill
script by name instead of a free-form prompt, so a task can invoke authored, deterministic behavior
rather than a generated turn. Decide how a task record distinguishes the two actions and how a
missing/renamed skill is handled at fire time.

## 5. Consider upstreaming `next_fire` recurrence math to AIMU
`scheduling.recurrence.next_fire(schedule, now)` is pure, stateless, provider-agnostic scheduler math
(seconds until the next once/interval/daily/weekly occurrence). It's the one piece of the scheduling
stack that could reasonably live beside AIMU's `aio.Scheduler` as a generic helper (or a small
`Recurrence` type). The rest stays in Kokua by AIMU's own boundary: the `Scheduler` docstring puts
persistence and durable cron-like scheduling in "a wrapper above the library," so the JSON registry,
`scheduling.tools.make_scheduler_tools`, and the proactive firing (all app policy, coupled to Kokua's
`Assistant`) belong here. Defer until a second AIMU consumer actually needs durable scheduling;
upstreaming for one consumer is speculative generality. `next_fire` bakes in opinions (four schedule
types, local tz, `None` for a past one-shot, weekly semantics), so any upstream move is a judgment call
about whether AIMU wants that shape.

## 6. Add a model-client request timeout (deferred pending recurrence)
The model client is built with no request timeout: `core.build.build_model_client` calls
`aio.client(config.model, ...)` without `timeout=`, and the async providers only apply a timeout when one
is passed (e.g. `AsyncOllamaClient` at `aimu/aio/providers/ollama.py`). A stalled backend can therefore
block a turn indefinitely; if the process is killed mid-turn the transcript persists nothing, leaving a
conversation that ends on a `user` message with no assistant reply and no error (the shape seen once in
session 1 during the 2026-07-16 empty-turn investigation, but never reproduced).

Deferred by decision until it recurs and can be diagnosed live, rather than fixing an inferred symptom.
Design already scoped: a single `AssistantConfig.request_timeout` (seconds) threaded into `aio.client`
covers all network providers (Ollama, Anthropic, OpenAI + the openai-compat family, Gemini all accept
`timeout`); it must be withheld from the in-process `hf:` / `llamacpp:` providers, which take no
`timeout`. Because Kokua streams, an httpx `timeout` acts as a per-chunk *stall* timeout, so mind large
local-model cold-start (time to first token) when picking a default. Open question left for diagnosis:
default value vs. opt-in `None`. When it recurs, capture (before restart) what was on screen (was a tool
call streaming? which one?) and whether the backend was responsive, plus the persisted state.

## 7. A message can bind to the wrong conversation if the user switches immediately after sending
A reactive turn is bound to a conversation at the moment `_serve_channel` dequeues its message
(`conversation_id = self._active_id` at submit), not at the moment the message was enqueued. The web
front end feeds a chat message onto the channel queue (`channel.feed`) but handles `new`/`select`/
`delete` controls inline in `frontends/web.py`'s `pump()` (which mutate `self._active_id`). So if a
`new`/`select` control is processed in the window between `feed` and the serve loop's dequeue, the turn
binds to the conversation switched *to*, and its reply renders there instead of where the user typed it.

The window is sub-millisecond (the serve loop dequeues on the next loop tick), so a human can't
realistically trigger it; it was surfaced deterministically by the Playwright e2e suite
(`tests/frontends/test_web_e2e.py`), which now waits for the turn to be observably running before
switching to avoid the race rather than assert the bug. Low priority. Fix direction: bind the message to
the active conversation at *enqueue* time -- e.g. capture `_active_id` when feeding and carry it on the
queued message, or route control frames (`new`/`select`/`delete`) through the same inbound queue as
messages so their ordering relative to a just-sent message is preserved.

## 8. Bound the growth of a `target="task"` scheduled-task conversation
A scheduled task with `target="task"` (see `scheduling/` / `core.turns.TurnRunner.proactive`) reuses one
dedicated conversation across every firing, so its history grows without limit and each firing replays
the full, growing transcript to the model. That is the intended continuity tradeoff, but for a
high-frequency or long-lived task it means steadily rising token cost and, eventually, hitting the
model's context window. Decide on a mitigation: e.g. cap/trim the reused conversation (drop or summarize
older firings), roll over to a fresh conversation past a size threshold, or expose the choice per task.
No cap exists today; it is listed under Known limitations in `CHANGELOG.md`.

## 9. Change the default model to a local model
`config.example.toml` already documents the fallback as "$AIMU_LANGUAGE_MODEL / a local model", but
`AssistantConfig.model` defaults to `None`. Verify what `None` actually resolves to at agent-build time,
then make a concrete local model the effective default. Update `config.example.toml`, README, and
tests/mocks.

Watch-out: CI and the mock-only tests run without `../aimu` (they use `uv sync --no-sources` / a git
install of AIMU) and without a local model available. A local default must not cause a real client to be
instantiated at import or build time.

## 10. Decide how releases get published
CI builds the sdist and wheel and verifies the wheel installs and runs from PyPI-resolved dependencies,
but nothing publishes. Nothing blocks it any more -- `aimu` 0.13.1 is on PyPI, so the pin is satisfiable
for an ordinary `pip install kokua`. Decide whether 0.1.0 goes to PyPI, and if so add a tag-triggered
workflow using trusted publishing rather than a stored token. If Kokua stays source-installed for now,
say so in the README and leave the name unclaimed deliberately rather than by omission.

## 11. Decide whether to adopt ruff 0.16's default rule set
`[project.optional-dependencies] dev` pins `ruff>=0.15,<0.16` so CI's lint verdict matches the local
one. 0.16 widened the defaults considerably and reports 312 findings on a tree that lints clean under
0.15, of which 234 are auto-fixable. The bulk is mechanical (`UP045` non-pep604-annotation-optional at
130, `I001` unsorted-imports at 51, `UP035` deprecated-import at 25), but the remainder is worth reading
rather than fixing blind: 13 naive-datetime findings between `DTZ001` and `DTZ005`, six of them in
`scheduling/tools.py`, where local-time arithmetic is the intended semantics (a daily task fires at
09:00 where the user is); `B023` function-uses-loop-variable (7); `RUF059` unused-unpacked-variable
(35); and `BLE001` blind-except (7), spread across `core/turns.py`, `core/assistant.py`,
`core/diagnostics.py`, and `channels/web.py` -- the deliberate catch-and-report paths that exist so a
failure reaches the channel instead of killing a turn.

Decide as one change: adopt with an explicit `[tool.ruff.lint] select`, apply the auto-fixes in a
commit that does nothing else, and either fix or `noqa`-with-a-reason the judgment calls. Raising the
pin without that is how a linter upgrade turns into an unreviewed diff across the tree.

## 12. Add a SECURITY.md
The README's Security section is thorough about the threat model -- unsandboxed subprocesses with the
user's privileges, prompt injection crossing conversations -- but there is no `SECURITY.md`, so a
reporter has no stated channel or expectation. Add one covering how to report, what is in scope (the
approval gate, the config blocklist, path traversal in the email/download paths) and what is explicitly
out of scope (a trusted model running code you approved).
