"""Read playlist log files with priority fallback and per-date disk cache."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import smb_client as smb
from .app_logger import get_logger
from .models import PlaylistConfig, PlaylistEntry, PlaylistSource

log = get_logger("playlist")

CACHE_DIR = Path(__file__).parent.parent / "cache_playlogs"
CACHE_DIR.mkdir(exist_ok=True)


# ── Public API ────────────────────────────────────────────────────────────────

def get_entries(
    config: PlaylistConfig,
    start: datetime,
    end: datetime,
) -> list[PlaylistEntry]:
    """Return entries in [start, end] with priority-based source fallback."""
    result: list[PlaylistEntry] = []
    d = start.date()
    while d <= end.date():
        for e in _get_date(config, d):
            if start <= e.timestamp <= end:
                result.append(e)
        d += timedelta(days=1)
    return result


# ── Per-date loading with cache ───────────────────────────────────────────────

def _get_date(config: PlaylistConfig, d: date) -> list[PlaylistEntry]:
    today = datetime.now().date()
    cacheable = d < today  # today's file may still be growing

    if cacheable:
        cached = _cache_load(config.id, d)
        if cached is not None:
            return cached

    entries = _load_with_fallback(config, d)

    if cacheable and entries:
        _cache_save(config.id, d, entries)

    return entries


def _load_with_fallback(config: PlaylistConfig, d: date) -> list[PlaylistEntry]:
    for source in sorted(config.sources, key=lambda s: s.priority):
        entries = _load_source(config, source, d)
        if entries:
            log.debug("Playlist %s date %s: %d entries from priority %d",
                      config.id, d, len(entries), source.priority)
            return entries
    return []


# ── Source reader ─────────────────────────────────────────────────────────────

def _load_source(config: PlaylistConfig, source: PlaylistSource, d: date) -> list[PlaylistEntry]:
    filename = d.strftime(source.file_mask)
    try:
        raw = smb.read_bytes(source.local_path, source.smb, filename)
    except Exception:
        return []
    return _parse(config, source, raw, d)


def _parse(
    config: PlaylistConfig,
    source: PlaylistSource,
    raw: bytes,
    d: date,
) -> list[PlaylistEntry]:
    text = raw.decode(source.encoding, errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter=source.delimiter)

    f = config.fields
    f_dt       = f.get("datetime",  "EventTime")
    f_title    = f.get("title",     "ElemName")
    f_artist   = f.get("artist",    "ElemArtist")
    f_cls      = f.get("cls",       "ElemClass")
    f_db_id    = f.get("db_id",     "ElemDbId")
    f_id_num   = f.get("id_number", "ElemIdNumber")
    skip_pfx   = source.header_skip_prefix

    col: dict[str, int] = {}  # field_name → column index in data rows
    entries: list[PlaylistEntry] = []

    for row in reader:
        if not row:
            continue

        # ── Header detection ──────────────────────────────────────────────
        if not col:
            first = row[0].strip().strip('"')
            if skip_pfx and first == skip_pfx:
                # "FIELD LIST", "EventTime", "Type", ...
                # data rows have no leading cell, so column N-1 of header → column N-2 of data
                # Actually: header[1]="EventTime" is at data[0], header[2]="Type" at data[1], …
                col = {name.strip().strip('"'): i - 1
                       for i, name in enumerate(row) if i > 0}
            elif first in ("DAY START", "DAY END"):
                continue  # skip, wait for real header
            else:
                col = {name.strip().strip('"'): i for i, name in enumerate(row)}
            continue

        # ── Data row ──────────────────────────────────────────────────────
        def _get(name: str) -> str:
            idx = col.get(name, -1)
            if idx < 0 or idx >= len(row):
                return ""
            return row[idx].strip().strip('"')

        first = row[0].strip().strip('"')
        if first in ("DAY START", "DAY END", ""):
            continue

        try:
            dt_str = _get(f_dt)
            if not dt_str:
                continue
            ts = _parse_dt(dt_str, d)

            name   = _get(f_title)
            artist = _get(f_artist)
            title  = f"{artist} — {name}" if artist else name

            cls    = _get(f_cls)

            db_id  = _get(f_db_id)
            id_num = _get(f_id_num)
            elem_id = (
                f"[dbID: {db_id} // ID_Number: {id_num}]"
                if db_id or id_num else ""
            )

            entries.append(PlaylistEntry(
                timestamp=ts,
                title=title,
                cls=cls,
                elem_id=elem_id,
            ))
        except Exception:
            continue

    return entries


# ── Datetime parsing ──────────────────────────────────────────────────────────

def _parse_dt(s: str, fallback_date: date) -> datetime:
    """Parse combined datetime string or time-only string."""
    s = s.strip().strip('"')
    # Try combined formats first
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    # Time-only
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt).time()
            return datetime.combine(fallback_date, t)
        except ValueError:
            pass
    raise ValueError(f"Unparseable datetime: {s!r}")


# ── Disk cache ────────────────────────────────────────────────────────────────

def _cache_path(playlist_id: str, d: date) -> Path:
    pl_dir = CACHE_DIR / playlist_id
    pl_dir.mkdir(exist_ok=True)
    return pl_dir / f"{d.isoformat()}.json"


def _cache_load(playlist_id: str, d: date) -> Optional[list[PlaylistEntry]]:
    p = _cache_path(playlist_id, d)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [
            PlaylistEntry(
                timestamp=datetime.fromtimestamp(e["ts"]),
                title=e["title"],
                cls=e["cls"],
                elem_id=e.get("elem_id", ""),
            )
            for e in data.get("entries", [])
        ]
    except Exception as exc:
        log.debug("Playlist cache load failed %s %s: %s", playlist_id, d, exc)
        return None


def _cache_save(playlist_id: str, d: date, entries: list[PlaylistEntry]) -> None:
    p = _cache_path(playlist_id, d)
    try:
        data = {
            "date":     d.isoformat(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "entries": [
                {"ts": e.timestamp.timestamp(), "title": e.title,
                 "cls": e.cls, "elem_id": e.elem_id}
                for e in entries
            ],
        }
        p.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")
    except Exception as exc:
        log.debug("Playlist cache save failed %s %s: %s", playlist_id, d, exc)
