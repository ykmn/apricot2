"""Read playlist log CSV files from local or SMB sources."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Iterator

from . import smb_client as smb
from .models import PlaylistConfig, PlaylistEntry


def get_entries(
    config: PlaylistConfig,
    start: datetime,
    end: datetime,
) -> list[PlaylistEntry]:
    """Return playlist entries in the given time range."""
    result: list[PlaylistEntry] = []

    # Determine which dates to load
    d = start.date()
    while d <= end.date():
        entries = _load_date(config, d)
        for e in entries:
            if start <= e.timestamp <= end:
                result.append(e)
        d = date(d.year, d.month, d.day)
        from datetime import timedelta
        d += timedelta(days=1)

    return result


def _load_date(config: PlaylistConfig, d: date) -> list[PlaylistEntry]:
    filename = d.strftime(config.file_mask)
    try:
        raw = smb.read_bytes(config.local_path, config.smb, filename)
    except Exception:
        return []

    text = raw.decode(config.encoding, errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=config.delimiter)

    fields = config.fields
    f_date = fields.get("date", "Date")
    f_time = fields.get("time", "Time")
    f_title = fields.get("title", "Title")
    f_cls = fields.get("cls", "Class")
    f_dur = fields.get("duration")

    entries = []
    for row in reader:
        try:
            date_str = row.get(f_date, "").strip()
            time_str = row.get(f_time, "").strip()
            if not time_str:
                continue
            # Try to parse combined or separate date+time
            if date_str:
                ts = _parse_datetime(date_str, time_str)
            else:
                ts = datetime.strptime(time_str, "%H:%M:%S").replace(
                    year=d.year, month=d.month, day=d.day
                )
            dur = None
            if f_dur and row.get(f_dur):
                try:
                    dur = float(row[f_dur])
                except ValueError:
                    pass
            entries.append(PlaylistEntry(
                timestamp=ts,
                title=row.get(f_title, "").strip(),
                cls=row.get(f_cls, "").strip(),
                duration=dur,
            ))
        except Exception:
            continue
    return entries


def _parse_datetime(date_str: str, time_str: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            d = datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"Unparseable date: {date_str!r}")

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(time_str, fmt).time()
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"Unparseable time: {time_str!r}")

    return datetime.combine(d, t)
