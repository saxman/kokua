# Config file + consolidated data directory

Date: 2026-06-29

## Goal

Two related changes to how Mopai is configured and where it stores state:

1. Introduce an optional TOML **config file** with defaults defined for every setting, read at
   startup. CLI flags override the file; the file overrides built-in defaults.
2. **Consolidate** all transient and user-provided content (conversation history, semantic memory,
   documents, authored skills) under a single `data/` directory inside the app's state root.

## Non-goals

- Auto-generating/writing a config file on first run. The file is read-only-if-present; a
  documented `config.example.toml` ships with the repo instead.
- Per-path CLI/config knobs for each store. A single root is configurable; the four leaf paths are
  derived.
- Any change to the assistant's runtime behavior, tools, or front ends beyond where settings come
  from and where files live.

## Directory layout

```
$MOPAI_HOME (default ~/.mopai)/
  config.toml            # optional; read if present
  data/
    history.json
    memory/
    documents/
    skills/
```

Defined in `mopai/paths.py`:

- `state_dir()` -> `$MOPAI_HOME` if set, else `~/.mopai`. Root. Configurable only via the env var
  (it must be known before the config file, which lives inside it, can be read).
- `data_dir()` -> `state_dir() / "data"`.
- `config_path()` -> `state_dir() / "config.toml"`.
- Leaf paths derived under `data_dir()`: `history_path()`, `memory_dir()`, `documents_dir()`,
  `skills_dir()`.

### Migration

`paths.migrate_legacy_layout()`: if old-layout entries exist directly under `state_dir()`
(`history.json`, `memory/`, `documents/`, `skills/`) and `data/` does not yet exist, create `data/`
and move them in, once. Called from `resolve_config(args)` before any store opens, against the
resolved `data_dir`'s parent (so a `[paths].data_dir` override outside `state_dir` skips migration).
Without it, existing users silently lose history and memory. No-op when `data/` already exists or
there is nothing to move.

## AssistantConfig (`mopai/config.py`)

- Replace the four path fields (`skills_dir`, `history_path`, `memory_path`, `documents_path`) with
  a single field:
  - `data_dir: Path = field(default_factory=paths.data_dir)`
- Re-expose the four leaf paths as read-only `@property` values derived from `data_dir`:
  - `skills_dir -> data_dir / "skills"`
  - `history_path -> str(data_dir / "history.json")`
  - `memory_path -> data_dir / "memory"`
  - `documents_path -> data_dir / "documents"`
  - (Property names and return types match today's fields so `assistant.py` is unchanged.)
- Add runtime fields the config file now covers (previously read straight off the argparse
  namespace):
  - `frontend: str = "cli"`
  - `host: str = "127.0.0.1"`
  - `port: int = 8000`

`assistant.py` continues to read `config.skills_dir` / `config.history_path` /
`config.memory_path` / `config.documents_path` and needs no change.

## Config file (`mopai/settings.py`, new)

A small module responsible only for finding and parsing the TOML file into a flat dict of config
field overrides.

- Reader: `tomllib` (stdlib, Python 3.11+). No new dependency.
- Lookup order for the file path: `--config PATH` -> `$MOPAI_CONFIG` -> `state_dir()/config.toml`.
  First hit wins. A missing file (including the default location) is a silent no-op.
- TOML section -> field mapping:
  - `[assistant]`: `model`, `system_message`, `reminder_seconds`, `reminder_text`, `show_thinking`,
    `show_tools`, `memory`, `load_plugins`
  - `[tools]`: `groups` -> `tools` (list of strings)
  - `[mcp]`: `servers` -> `mcp_servers` (list), `bearer` -> `mcp_bearer`
  - `[paths]`: `data_dir` (optional absolute override)
  - `[frontend]`: `name` -> `frontend`
  - `[web]`: `host`, `port`
- Validation:
  - Unknown keys: log a warning naming the key and ignore (typo safety).
  - Type mismatch vs. the target field: raise a clear error naming the section/key and expected
    type. Fail fast rather than silently coercing.

## Precedence and resolution (`mopai/cli.py`)

CLI flag > config-file value > built-in default.

- Argparse defaults change to `None`/sentinels so "not provided" is distinguishable from "provided
  with the default value". Boolean flags use `BooleanOptionalAction` with `default=None`.
- `resolve_config(args)` (replaces `config_from_args`):
  1. Start from `AssistantConfig()` built-in defaults.
  2. Overlay file values (from `settings.load(...)`).
  3. Overlay CLI args that are not `None`.
  4. Run the data-dir migration, then construct and return the final `AssistantConfig`.
- Flag changes:
  - Remove `--skills-dir` and `--history`.
  - Add `--config PATH`.
  - `--host`, `--port`, `--frontend` still exist but now feed the merged config (and default to
    `None` at the parser so the file can supply them).
- `cli.main` selects the front end from `config.frontend`; `frontends/web.py` reads
  `config.host` / `config.port` instead of `args.host` / `args.port`.

## Docs

- `config.example.toml` at the repo root: every setting, commented, set to its built-in default.
  This is the "defaults defined for everything" artifact (the code holds the live defaults; the
  example documents them).
- README: update the storage layout section (now `data/`), the flags list (drop `--skills-dir` /
  `--history`, add `--config`), and add a short "Configuration file" subsection.

## Testing

- `data_dir` derivation: the four leaf properties resolve under `data_dir`.
- File overrides default; CLI overrides file; missing file falls back to defaults.
- `--config PATH` and `$MOPAI_CONFIG` are honored in the documented order.
- Unknown key warns and is ignored; type mismatch raises.
- One-time migration: old-layout files move into `data/`; no-op when `data/` exists.
- Update `tests/test_assistant.py`, `tests/test_web.py`, `tests/test_plugins.py` to set
  `data_dir=tmp_path` instead of the four removed path kwargs. Drop/replace the
  `--skills-dir` / `--history` flag tests with `data_dir` / `--config` equivalents.

## Module boundaries

- `paths.py`: pure path computation + one-time migration. No config knowledge.
- `config.py`: the `AssistantConfig` data shape and built-in defaults.
- `settings.py`: file discovery + TOML parsing + validation -> override dict. No argparse, no
  precedence.
- `cli.py`: argparse + precedence merge + wiring. Owns the order defaults < file < CLI.
