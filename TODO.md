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

## 4. Change the default model to a local model — DONE (2026-07-14)
Verified that `None` already resolves to a local model, never to Anthropic/cloud: `config.model`
(default `None`) is passed to `aio.client(None)` (`assistant.py`), which calls AIMU's
`resolve_default_text_model(include_hf_cache=False)` — `AIMU_LANGUAGE_MODEL` env var, else a running
Ollama then a local OpenAI-compatible server, else raise; never a cloud provider, never downloads
weights. So the original "stop defaulting to cloud" concern was unfounded.

Chose to keep AIMU's probe-or-raise behavior (a hardcoded default would break whenever that exact
model is not pulled) and instead (a) document the real resolution order in `config.example.toml`,
`README.md`, and the `--model` help, and (b) surface the resolution failure cleanly: `Assistant.create`
wraps the build in `ModelClientError`, the CLI prints it + exits non-zero, and the web UI shows it in
chat and releases the busy guard, all instead of a traceback. See
`docs/superpowers/specs/2026-07-14-todo4-local-model-default-design.md`.

Left for item 1: general routing of all config/settings/build errors to the channel.

## 5. Add strictness and max-iterations controls to reviewers
`review.py` hardcodes `max_iterations=6`; `AssistantConfig.review_rounds` defaults to 2 and there is
no strictness dial. Expose reviewer `max_iterations` and a `strictness` setting (adjusting the
reviewer prompt / verdict threshold) through `AssistantConfig` + `config.example.toml`, and thread
them into `_reviewer_agent` and the plan/result review loops.

## 6. Continue slimming assistant.py: extract MCP connection management
Follow-up to the PlanRunner extraction (deep-planning moved to `planning.py`). The remaining MCP
helpers in `assistant.py` (`_ServerConnection`, `_connect_mcp`, `_attach_server`, `make_mcp_tools`,
`_looks_like_auth_required`) are already free functions but live in the core. Move them to a dedicated
`mcp.py` module alongside `mcp_auth.py` / `mcp_registry.py`. Near-mechanical: they touch only the
passed-in agent/connections list, not `Assistant` state.

## 7. Continue slimming assistant.py: extract message/image transcript helpers
Move the transcript/image helpers out of `assistant.py` into a `messages.py` module (or fold the
image ones into `images.py`): `_map_image_block_urls`, `_compact_message_images`,
`_expand_message_images`, `_message_text`, `_derive_title`. These are pure functions with no
`Assistant` coupling; `tests/test_images.py` already imports `_compact_message_images` /
`_expand_message_images` directly, so update those imports as part of the move.

## 8. Slim Assistant.create() into builder functions
`Assistant.create()` still does ~130 lines of tool/MCP/subagent assembly inline. Extract that wiring
into free builder functions (e.g. a `build.py` with `build_tools(config, agent, ...)` and
`reconnect_mcp_servers(...)`) so `create()` reads as a short orchestrator and the assembly is testable
in isolation. Do this after items 6 and 7, since the builders will call into the relocated `mcp.py`.
