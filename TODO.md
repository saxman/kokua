# TODO / Backlog

Captured 2026-07-14. Backlog only, not yet scheduled. File references point at current code.

## 1. Surface model and configuration errors in the channel
Catch model-client build/switch failures (`assistant._switch_model`, initial agent build),
settings/TOML parse errors (`settings.py`), and invalid runtime settings, then route them to the
active `Channel` (`channels/cli.py`, `channels/web.py`) as a user-visible message instead of only
logging/raising. Today a bad model string or malformed config surfaces as a stack trace or a silent
failure rather than something the user sees in the chat.

## 2. Make session-level config overrides visible
When the web settings panel / `runtime-settings.json` (`runtime_settings.py`) or a mid-session
`/model` switch overrides the default `AssistantConfig` (TOML / built-in), indicate to the user that
the active session differs from the baseline (which model, generation kwargs, or toggles are
overridden). Tie into `assistant.get_runtime_settings` and the web history/approval frames.

## 3. Document SearXNG config and usage; consider promoting to config
SearXNG is currently only the `SEARXNG_BASE_URL` env var (default `http://localhost:8080`) read by
`aimu.tools.builtin.web_search`, and is undocumented in Kokua's README / CLAUDE.md /
`config.example.toml`. Document how the `web` tool group depends on it, and decide whether to add a
first-class key to `AssistantConfig` + `config.example.toml` instead of a bare env var.

Note: fix may belong upstream in the editable `../aimu` sibling rather than Kokua.

## 4. Add strictness and max-iterations controls to reviewers
`review.py` hardcodes `max_iterations=6`; `AssistantConfig.review_rounds` defaults to 2 and there is
no strictness dial. Expose reviewer `max_iterations` and a `strictness` setting (adjusting the
reviewer prompt / verdict threshold) through `AssistantConfig` + `config.example.toml`, and thread
them into `_reviewer_agent` and the plan/result review loops.

## 5. Add "run a named skill script" as a scheduled-task action
The scheduled-task system (see the spec under `docs/superpowers/specs/`) fires a natural-language
prompt at the agent when a task is due (via `Assistant._proactive`). Add a second action type that
runs an existing `SkillAgent` skill script by name instead of a free-form prompt, so a task can invoke
authored, deterministic behavior rather than a generated turn. Decide how a task record distinguishes
the two actions and how a missing/renamed skill is handled at fire time.

## 6. Determine if and how to build a hierarchy for the kokua package
The `src/kokua` package is currently flat: core, front ends, channels, tool packs, and helper modules
all sit near the top level. Assess whether growth (recent extractions like `mcp.py`, `messages.py`,
`build.py`) warrants grouping modules into subpackages (e.g. by concern or layer), and if so, decide
the structure and migration path. Weigh the churn to imports and entry-point paths against the
navigability gain; keep the core small either way.

## 7. Consider upstreaming `next_fire` recurrence math to AIMU
`scheduling.next_fire(schedule, now)` is pure, stateless, provider-agnostic scheduler math (seconds
until the next once/interval/daily/weekly occurrence). It's the one piece of the scheduling stack that
could reasonably live beside AIMU's `aio.Scheduler` as a generic helper (or a small `Recurrence` type).
The rest stays in kokua by AIMU's own boundary: the `Scheduler` docstring puts persistence and durable
cron-like scheduling in "a wrapper above the library," so the JSON registry, `make_scheduler_tools`,
and the `_proactive` firing (all app policy, coupled to kokua's `Assistant`) belong here. Defer until a
second AIMU consumer actually needs durable scheduling; upstreaming for one consumer is speculative
generality. `next_fire` bakes in opinions (four schedule types, local tz, `None` for a past one-shot,
weekly semantics), so any upstream move is a judgment call about whether AIMU wants that shape.

## 8. Add a model-client request timeout (deferred pending recurrence)
The model client is built with no request timeout: `build.build_model_client` calls
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
Switching conversations cancels a running reply. `Assistant.select_conversation` (and
`new_conversation` / `delete_conversation`) call `_cancel_current_turn()` before `agent.restore(...)`,
so a slow turn (e.g. 60s+ on a large local model) is lost when the user clicks away and back. This is a
safety interlock, not an arbitrary choice: there is **one shared agent + model client**, and a switch
mutates the two things a running turn depends on -- `agent.model_client.messages` (replaced by
`restore()`) and `self._session` (where `_persist()` writes on completion). Cancelling avoids
corrupting both.

Goal: let a turn keep running (and persist to its own conversation) while the user views/uses another.
Assessed as medium-high effort because the turn must be decoupled from the shared state a switch
mutates:
- **Turn-owned model client** captured at turn start (its own `messages`), so restoring the shared view
  can't corrupt it. This is the crux: AIMU binds tools to the *agent* and `agent.run()` uses
  `agent.model_client`, so a per-turn client effectively means a per-turn agent, and rebuilding the full
  tool set per turn (skills, MCP, memory, scheduler, subagent -- some mutating shared live state) is
  fiddly.
- **Persist to the captured session**, not `self._session`, then push a refreshed conversation list.
- **Approval / plan gates** are single-slot today (`self._pending_approval`, `self._pending_plan`) and
  assume one turn at a time; a backgrounded turn hitting a gated tool while the user is elsewhere would
  collide. Needs per-turn gating, or treat a backgrounded turn like a proactive one (auto-deny gated
  tools).
- **The turn lock** (`self._lock`) exists to protect the shared message list; its role changes once
  turns own their clients (and two turns could overlap, though a single local Ollama serializes them).

Cheaper partial wins if the full change is too much: (a) **confirm-before-cancel** in the web UI ("a
reply is still generating -- switch anyway?"), ~an hour, client-only, prevents accidental loss;
(b) **browse-without-committing** -- switching only changes the displayed history (read from the store)
and defers cancel/restore until the user actually sends in the other conversation. Related: this is a
follow-on to the multiple-conversations design under `docs/superpowers/specs/`. The real trigger is
often latency (TODO #8-adjacent): trimming the per-turn tool surface (e.g. the robinhood MCP server adds
~45 tools to every request) makes users less likely to wander off mid-turn.
