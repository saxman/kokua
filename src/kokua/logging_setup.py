"""Diagnostic logging setup: a rotating file log plus a faulthandler hook.

Kokua otherwise logs only to the launch terminal, so a hang or crash leaves nothing to inspect after
the fact. :func:`configure_logging` attaches a rotating file handler (``logs_path/kokua.log``) to the
``kokua`` and ``aimu`` loggers, and enables ``faulthandler`` so that ``kill -USR1 <pid>`` dumps every
thread's stack (catching non-asyncio / C-level hangs the async ``/diag`` command cannot see). Pairs with
``Assistant._diag_report`` (the on-demand async-stack dump for a wedged turn).
"""

from __future__ import annotations

import faulthandler
import logging
import signal
from logging.handlers import RotatingFileHandler

from .config import AssistantConfig

# Marks the handler this module owns, so repeated calls replace it rather than stacking duplicates.
_OWNED = "_kokua_file_handler"

# One shared handler serves both loggers; a single writer avoids two handlers racing on log rotation.
_LOGGERS = ("kokua", "aimu")


def configure_logging(config: AssistantConfig) -> None:
    """Attach the rotating file log to the kokua/aimu loggers and enable faulthandler. Idempotent."""
    config.logs_path.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.log_level.upper(), logging.INFO)

    handler = RotatingFileHandler(
        config.logs_path / "kokua.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _OWNED, True)

    for name in _LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(level)
        for existing in [h for h in lg.handlers if getattr(h, _OWNED, False)]:
            lg.removeHandler(existing)
            existing.close()
        lg.addHandler(handler)

    faulthandler.enable()
    # POSIX-only: `kill -USR1 <pid>` dumps all thread stacks. No SIGUSR1 on Windows.
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1)
