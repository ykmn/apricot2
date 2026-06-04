"""
File index: maintains an in-memory index of audio files per channel.
Startup sequence:
  1. Load from disk cache  → immediate availability
  2. Background rescan     → audio-probe for correct bitrate, update cache
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import yaml
from sortedcontainers import SortedList

from . import audio_probe
from . import cache as disk_cache
from . import smb_client as smb
from .app_logger import get_logger
from .models import AudioFile, ChannelConfig

log = get_logger("file_index")

# ── Detected audio params ─────────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_DETECTED_PATH = _CONFIG_DIR / "detected_params.yaml"
_detected_mem: dict = {}   # in-memory mirror of the file


def _load_detected() -> dict:
    global _detected_mem
    if _detected_mem:
        return _detected_mem
    if not _DETECTED_PATH.exists():
        return {}
    try:
        with _DETECTED_PATH.open(encoding="utf-8") as fh:
            _detected_mem = yaml.safe_load(fh) or {}
    except Exception as exc:
        log.warning("Failed to read detected_params.yaml: %s", exc)
    return _detected_mem


def _save_detected(channel_id: str, params: dict) -> None:
    global _detected_mem
    all_params = dict(_load_detected())
    all_params[channel_id] = {
        k: v for k, v in params.items() if k != "detected_at"
    }
    all_params[channel_id]["detected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _detected_mem = all_params
    try:
        header = "# Auto-detected audio parameters — regenerated automatically, do not edit\n"
        body = yaml.dump(all_params, allow_unicode=True,
                         default_flow_style=False, sort_keys=True)
        _DETECTED_PATH.write_text(header + body, encoding="utf-8")
    except Exception as exc:
        log.error("Failed to write detected_params.yaml: %s", exc)


def _apply_detected(ch: ChannelConfig, params: dict) -> bool:
    """Merge detected params into channel config. Returns True if bitrate changed."""
    changed = False
    if ch.file_extension.lower() == "mp3":
        new_br = params.get("bitrate")
        if new_br and new_br != ch.bitrate:
            log.info("Applying detected bitrate for %s: %r -> %r", ch.id, ch.bitrate, new_br)
            ch.bitrate = new_br
            changed = True
    new_sr = params.get("sample_rate")
    if new_sr and new_sr != ch.sample_rate:
        ch.sample_rate = new_sr
    return changed


# ── Callback types ────────────────────────────────────────────────────────────

BroadcastFn = Callable[[str, list[dict], list[dict]], None]
ProgressFn  = Callable[[dict], None]


# ── ChannelIndex ──────────────────────────────────────────────────────────────

class ChannelIndex:
    """Sorted in-memory list of AudioFile records for one channel."""

    def __init__(self, channel: ChannelConfig) -> None:
        self.channel = channel
        self._files: SortedList[AudioFile] = SortedList(key=lambda f: f.start_dt)
        self._paths: set[str] = set()

    # ── Query ──────────────────────────────────────────────────────────────

    def get_intervals(self, start: datetime, end: datetime) -> list[dict]:
        result = []
        for af in self._files:
            if af.end_dt <= start:
                continue
            if af.start_dt >= end:
                break
            result.append({"start": af.start_dt.timestamp(), "end": af.end_dt.timestamp()})
        return result

    def files_for_range(self, start: datetime, end: datetime) -> list[AudioFile]:
        result = []
        for af in self._files:
            if af.end_dt <= start:
                continue
            if af.start_dt >= end:
                break
            result.append(af)
        return result

    # ── Scan ───────────────────────────────────────────────────────────────

    def _scan_date(self, d: date) -> dict[str, AudioFile]:
        """Scan one date folder; returns rel_path → AudioFile map."""
        ch = self.channel
        folder = d.strftime(ch.folder_format)
        ext = f".{ch.file_extension.lower()}"
        # scandir returns (name, size) in a single directory query — no per-file stat
        entries = smb.scandir(ch.local_path, ch.smb, folder)
        result: dict[str, AudioFile] = {}
        for name, size in entries:
            if not name.lower().endswith(ext):
                continue
            stem = name.rsplit(".", 1)[0]
            try:
                start_dt = datetime.strptime(stem, ch.file_format).replace(
                    year=d.year, month=d.month, day=d.day
                )
            except ValueError:
                continue
            rel = f"{folder}/{name}"
            dur = _estimate_duration(size, ch.file_extension, ch.sample_rate, ch.bitrate)
            result[rel] = AudioFile(
                channel_id=ch.id,
                path=smb.full_path(ch.local_path, ch.smb, rel),
                rel_path=rel,
                start_dt=start_dt,
                end_dt=start_dt + timedelta(seconds=dur),
                duration=dur,
                is_smb=ch.smb is not None,
            )
        return result

    def refresh(self, days_back: int = 90, days_ahead: int = 1) -> tuple[list[AudioFile], list[AudioFile]]:
        """Full rescan of [today-days_back … today+days_ahead]. Returns (added, removed)."""
        today = date.today()
        dates = [today - timedelta(days=i) for i in range(days_back, -days_ahead - 1, -1)]
        current: dict[str, AudioFile] = {}
        for d in dates:
            try:
                current.update(self._scan_date(d))
            except Exception as exc:
                log.debug("scan_date skipped %s/%s: %s", self.channel.id, d, exc)

        new_paths     = set(current.keys())
        added_keys    = new_paths - self._paths
        removed_keys  = self._paths - new_paths

        # Also detect files whose duration changed (e.g. still being recorded
        # when first scanned — captured with a partial size).
        changed_keys: set[str] = set()
        if self._files:
            existing: dict[str, AudioFile] = {af.rel_path: af for af in self._files}
            for key in new_paths & self._paths:
                old_af = existing.get(key)
                if old_af and current[key].end_dt != old_af.end_dt:
                    changed_keys.add(key)

        added   = [current[k] for k in added_keys | changed_keys]
        removed = [af for af in self._files
                   if af.rel_path in removed_keys or af.rel_path in changed_keys]

        for af in removed:
            self._files.remove(af)
        for af in added:
            self._files.add(af)
        self._paths = new_paths
        return added, removed

    def clear(self) -> None:
        """Wipe the index (called when bitrate changes invalidate all durations)."""
        self._files.clear()
        self._paths.clear()


# ── Error helpers ─────────────────────────────────────────────────────────────

def _short_error(exc: Exception) -> str:
    """Return a concise human-readable error string from an exception."""
    msg = str(exc)
    # Strip long tracebacks or repeated class names embedded in message
    if "\n" in msg:
        msg = msg.splitlines()[0]
    # smbprotocol wraps errors like: SMBOSError: [Errno 111] Connection refused
    # or NtStatus 0xc000006d: Logon Failure
    for prefix in ("SMBOSError: ", "SMBException: ", "NtStatus "):
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
            break
    return msg[:160]


# ── Duration estimation ───────────────────────────────────────────────────────

def _estimate_duration(size: int, ext: str, sample_rate: int, bitrate: str | None) -> float:
    ext = ext.lower()
    if ext == "wav":
        data_bytes = max(size - 44, 0)
        bps = sample_rate * 2 * 2      # 2 ch, 16-bit
        return data_bytes / bps if bps else 3600.0
    if bitrate:
        try:
            kbps = float(bitrate.lower().rstrip("k"))
            return size / (kbps * 1000 / 8)
        except ValueError:
            pass
    return size / (128 * 1000 / 8)    # fallback 128 kbps


# ── FileIndexManager ──────────────────────────────────────────────────────────

class FileIndexManager:

    def __init__(self) -> None:
        self._indexes: dict[str, ChannelIndex] = {}
        self._broadcast: BroadcastFn | None = None
        self._task: asyncio.Task | None = None
        self._poll_interval: int = 10
        self.index_status: str = "idle"
        self.index_channels: list[dict] = []
        # Channels currently known to be unreachable — used to log errors once
        self._conn_failed: set[str] = set()

    # ── Setup ──────────────────────────────────────────────────────────────

    def setup(self, channels: list[ChannelConfig], poll_interval: int = 10,
              broadcast: BroadcastFn | None = None) -> None:
        self._broadcast = broadcast
        self._poll_interval = poll_interval
        self._indexes = {ch.id: ChannelIndex(ch) for ch in channels}
        self.index_status = "idle"
        self.index_channels = [
            {"id": ch.id, "name": ch.name, "files": 0,
             "done": False, "failed": False, "cached": False, "error": ""}
            for ch in channels
        ]
        # Apply previously detected audio params immediately
        detected = _load_detected()
        for ch in channels:
            if ch.id in detected:
                _apply_detected(ch, detected[ch.id])

    def get_index(self, channel_id: str) -> ChannelIndex | None:
        return self._indexes.get(channel_id)

    def get_state(self) -> dict:
        total_files = sum(len(idx._files) for idx in self._indexes.values())
        return {
            "status":          self.index_status,
            "total_channels":  len(self._indexes),
            "done_channels":   sum(1 for c in self.index_channels if c["done"]),
            "failed_channels": sum(1 for c in self.index_channels if c["failed"]),
            "total_files":     total_files,
            "channels":        list(self.index_channels),
        }

    # ── Scan helpers ───────────────────────────────────────────────────────

    async def _try_scan(self, idx: ChannelIndex,
                        max_retries: int = 2,
                        timeout: float = 30.0) -> tuple[bool, str]:
        """Scan with retries + per-attempt timeout. Returns (success, error_message)."""
        loop = asyncio.get_event_loop()
        last_error = ""
        for attempt in range(1, max_retries + 1):
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, idx.refresh),
                    timeout=timeout,
                )
                return True, ""
            except asyncio.TimeoutError:
                last_error = f"Timeout {int(timeout)}s"
                log.debug("Scan timeout (%ds) for %s (attempt %d/%d)",
                          int(timeout), idx.channel.id, attempt, max_retries)
            except Exception as exc:
                last_error = _short_error(exc)
                log.debug("Scan error for %s (attempt %d/%d): %s",
                          idx.channel.id, attempt, max_retries, exc)
        # All retries exhausted — caller decides whether to log ERROR
        return False, last_error

    async def probe_channel(self, idx: ChannelIndex) -> bool:
        """
        Probe audio params from first available file (cache or quick scan).
        Saves to detected_params.yaml and updates channel config in memory.
        Returns True if bitrate changed (stale cache must be cleared).
        """
        ch = idx.channel
        if ch.file_extension.lower() not in ("mp3",):
            return False            # WAV: duration from header, no probe needed
        if ch.bitrate is not None:
            return False            # Already configured by user

        loop = asyncio.get_event_loop()

        # Find a sample relative path to probe
        sample_rel: str | None = None

        if idx._files:
            sample_rel = list(idx._files)[0].rel_path
        else:
            # Quick scan of last 5 days to find any file
            today = date.today()
            for delta in range(5):
                d = today - timedelta(days=delta)
                try:
                    day_files = await loop.run_in_executor(
                        None, lambda d=d: idx._scan_date(d)
                    )
                    if day_files:
                        sample_rel = next(iter(day_files))
                        break
                except Exception:
                    pass

        if not sample_rel:
            return False

        # Probe up to 2 files for reliability
        for rel in ([sample_rel] +
                    [f.rel_path for f in list(idx._files)[1:2] if idx._files]):
            result = await loop.run_in_executor(
                None,
                lambda rel=rel: audio_probe.probe(
                    ch.local_path, ch.smb, rel, ch.file_extension
                )
            )
            if result:
                log.info("Auto-detected params for %s: %s", ch.id, result)
                changed = _apply_detected(ch, result)
                _save_detected(ch.id, result)
                return changed

        return False

    # ── Initial scan ───────────────────────────────────────────────────────

    async def initial_scan(self, progress: ProgressFn | None = None) -> None:
        total = len(self._indexes)

        # ── Phase 1: Fast load from disk cache ───────────────────────────
        total_cached = 0
        for idx in self._indexes.values():
            cached = disk_cache.load(idx.channel.id)
            if cached:
                for af in cached:
                    idx._files.add(af)
                    idx._paths.add(af.rel_path)
                n = len(cached)
                for entry in self.index_channels:
                    if entry["id"] == idx.channel.id:
                        entry["files"]  = n
                        entry["cached"] = True
                total_cached += n

        if total_cached:
            log.info("Loaded %d files from disk cache (%d channels)", total_cached, total)
            if progress:
                progress({
                    "type":        "cache_loaded",
                    "total_files": total_cached,
                    "channels":    total,
                })

        # ── Phase 2: Background rescan ────────────────────────────────────
        self.index_status = "scanning"
        log.info("Background rescan started — %d channel(s)", total)
        done = 0

        for idx in self._indexes.values():
            ch = idx.channel

            if progress:
                progress({
                    "type":         "index_scanning",
                    "done":         done,
                    "total":        total,
                    "channel_id":   ch.id,
                    "channel_name": ch.name,
                })

            # Probe audio params (only if bitrate unknown)
            bitrate_changed = await self.probe_channel(idx)
            if bitrate_changed:
                log.info("Bitrate updated for %s — clearing stale cache entries", ch.id)
                idx.clear()   # force fresh duration computation

            success, err_msg = await self._try_scan(idx)
            done += 1
            n_files = len(idx._files)

            if success:
                if ch.id in self._conn_failed:
                    log.info("Connection restored for channel %s", ch.id)
                    self._conn_failed.discard(ch.id)
                log.info("Channel %s: %d files (%d/%d)", ch.id, n_files, done, total)
                disk_cache.save(ch.id, list(idx._files))
            else:
                if ch.id not in self._conn_failed:
                    log.error("Channel %s unreachable: %s", ch.id, err_msg)
                    self._conn_failed.add(ch.id)

            for entry in self.index_channels:
                if entry["id"] == ch.id:
                    entry["files"]  = n_files
                    entry["done"]   = True
                    entry["failed"] = not success
                    entry["cached"] = False
                    entry["error"]  = err_msg

            if progress:
                msg: dict = {
                    "type":         "index_error" if not success else "index_progress",
                    "done":         done,
                    "total":        total,
                    "channel_id":   ch.id,
                    "channel_name": ch.name,
                    "files":        n_files,
                }
                if not success:
                    msg["error"] = err_msg
                progress(msg)

        self.index_status = "ready"
        total_files = sum(len(idx._files) for idx in self._indexes.values())
        failed = sum(1 for c in self.index_channels if c["failed"])
        log.info("Rescan complete — %d files, %d channels, %d failed",
                 total_files, total, failed)

        if progress:
            progress({
                "type":        "index_done",
                "total_files": total_files,
                "channels":    total,
                "failed":      failed,
            })

    # ── Poll loop ──────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(self._poll_interval)
            for idx in self._indexes.values():
                ch_id = idx.channel.id
                try:
                    added, removed = await loop.run_in_executor(None, idx.refresh)
                    if ch_id in self._conn_failed:
                        log.info("Connection restored for channel %s", ch_id)
                        self._conn_failed.discard(ch_id)
                    if (added or removed) and self._broadcast:
                        log.debug("Poll %s: +%d/-%d", ch_id, len(added), len(removed))
                        self._broadcast(
                            ch_id,
                            [{"start": a.start_dt.timestamp(), "end": a.end_dt.timestamp()}
                             for a in added],
                            [{"start": r.start_dt.timestamp(), "end": r.end_dt.timestamp()}
                             for r in removed],
                        )
                        for entry in self.index_channels:
                            if entry["id"] == ch_id:
                                entry["files"] = len(idx._files)
                        disk_cache.save(ch_id, list(idx._files))
                except Exception as exc:
                    if ch_id not in self._conn_failed:
                        log.error("Connection lost for channel %s: %s", ch_id, _short_error(exc))
                        self._conn_failed.add(ch_id)

    def start_polling(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())


file_index = FileIndexManager()
