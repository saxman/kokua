"""Rotating-file logging setup."""

from __future__ import annotations


from tests.channels import _config


def test_configure_logging_writes_to_log_file(tmp_path):
    import logging as _logging
    from logging.handlers import RotatingFileHandler

    from kokua.logging_setup import configure_logging

    cfg = _config(tmp_path)
    try:
        configure_logging(cfg)
        _logging.getLogger("kokua").info("hello-diag-test-line")
        logfile = cfg.logs_path / "kokua.log"
        assert logfile.exists()
        assert "hello-diag-test-line" in logfile.read_text()
    finally:
        for name in ("kokua", "aimu"):
            lg = _logging.getLogger(name)
            for h in list(lg.handlers):
                if isinstance(h, RotatingFileHandler):
                    lg.removeHandler(h)
                    h.close()


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
        for name in ("kokua", "aimu"):
            lg = _logging.getLogger(name)
            for h in list(lg.handlers):
                if isinstance(h, RotatingFileHandler):
                    lg.removeHandler(h)
                    h.close()
