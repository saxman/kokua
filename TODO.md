# TODO / Backlog

Captured 2026-07-14; module references refreshed 2026-08-10. Backlog only, not yet scheduled. File references point at current code.

## 1. Surface model and configuration errors in the channel
Catch model-client build/switch failures (`core.settings_runtime.SettingsApplier.switch_model`, initial agent build),
settings/TOML parse errors (`config/file.py`), and invalid runtime settings, then route them to the
active `Channel` (`channels/cli.py`, `channels/web.py`) as a user-visible message instead of only
logging/raising. Today a bad model string or malformed config surfaces as a stack trace or a silent
failure rather than something the user sees in the chat.

## 2. Make session-level config overrides visible
When the web settings panel / `runtime-settings.json` (`config/table.py`) or a mid-session
`/model` switch overrides the default `AssistantConfig` (TOML / built-in), indicate to the user that
the active session differs from the baseline (which model, generation kwargs, or toggles are
overridden). Tie into `Assistant.current_settings` and the web history/approval frames.

## 3. Document SearXNG config and usage; consider promoting to config
SearXNG is currently only the `SEARXNG_BASE_URL` env var (default `http://localhost:8080`) read by
`aimu.tools.builtin.web_search`, and is undocumented in Kokua's README / CLAUDE.md /
`config.example.toml`. Document how the `web` tool group depends on it, and decide whether to add a
first-class key to `AssistantConfig` + `config.example.toml` instead of a bare env var.

Note: fix may belong upstream in the editable `../aimu` sibling rather than Kokua.

## 4. Add strictness and max-iterations controls to reviewers
`planning/reviewers.py` hardcodes `max_iterations=6`; `AssistantConfig.review_rounds` defaults to 2 and there is
no strictness dial. Expose reviewer `max_iterations` and a `strictness` setting (adjusting the
reviewer prompt / verdict threshold) through `AssistantConfig` + `config.example.toml`, and thread
them into `planning.reviewers._reviewer_agent` and the plan/result review loops.

## 5. Add "run a named skill script" as a scheduled-task action
The scheduled-task system (see the spec under `docs/superpowers/specs/`) fires a natural-language
prompt at the agent when a task is due (via `core.turns.TurnRunner.proactive`). Add a second action type that
runs an existing `SkillAgent` skill script by name instead of a free-form prompt, so a task can invoke
authored, deterministic behavior rather than a generated turn. Decide how a task record distinguishes
the two actions and how a missing/renamed skill is handled at fire time.

## 6. Determine if and how to build a hierarchy for the kokua package
**Resolved by the 2026-08 modularization.** `src/kokua` is now grouped by subsystem: `core/`,
`config/`, `planning/`, `mcp/`, `scheduling/`, beside the existing `channels/`, `frontends/`,
`toolpacks/`. No entry-point paths changed. See
[docs/explanation/architecture.md](docs/explanation/architecture.md) for the layout and
[CONTRIBUTING.md](CONTRIBUTING.md) for where a new module goes.

## 7. Consider upstreaming `next_fire` recurrence math to AIMU
`scheduling.recurrence.next_fire(schedule, now)` is pure, stateless, provider-agnostic scheduler math (seconds
until the next once/interval/daily/weekly occurrence). It's the one piece of the scheduling stack that
could reasonably live beside AIMU's `aio.Scheduler` as a generic helper (or a small `Recurrence` type).
The rest stays in kokua by AIMU's own boundary: the `Scheduler` docstring puts persistence and durable
cron-like scheduling in "a wrapper above the library," so the JSON registry, `scheduling.tools.make_scheduler_tools`,
and the `_proactive` firing (all app policy, coupled to kokua's `Assistant`) belong here. Defer until a
second AIMU consumer actually needs durable scheduling; upstreaming for one consumer is speculative
generality. `next_fire` bakes in opinions (four schedule types, local tz, `None` for a past one-shot,
weekly semantics), so any upstream move is a judgment call about whether AIMU wants that shape.

## 8. Add a model-client request timeout (deferred pending recurrence)
The model client is built with no request timeout: `core.build.build_model_client` calls
`aio.client(config.model, ...)` without `timeout=`, and the async providers only apply a timeout when
one is passed (e.g. `AsyncOllamaClient` at `aimu/aio/providers/ollama.py`). A stalled backend can
therefore block a turn indefinitely; if the process is killed mid-turn the transcript persists nothing,
leaving a conversation that ends on a `user` message with no assistant reply and no error (the shape
seen once in session 1 during the 2026-07-16 empty-turn investigation, but never reproduced).

Deferred by decision until it recurs and can be diagnosed live, rather than fixing an inferred symptom.
Design already scoped: a single `AssistantConfig.request_timeout` (seconds) threaded into `aio.client`
covers all network providers (Ollama, Anthropic, OpenAI + the openai-compat family, Gemini all accept
`timeout`); it must be withheld from the in-process `hf:` / `llamacpp:` providers, which take no
`timeout`. Because Kokua streams, an httpx `timeout` acts as a per-chunk *stall* timeout, so mind large
local-model cold-start (time to first token) when picking a default. Open question left for diagnosis:
default value vs. opt-in `None`. When it recurs, capture (before restart) what was on screen (was a
tool call streaming? which one?) and whether the backend was responsive, plus the persisted state.

## 9. Don't cancel the in-flight turn when the user switches conversations
**Resolved by the agent-per-thread refactor (merged 2026-07-22).** Each conversation owns its agent
and model client (`core/agent_registry.py`); switching no longer cancels the running turn. A
backgrounded turn persists to its own conversation, streams muted, and posts a completion
notification; approval is foreground-gated, so background and proactive turns auto-deny. The single
shared lock was replaced by `TurnGate`. The invariants that make this safe are documented at the top
of `core/turns.py`.

## 10. A message can bind to the wrong conversation if the user switches immediately after sending
A reactive turn is bound to a conversation at the moment `_serve_channel` dequeues its message
(`conversation_id = self._active_id` at submit), not at the moment the message was enqueued. The web
front end feeds a chat message onto the channel queue (`channel.feed`) but handles `new`/`select`/
`delete` controls inline in `frontends/web.py`'s `pump()` (which mutate `self._active_id`). So if a
`new`/`select` control is processed in the window between `feed` and the serve loop's dequeue, the turn
binds to the conversation switched *to*, and its reply renders there instead of where the user typed it.

The window is sub-millisecond (the serve loop dequeues on the next loop tick), so a human can't
realistically trigger it; it was surfaced deterministically by the Playwright e2e suite
(`tests/frontends/test_web_e2e.py`), which now waits for the turn to be observably running before switching to
avoid the race rather than assert the bug. Low priority. Fix direction: bind the message to the active
conversation at *enqueue* time -- e.g. capture `_active_id` when feeding and carry it on the queued
message, or route control frames (`new`/`select`/`delete`) through the same inbound queue as messages
so their ordering relative to a just-sent message is preserved. Introduced by the agent-per-thread
refactor (Phase B); before it, one shared agent meant every turn used the currently-active state anyway.

## 11. Bound the growth of a `target="task"` scheduled-task conversation
A scheduled task with `target="task"` (see `scheduling/` / `core.turns.TurnRunner.proactive` (target="task")) reuses one
dedicated conversation across every firing, so its history grows without limit and each firing replays
the full, growing transcript to the model. That is the intended continuity tradeoff, but for a
high-frequency or long-lived task it means steadily rising token cost and, eventually, hitting the
model's context window. Decide on a mitigation: e.g. cap/trim the reused conversation (drop or
summarize older firings), roll over to a fresh conversation past a size threshold, or expose the choice
per task. No cap exists today.

## 12. Change the default model to a local model
`config.example.toml` already documents the fallback as "$AIMU_LANGUAGE_MODEL / a local model", but
`AssistantConfig.model` defaults to `None`. Verify what `None` actually resolves to at agent-build
time, then make a concrete local model the effective default. Update `config.example.toml`, README,
and tests/mocks.

Watch-out: CI and mock-only tests run without `../aimu` or a local model available
(see CLAUDE.md). A local default must not cause a real client to be instantiated at import/build time.

Recovered 2026-08-11 from a 2026-07-14 snapshot that was never committed; renumbered from 4 to 12.
