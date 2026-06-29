# Contributing to Mopai

Thanks for your interest. Mopai is a small, hackable application built on [AIMU](https://github.com/saxman/aimu).

## Setup

Mopai needs AIMU's latest features, so install AIMU from source first (see the README):

```bash
pip install -e ../aimu                 # a local AIMU checkout (sibling dir)
pip install -e '.[web,dev]'
```

## Checks

Run these before opening a PR:

```bash
ruff check .
ruff format --check .       # use `ruff format .` to apply
pytest -q
```

Line length is 120 (`ruff` is configured in `pyproject.toml`). Tests are mock-only and require no model,
network, or API keys.

## Conventions

- Plain Python: dataclasses, functions, type hints. Keep the core small; push capability into plugins.
- Add a **front end** (a new transport) or a **tool-pack** (new tools) as a plugin, in its own package or
  under `src/mopai/frontends` / `src/mopai/toolpacks`, registered via the `mopai.frontends` / `mopai.tools`
  entry-point groups. See `src/mopai/toolpacks/example.py` for the template.
- Use English punctuation (no em dashes); inclusive terminology (allowlist/blocklist, primary/replica, main
  branch).
- Update `CHANGELOG.md` and the README when you change behavior or the public surface. Include tests.

## Pull requests

One concern per PR. Make sure `ruff check`, `ruff format --check`, and `pytest` pass.
