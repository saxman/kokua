"""Rotating-file logging setup."""

from __future__ import annotations


from tests.channels import _config


def _detach():
    """Remove the handler `configure_logging` attached, so one test's setup cannot leak into another."""
    import logging as _logging
    from logging.handlers import RotatingFileHandler

    from kokua.logging_setup import _LOGGERS

    for name in _LOGGERS:
        lg = _logging.getLogger(name)
        for h in list(lg.handlers):
            if isinstance(h, RotatingFileHandler):
                lg.removeHandler(h)
                h.close()


def test_configure_logging_writes_to_log_file(tmp_path):
    import logging as _logging

    from kokua.logging_setup import configure_logging

    cfg = _config(tmp_path)
    try:
        configure_logging(cfg)
        _logging.getLogger("kokua").info("hello-diag-test-line")
        logfile = cfg.logs_path / "kokua.log"
        assert logfile.exists()
        assert "hello-diag-test-line" in logfile.read_text()
    finally:
        _detach()


def test_configure_logging_is_idempotent(tmp_path):
    import logging as _logging
    from logging.handlers import RotatingFileHandler

    from kokua.logging_setup import configure_logging

    cfg = _config(tmp_path)
    try:
        configure_logging(cfg)
        configure_logging(cfg)
        handlers = [h for h in _logging.getLogger("kokua").handlers if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1
    finally:
        _detach()


def test_mcp_sdk_logger_is_captured_instead_of_falling_through_to_stderr(tmp_path):
    """The MCP SDK logs OAuth failures itself, and unhandled they reach the console unattributed.

    Without a handler the `mcp` logger propagates to `logging.lastResort`, which prints to stderr with
    no timestamp, no logger name, and no server URL, so a failed client registration during a
    concurrent reconnect reads as if it belonged to whatever flow logged last. Both halves are asserted
    because each is the point: the record has to reach the file, and the logger has to be handled so
    `lastResort` stops writing the bare copy.
    """
    import logging as _logging

    from kokua.logging_setup import configure_logging

    cfg = _config(tmp_path)
    try:
        configure_logging(cfg)
        _logging.getLogger("mcp.client.auth.oauth2").error("registration-failed-test-line")
        assert "registration-failed-test-line" in (cfg.logs_path / "kokua.log").read_text()
        assert _logging.getLogger("mcp").hasHandlers()
    finally:
        _detach()
