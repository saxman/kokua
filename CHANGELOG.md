# Changelog

## 0.1.0 (unreleased)

Initial release. Mopai starts from AIMU's `examples/personal-assistant/` and restructures it into an
installable, modular application.

- **Package**: `src`-layout `mopai` package with console scripts `mopai` (runs the selected front end) and
  `mopai-web`. Apache-2.0, Python 3.11+.
- **Assistant core** (`mopai.assistant`): the transport-agnostic `Assistant` wiring an AIMU `SkillAgent`
  with skill authoring + runnable skill scripts, persistent conversation history, a proactive scheduler,
  remote MCP servers (startup `--mcp` + runtime `add_mcp_server`), and persistent memory (a
  `SemanticMemoryStore` for facts + a `DocumentStore` for documents, on by default).
- **Plugin system** (`mopai.plugins`): front ends and tool-packs discovered via the `mopai.frontends` and
  `mopai.tools` entry-point groups. Built-in `cli` and `web` front ends and an `example` tool-pack are
  registered as plugins; third parties add their own by publishing a package. `--list-frontends`,
  `--list-tool-packs`, `--no-plugins`.
- **Front ends**: `cli` (terminal via AIMU's `CLIChannel`) and `web` (Starlette + uvicorn WebSocket server
  with a streaming browser UI, behind the `web` extra).
- **App-owned state**: all state under `~/.mopai` (override `MOPAI_HOME`), replacing the example's reliance
  on `aimu.paths.output`.
- **Tests**: mock-only suite (assistant wiring, CLI parsing, MCP, memory, web channel + server round-trip,
  plugin discovery), with a vendored async mock model client (no reach into the AIMU repo).
