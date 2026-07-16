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
