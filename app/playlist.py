"""Read playlist log files with priority fallback and per-date disk cache."""
from __future__ import annotations

import bisect
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

# In-memory TTL cache for today's entries (avoids repeated SMB reads on scroll)
_mem_cache: dict[tuple[str, date], tuple[datetime, list]] = {}
MEM_CACHE_TTL = 300  # seconds — re-read from source after 5 minutes


# ── Public API ────────────────────────────────────────────────────────────────

# Default look-back window used by preload()
PRELOAD_DAYS = 14


def preload(
    configs: list,   # list[PlaylistConfig]
    days_back: int = PRELOAD_DAYS,
    progress_cb=None,  # callable(playlist_id, done, total) | None
) -> None:
    """Eagerly load and disk-cache playlogs for [today-days_back … today].

    Skips dates that are already on disk.  today's data goes into the
    short in-memory TTL cache (as usual).  progress_cb is called after
    each playlist completes all its dates.
    """
    today = datetime.now().date()
    total = len(configs)
    for done_idx, config in enumerate(configs, 1):
        for i in range(days_back, -1, -1):   # oldest first so cache fills chronologically
            d = today - timedelta(days=i)
            try:
                _get_date(config, d)
            except Exception as exc:
                log.debug("Preload %s %s: %s", config.id, d, exc)
        log.debug("Preload done: %s (%d/%d)", config.id, done_idx, total)
        if progress_cb:
            progress_cb(config.id, done_idx, total)


def invalidate_today(playlist_id: str) -> None:
    """Drop today's in-memory cache so the next request re-reads from the source."""
    today = datetime.now().date()
    _mem_cache.pop((playlist_id, today), None)
    log.debug("Playlog today cache invalidated: %s", playlist_id)


def invalidate_recent(playlist_id: str, days: int = 2) -> None:
    """Delete disk cache files and memory cache for the last N days."""
    pl_dir = CACHE_DIR / playlist_id
    today = datetime.now().date()
    for i in range(days):
        d = today - timedelta(days=i)
        # Disk cache
        if pl_dir.exists():
            p = _cache_path(playlist_id, d)
            if p.exists():
                p.unlink()
                log.debug("Playlog disk cache invalidated: %s %s", playlist_id, p.stem)
        # Memory cache
        _mem_cache.pop((playlist_id, d), None)


def check_sources(config: PlaylistConfig, _unused_date: date = None) -> list[dict]:
    """Check each source folder.  Returns [{priority, ok, error}].

    Green (ok=True)  — folder is accessible, contains ≥1 .log file,
                       and at least one file parses into recognisable entries.
    Red   (ok=False) — folder unreachable, empty, or all files fail to parse.
    """
    results = []
    for source in sorted(config.sources, key=lambda s: s.priority):
        result = _check_source_health(config, source)
        results.append(result)
    return results


def _check_source_health(config: PlaylistConfig, source: PlaylistSource) -> dict:
    base = {"priority": source.priority, "ok": False, "error": ""}

    # 1. List the folder — raises if inaccessible
    try:
        names = smb.listdir_strict(source.local_path, source.smb)
    except Exception as exc:
        base["error"] = str(exc)[:200]
        return base

    # 2. Find .log files (match the file_mask pattern loosely by extension)
    log_files = sorted(n for n in names if n.lower().endswith(".log"))
    if not log_files:
        base["error"] = "no .log files in folder"
        return base

    # 3. Try to parse the most recent file; verify it yields entries with expected fields
    for filename in reversed(log_files):   # most recent last in sorted order
        try:
            raw = smb.read_bytes(source.local_path, source.smb, filename)
        except Exception:
            continue
        entries = _parse(config, source, raw, datetime.now().date())
        if entries:
            base["ok"] = True
            return base

    base["error"] = "files found but none parsed successfully"
    return base


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
    cacheable = d < today  # past dates → permanent disk cache

    if cacheable:
        cached = _cache_load(config.id, d)
        if cached is not None:
            return cached
    else:
        # Today: short in-memory TTL cache so repeated scroll doesn't hit SMB
        key = (config.id, d)
        if key in _mem_cache:
            fetched_at, mem_entries = _mem_cache[key]
            if (datetime.now() - fetched_at).total_seconds() < MEM_CACHE_TTL:
                return mem_entries

    entries = _load_with_fallback(config, d)

    if cacheable and entries:
        _cache_save(config.id, d, entries)
    elif not cacheable:
        # Store even empty result so we don't spam SMB while file doesn't exist yet
        _mem_cache[(config.id, d)] = (datetime.now(), entries)

    return entries


def _load_with_fallback(config: PlaylistConfig, d: date) -> list[PlaylistEntry]:
    sources_sorted = sorted(config.sources, key=lambda s: s.priority)
    if not sources_sorted:
        return []

    # Load each source independently
    by_priority: list[tuple[int, list[PlaylistEntry]]] = []
    for source in sources_sorted:
        entries = _load_source(config, source, d)
        if entries:
            log.debug("Playlist %s date %s: %d entries from priority %d",
                      config.id, d, len(entries), source.priority)
            by_priority.append((source.priority, entries))

    if not by_priority:
        return []
    if len(by_priority) == 1:
        return by_priority[0][1]

    # Merge: priority-1 is the base; fill gaps from lower-priority sources.
    # GAP_THRESHOLD — минимальный интервал без записей в базе, считающийся пробелом.
    # DUPE_THRESHOLD — максимальное расхождение между теми же событиями на разных машинах.
    GAP_THRESHOLD  = 60   # seconds
    DUPE_THRESHOLD = 5    # seconds — clock skew between broadcast machines

    base = sorted(by_priority[0][1], key=lambda e: e.timestamp)
    base_times = sorted(e.timestamp for e in base)

    merged = list(base)
    for _, fallback_entries in by_priority[1:]:
        for entry in fallback_entries:
            ts = entry.timestamp
            # Skip if this is the same event already present in the base
            # (clock skew between machines is typically < 5s)
            if any(abs((t - ts).total_seconds()) <= DUPE_THRESHOLD for t in base_times):
                continue
            # Include only if the entry falls inside a genuine gap in base data:
            # both the nearest preceding and the nearest following base entry
            # are further than GAP_THRESHOLD away.
            idx = bisect.bisect_left(base_times, ts)
            gap_before = (ts - base_times[idx - 1]).total_seconds() if idx > 0 else float('inf')
            gap_after  = (base_times[idx] - ts).total_seconds() if idx < len(base_times) else float('inf')
            if gap_before >= GAP_THRESHOLD and gap_after >= GAP_THRESHOLD:
                merged.append(entry)

    merged.sort(key=lambda e: e.timestamp)
    log.debug("Playlist %s date %s: %d merged entries (%d sources)",
              config.id, d, len(merged), len(by_priority))
    return merged


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
    f_duration = f.get("duration",  "ElemLength")
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

            duration = _parse_duration(_get(f_duration))

            entries.append(PlaylistEntry(
                timestamp=ts,
                title=title,
                cls=cls,
                duration=duration,
                elem_id=elem_id,
            ))
        except Exception:
            continue

    return entries


# ── Duration parsing ─────────────────────────────────────────────────────────

def _parse_duration(s: str) -> Optional[float]:
    """Parse ElemLength format 'MM:SS.mmm' or 'HH:MM:SS.mmm' → seconds, or None."""
    s = s.strip()
    if not s:
        return None
    try:
        parts = s.split(":")
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours   = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
    except (ValueError, IndexError):
        pass
    return None


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
                duration=e.get("duration"),
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
                 "cls": e.cls, "duration": e.duration, "elem_id": e.elem_id}
                for e in entries
            ],
        }
        p.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")
    except Exception as exc:
        log.debug("Playlist cache save failed %s %s: %s", playlist_id, d, exc)
