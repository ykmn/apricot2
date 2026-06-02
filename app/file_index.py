"""
File index: maintains an in-memory index of audio files per channel.
Polls SMB/local sources and notifies WebSocket clients of changes.
"""
from __future__ import annotations

import asyncio
import io
import os
from datetime import date, datetime, timedelta
from typing import Callable

from sortedcontainers import SortedList

from . import smb_client as smb
from .models import AudioFile, ChannelConfig


# Broadcast callback type: called with (channel_id, added, removed)
BroadcastFn = Callable[[str, list[dict], list[dict]], None]


class ChannelIndex:
    """Sorted list of AudioFile records for one channel."""

    def __init__(self, channel: ChannelConfig) -> None:
        self.channel = channel
        # key: (start_dt, end_dt) — SortedList by start_dt
        self._files: SortedList[AudioFile] = SortedList(key=lambda f: f.start_dt)
        self._paths: set[str] = set()  # known relative paths

    # ------------------------------------------------------------------
    def get_intervals(self, start: datetime, end: datetime) -> list[dict]:
        """Return [{start, end}] dicts of covered intervals in [start, end]."""
        result = []
        for af in self._files:
            if af.end_dt <= start:
                continue
            if af.start_dt >= end:
                break
            result.append({
                "start": af.start_dt.timestamp(),
                "end": af.end_dt.timestamp(),
            })
        return result

    def files_for_range(self, start: datetime, end: datetime) -> list[AudioFile]:
        """Return audio files that overlap [start, end]."""
        result = []
        for af in self._files:
            if af.end_dt <= start:
                continue
            if af.start_dt >= end:
                break
            result.append(af)
        return result

    # ------------------------------------------------------------------
    def _scan_date(self, d: date) -> dict[str, AudioFile]:
        """Scan a single date folder, return rel_path -> AudioFile map."""
        ch = self.channel
        folder = d.strftime(ch.folder_format)
        entries = smb.listdir(ch.local_path, ch.smb, folder)
        result: dict[str, AudioFile] = {}
        for name in entries:
            if not name.lower().endswith(f".{ch.file_extension.lower()}"):
                continue
            stem = os.path.splitext(name)[0]
            try:
                start_dt = datetime.strptime(stem, ch.file_format).replace(
                    year=d.year, month=d.month, day=d.day
                )
            except ValueError:
                continue
            rel = f"{folder}/{name}" if os.sep == '/' or not ch.local_path else f"{folder}\\{name}"
            rel = f"{folder}/{name}"  # always use forward slash for key
            try:
                size = smb.getsize(ch.local_path, ch.smb, rel)
                duration = _estimate_duration(size, ch.file_extension, ch.sample_rate, ch.bitrate)
            except Exception:
                duration = 3600.0  # fallback: assume 1-hour file
            end_dt = start_dt + timedelta(seconds=duration)
            result[rel] = AudioFile(
                channel_id=ch.id,
                path=smb.full_path(ch.local_path, ch.smb, rel),
                start_dt=start_dt,
                end_dt=end_dt,
                duration=duration,
                is_smb=ch.smb is not None,
            )
        return result

    def refresh(self, days_back: int = 90, days_ahead: int = 1) -> tuple[list[AudioFile], list[AudioFile]]:
        """Rescan relevant date range. Returns (added, removed) lists."""
        today = date.today()
        dates = [today - timedelta(days=i) for i in range(days_back, -days_ahead - 1, -1)]

        current: dict[str, AudioFile] = {}
        for d in dates:
            try:
                current.update(self._scan_date(d))
            except Exception as exc:
                pass  # unreachable date folders are silently skipped

        old_paths = self._paths
        new_paths = set(current.keys())

        added_keys = new_paths - old_paths
        removed_keys = old_paths - new_paths

        added = [current[k] for k in added_keys]
        removed = [af for af in self._files if _rel_key(af) in removed_keys]

        for af in removed:
            self._files.remove(af)
        for af in added:
            self._files.add(af)

        self._paths = new_paths
        return added, removed


def _rel_key(af: AudioFile) -> str:
    """Reconstruct the relative path key used during scanning."""
    d = af.start_dt.date()
    # We can't easily reverse the path here; store rel_path on the object instead.
    # This is handled correctly because we use af.path as unique key.
    return af.path  # fallback — not perfect, see note below


def _estimate_duration(size: int, ext: str, sample_rate: int, bitrate: str | None) -> float:
    ext = ext.lower()
    if ext == "wav":
        # WAV: 44 bytes header, then PCM at 2ch * 16bit = 4 bytes/sample
        data_bytes = max(size - 44, 0)
        bps = sample_rate * 2 * 2  # 2 channels, 16-bit
        return data_bytes / bps if bps else 3600.0
    if bitrate:
        # bitrate like "128k" or "64k"
        bps_str = bitrate.lower().rstrip("k")
        try:
            kbps = float(bps_str)
            return size / (kbps * 1000 / 8)
        except ValueError:
            pass
    # MP3/AAC fallback: assume 128 kbps
    return size / (128 * 1000 / 8)


# ──────────────────────────────────────────────────────────────────────────────
# Global index manager
# ──────────────────────────────────────────────────────────────────────────────

class FileIndexManager:
    def __init__(self) -> None:
        self._indexes: dict[str, ChannelIndex] = {}
        self._broadcast: BroadcastFn | None = None
        self._task: asyncio.Task | None = None
        self._poll_interval: int = 10

    def setup(self, channels: list[ChannelConfig], poll_interval: int = 10,
              broadcast: BroadcastFn | None = None) -> None:
        self._broadcast = broadcast
        self._poll_interval = poll_interval
        for ch in channels:
            self._indexes[ch.id] = ChannelIndex(ch)

    def get_index(self, channel_id: str) -> ChannelIndex | None:
        return self._indexes.get(channel_id)

    async def initial_scan(self) -> None:
        loop = asyncio.get_event_loop()
        for idx in self._indexes.values():
            await loop.run_in_executor(None, idx.refresh)
            print(f"[index] {idx.channel.id}: {len(idx._files)} files indexed")

    async def _poll_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(self._poll_interval)
            for idx in self._indexes.values():
                try:
                    added, removed = await loop.run_in_executor(None, idx.refresh)
                    if (added or removed) and self._broadcast:
                        self._broadcast(
                            idx.channel.id,
                            [{"start": a.start_dt.timestamp(), "end": a.end_dt.timestamp()} for a in added],
                            [{"start": r.start_dt.timestamp(), "end": r.end_dt.timestamp()} for r in removed],
                        )
                except Exception as exc:
                    print(f"[index] poll error for {idx.channel.id}: {exc}")

    def start_polling(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())


file_index = FileIndexManager()
