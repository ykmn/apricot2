"""Per-user UI state persistence (config/sessions-ui.yaml).

Stores channel selection, timeline position, selection markers and log items
keyed by username so state survives logout/login and works across devices.
When auth is disabled, state is stored under the anonymous key.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml

_UI_STATE_FILE = Path(__file__).parent.parent / "config" / "sessions-ui.yaml"
_ANON_KEY = "__anonymous__"

_ALLOWED_FIELDS = {"channel_id", "timeline_time", "sel_start", "sel_end", "log_items"}

# Guards the read-modify-write in save() against concurrent callers within
# this process (e.g. if save() is ever offloaded via asyncio.to_thread).
# Currently safe anyway under the single-worker, no-await-between-read-write
# deployment, but this makes that an enforced invariant instead of an
# incidental one. Does NOT protect against multiple worker *processes*
# writing the same file — this app is deployed single-process.
_save_lock = threading.Lock()


def _key(username: str | None) -> str:
    return username if username else _ANON_KEY


def load(username: str | None) -> dict:
    """Return stored UI state for the given user (empty dict if none)."""
    if not _UI_STATE_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(_UI_STATE_FILE.read_text(encoding="utf-8")) or {}
        return data.get(_key(username)) or {}
    except Exception:
        return {}


def save(username: str | None, state: dict) -> None:
    """Persist UI state for the given user atomically."""
    try:
        with _save_lock:
            if _UI_STATE_FILE.exists():
                data: dict = yaml.safe_load(_UI_STATE_FILE.read_text(encoding="utf-8")) or {}
            else:
                data = {}

            # Keep only known fields to avoid accumulating garbage
            clean: dict[str, Any] = {k: v for k, v in state.items() if k in _ALLOWED_FIELDS}
            data[_key(username)] = clean

            _UI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(_UI_STATE_FILE.parent), suffix=".tmp", prefix="ui_state_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                os.replace(tmp, str(_UI_STATE_FILE))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        pass  # non-critical — UI state loss is acceptable
