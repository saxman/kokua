"""One-off scripts, not part of the installed `kokua` package.

`pyproject.toml`'s `[tool.setuptools.packages.find]` looks only under `src/`, so nothing here ever
ships in the wheel; this `__init__.py` exists only so `migrate_subagent_payloads` imports as a
normal package from a repository checkout (in tests, and for a developer running it by hand).
"""

from __future__ import annotations
