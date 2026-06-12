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
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
        today = datetime.now().strftime("%Y-%m-%d")
        self._current_date = today
        path = self.logs_dir / f"{today}.log"
        self._stream = open(path, "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self._open()
        try:
            msg = self.format(record)
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:
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
