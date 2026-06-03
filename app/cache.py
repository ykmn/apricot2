"""
Disk cache for the file index.
Each channel is stored as cache/<channel_id>.json for fast startup.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .app_logger import get_logger
from .models import AudioFile

log = get_logger("cache")

CACHE_DIR = Path(__file__).parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def save(channel_id: str, files: list[AudioFile]) -> None:
    """Persist file list for one channel to cache/<channel_id>.json."""
    path = CACHE_DIR / f"{channel_id}.json"
    try:
        data = {
            "channel_id": channel_id,
            "saved_at":   datetime.now().isoformat(timespec="seconds"),
            "count":      len(files),
            "files": [
                {
                    "path":  af.path,
                    "rel":   af.rel_path,
                    "start": af.start_dt.timestamp(),
                    "end":   af.end_dt.timestamp(),
                    "dur":   af.duration,
                    "smb":   af.is_smb,
                }
                for af in files
            ],
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        log.debug("Cache saved: %s (%d files)", channel_id, len(files))
    except Exception as exc:
        log.warning("Cache save failed for %s: %s", channel_id, exc)


def load(channel_id: str) -> list[AudioFile]:
    """Load cached file list. Returns [] on cache miss or error."""
    path = CACHE_DIR / f"{channel_id}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = []
        for f in data.get("files", []):
            result.append(AudioFile(
                channel_id=channel_id,
                path=f["path"],
                rel_path=f["rel"],
                start_dt=datetime.fromtimestamp(f["start"]),
                end_dt=datetime.fromtimestamp(f["end"]),
                duration=f["dur"],
                is_smb=f.get("smb", False),
            ))
        log.debug("Cache loaded: %s (%d files, saved %s)",
                  channel_id, len(result), data.get("saved_at", "?"))
        return result
    except Exception as exc:
        log.warning("Cache load failed for %s: %s", channel_id, exc)
        return []


def invalidate(channel_id: str) -> None:
    """Remove the cache file for a channel."""
    path = CACHE_DIR / f"{channel_id}.json"
    if path.exists():
        path.unlink()
        log.debug("Cache invalidated: %s", channel_id)
