"""
Audio streaming and export via ffmpeg.
Handles multi-file concatenation for ranges that span several audio files.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from . import smb_client as smb
from .app_logger import get_logger
from .file_index import file_index
from .models import AudioFile, ChannelConfig

log = get_logger("audio")

FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")


def _ffmpeg_ok() -> bool:
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False


def set_ffmpeg_path(path: str) -> None:
    global FFMPEG
    FFMPEG = path


def _native_format(channel: ChannelConfig) -> str:
    """Return the container format string ffmpeg expects for channel's native ext."""
    ext = channel.file_extension.lower()
    if ext == "wav":  return "wav"
    if ext == "aac":  return "adts"
    return "mp3"


async def stream_audio(
    channel: ChannelConfig,
    start: datetime,
    end: datetime,
    out_format: str = "mp3",
    bitrate: str = "192k",
    sample_rate: int | None = None,
    copy_mode: bool = False,
) -> AsyncGenerator[bytes, None]:
    """
    Yield audio bytes for the requested time range in the requested format.
    Uses ffmpeg to convert/concatenate source files.
    copy_mode=True streams without re-encoding (only valid when out_format
    matches the channel's native format).
    """
    idx = file_index.get_index(channel.id)
    if idx is None:
        return

    files = idx.files_for_range(start, end)
    if not files:
        return

    log.info(
        "stream_audio start: channel=%s files=%d start=%s end=%s copy_mode=%s",
        channel.id, len(files), start.strftime("%H:%M:%S"), end.strftime("%H:%M:%S"), copy_mode,
    )
    t0 = time.monotonic()

    # Build ffmpeg input list
    # For SMB files we need to download to temp first if not locally accessible
    with tempfile.TemporaryDirectory() as tmpdir:
        input_paths = await _stage_files(files, tmpdir, channel)
        if not input_paths:
            return
        t_staged = time.monotonic()
        log.info("stream_audio staged: channel=%s files=%d in %.2fs",
                 channel.id, len(input_paths), t_staged - t0)

        concat_list = Path(tmpdir) / "concat.txt"

        # Calculate trim offsets relative to the first file's start time.
        first_file = files[0]
        last_file  = files[-1]
        ss = max(0.0, (start - first_file.start_dt).total_seconds())

        # Auto copy-mode: mp3 and aac don't need re-encoding for streaming;
        # wav sources are always transcoded to mp3 for browser compatibility.
        native_ext = channel.file_extension.lower()
        if not copy_mode and out_format == native_ext and native_ext in ("mp3", "aac"):
            copy_mode = True

        # Use inpoint/outpoint directives in the concat script — the concat
        # demuxer honours them for fast seeking without decoding from the start.
        # Paths are normalised to forward slashes so ffmpeg parses them correctly
        # on both Windows (UNC paths) and Linux.
        last_file_duration = (end - last_file.start_dt).total_seconds()
        with concat_list.open("w", encoding="utf-8") as f:
            f.write("ffconcat version 1.0\n")
            for idx_f, p in enumerate(input_paths):
                safe_p = p.replace("\\", "/")
                f.write(f"file '{safe_p}'\n")
                if idx_f == 0 and ss > 0:
                    f.write(f"inpoint {ss}\n")
                if idx_f == len(input_paths) - 1:
                    f.write(f"outpoint {last_file_duration}\n")

        cmd = [
            FFMPEG,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-vn",
        ]

        if copy_mode:
            # Stream-copy: no transcoding, preserve original codec and quality
            fmt = _native_format(channel)
            cmd += ["-c", "copy", "-f", fmt]
        else:
            if sample_rate:
                cmd += ["-ar", str(sample_rate)]
            if out_format == "wav":
                cmd += ["-acodec", "pcm_s16le", "-f", "wav"]
            elif out_format == "aac":
                cmd += ["-acodec", "aac", "-b:a", bitrate, "-f", "adts"]
            else:  # mp3
                cmd += ["-acodec", "libmp3lame", "-b:a", bitrate, "-f", "mp3"]

        cmd += ["pipe:1"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None

        first_chunk = True
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                if first_chunk:
                    log.info("stream_audio first_chunk: channel=%s in %.2fs total",
                             channel.id, time.monotonic() - t0)
                    first_chunk = False
                yield chunk
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def export_audio(
    channel: ChannelConfig,
    start: datetime,
    end: datetime,
    out_format: str = "mp3",
    bitrate: str = "192k",
    sample_rate: int | None = None,
    out_path: str | None = None,
    copy_mode: bool = False,
) -> str:
    """Export audio segment directly to a file via ffmpeg.

    Unlike stream_audio (which pipes to stdout), this writes to a real file so
    ffmpeg can seek back and write a correct WAV/RIFF header with accurate size.
    """
    if out_path is None:
        ts = start.strftime("%Y%m%d_%H%M%S")
        fname = f"{channel.id}_{ts}.{out_format}"
        out_path = str(Path(tempfile.gettempdir()) / fname)

    idx = file_index.get_index(channel.id)
    if idx is None:
        raise RuntimeError(f"No file index for channel {channel.id!r}")

    files = idx.files_for_range(start, end)
    if not files:
        raise RuntimeError(f"No audio files found for {channel.id!r} in requested range")

    with tempfile.TemporaryDirectory() as tmpdir:
        input_paths = await _stage_files(files, tmpdir, channel)
        if not input_paths:
            raise RuntimeError(f"Failed to stage audio files for {channel.id!r}")

        concat_list = Path(tmpdir) / "concat.txt"
        first_file = files[0]
        last_file  = files[-1]
        ss = max(0.0, (start - first_file.start_dt).total_seconds())
        last_file_duration = (end - last_file.start_dt).total_seconds()

        native_ext = channel.file_extension.lower()
        if not copy_mode and out_format == native_ext and native_ext in ("mp3", "aac"):
            copy_mode = True

        with concat_list.open("w", encoding="utf-8") as f:
            f.write("ffconcat version 1.0\n")
            for idx_f, p in enumerate(input_paths):
                safe_p = p.replace("\\", "/")
                f.write(f"file '{safe_p}'\n")
                if idx_f == 0 and ss > 0:
                    f.write(f"inpoint {ss}\n")
                if idx_f == len(input_paths) - 1:
                    f.write(f"outpoint {last_file_duration}\n")

        cmd = [
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-vn",
        ]

        if copy_mode:
            fmt = _native_format(channel)
            cmd += ["-c", "copy", "-f", fmt]
        else:
            if sample_rate:
                cmd += ["-ar", str(sample_rate)]
            if out_format == "wav":
                cmd += ["-acodec", "pcm_s16le", "-f", "wav"]
            elif out_format == "aac":
                cmd += ["-acodec", "aac", "-b:a", bitrate, "-f", "adts"]
            else:
                cmd += ["-acodec", "libmp3lame", "-b:a", bitrate, "-f", "mp3"]

        # Write directly to file — ffmpeg can seek back to fix WAV/RIFF header size
        cmd += [out_path]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
            # Keep only last 10 lines — ffmpeg outputs a lot of progress info
            last_lines = "\n".join(stderr_text.splitlines()[-10:])
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}:\n{last_lines}")

    return out_path


async def _stage_files(
    files: list[AudioFile], tmpdir: str, channel: ChannelConfig
) -> list[str]:
    """
    Make audio files accessible on the local filesystem.
    If they're already local, return their paths as-is.
    If SMB, copy to tmpdir.
    """
    loop = asyncio.get_event_loop()
    results: list[str | None] = [None] * len(files)

    async def _download_one(i: int, af: AudioFile) -> None:
        if not af.is_smb or channel.local_path:
            results[i] = af.path
            return
        ext = channel.file_extension
        local_copy = str(Path(tmpdir) / f"seg_{i:04d}.{ext}")
        rel = _rel_from_af(af, channel)
        t_dl = time.monotonic()
        try:
            data = await loop.run_in_executor(
                None, lambda rel=rel: smb.read_bytes(None, channel.smb, rel)
            )
            elapsed = time.monotonic() - t_dl
            size_kb = len(data) / 1024
            log.info(
                "stage [%d/%d] %s: %.0f KB in %.2fs (%.0f KB/s) host=%s",
                i + 1, len(files), rel, size_kb, elapsed,
                size_kb / elapsed if elapsed > 0 else 0,
                channel.smb.host if channel.smb else "local",
            )
            with open(local_copy, "wb") as f:
                f.write(data)
            results[i] = local_copy
        except Exception as exc:
            log.error("stage [%d/%d] failed %s: %s", i + 1, len(files), rel, exc)

    await asyncio.gather(*(_download_one(i, af) for i, af in enumerate(files)))
    return [p for p in results if p is not None]


def _rel_from_af(af: AudioFile, channel: ChannelConfig) -> str:
    """Reconstruct the relative path for an AudioFile on an SMB source."""
    d = af.start_dt.date()
    folder = d.strftime(channel.folder_format)
    name = af.start_dt.strftime(channel.file_format) + f".{channel.file_extension}"
    return f"{folder}/{name}"
