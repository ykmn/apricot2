"""Daily rotating log files — logs/YYYY-mm-dd.log

Usage:
    from .app_logger import get_logger
    log = get_logger(__name__)
    log.info("Channel selected: %s", channel_id)
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)


class _DailyFileHandler(logging.Handler):
    """Appends to logs/YYYY-mm-dd.log; auto-rotates at midnight."""

    def __init__(self, logs_dir: Path) -> None:
        super().__init__()
        self.logs_dir = logs_dir
        self._current_date: str = ""
        self._stream = None
        self._open()

    def _open(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        path = self.logs_dir / f"{today}.log"
        # Open the new stream before touching the old one: if this raises
        # (logs/ briefly unwritable — disk full, permission change), the
        # still-open old stream and stale _current_date are left in place
        # instead of leaving self._stream pointing at nothing, and emit()
        # just retries the rollover on the next call.
        new_stream = open(path, "a", encoding="utf-8")
        old_stream, self._stream = self._stream, new_stream
        self._current_date = today
        if old_stream:
            try:
                old_stream.close()
            except Exception:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if today != self._current_date:
                self._open()
            msg = self.format(record)
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:
            # Rotation or write failure — don't let it propagate out of an
            # arbitrary log.info()/warning() call site anywhere in the app.
            self.handleError(record)

    def close(self) -> None:
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
        super().close()


_FMT = logging.Formatter(
    fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = _DailyFileHandler(LOGS_DIR)
_file_handler.setFormatter(_FMT)
_file_handler.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_FMT)
_console_handler.setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that writes to daily log file + stdout."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        logger.addHandler(_file_handler)
        logger.addHandler(_console_handler)
        logger.propagate = False
    return logger


def configure(screen_level: str = "INFO", file_level: str = "DEBUG") -> None:
    """Apply log level settings loaded from settings.yaml.

    screen_level — minimum level printed to stdout (e.g. "INFO", "WARNING", "ERROR")
    file_level   — minimum level written to the daily log file
    """
    s = getattr(logging, screen_level.upper(), None)
    if not isinstance(s, int):
        s = logging.INFO
    _console_handler.setLevel(s)

    f = getattr(logging, file_level.upper(), None)
    if not isinstance(f, int):
        f = logging.DEBUG
    _file_handler.setLevel(f)
