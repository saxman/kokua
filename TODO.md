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

## 4. Change the default model to a local model
`config.example.toml` already documents the fallback as "$AIMU_LANGUAGE_MODEL / a local model", but
`AssistantConfig.model` defaults to `None`. Verify what `None` actually resolves to at agent-build
time, then make a concrete local model the effective default. Update `config.example.toml`, README,
and tests/mocks.

Watch-out: CI and mock-only tests run without `../aimu` or a local model available
(see CLAUDE.md). A local default must not cause a real client to be instantiated at import/build time.

## 5. Add strictness and max-iterations controls to reviewers
`review.py` hardcodes `max_iterations=6`; `AssistantConfig.review_rounds` defaults to 2 and there is
no strictness dial. Expose reviewer `max_iterations` and a `strictness` setting (adjusting the
reviewer prompt / verdict threshold) through `AssistantConfig` + `config.example.toml`, and thread
them into `_reviewer_agent` and the plan/result review loops.
