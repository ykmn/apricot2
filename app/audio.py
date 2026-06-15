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

    All SMB files are downloaded in parallel as background tasks.
    ffmpeg starts on each file as soon as it is ready — without waiting
    for the rest — so the browser receives the first audio chunk as soon
    as the first file arrives.
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

    # Auto copy-mode: mp3/aac → no re-encoding needed for browser streaming.
    # WAV sources are always transcoded to mp3 for browser compatibility.
    native_ext = channel.file_extension.lower()
    if not copy_mode and out_format == native_ext and native_ext in ("mp3", "aac"):
        copy_mode = True

    with tempfile.TemporaryDirectory() as tmpdir:
        # Launch all downloads in parallel immediately — do not await yet.
        dl_tasks = [
            asyncio.create_task(_stage_one(i, af, len(files), tmpdir, channel))
            for i, af in enumerate(files)
        ]

        first_chunk = True
        try:
            # Process files in order: await each task, start ffmpeg as soon as ready.
            # While ffmpeg is processing file N, file N+1 is already downloading.
            for i, (af, dl_task) in enumerate(zip(files, dl_tasks)):
                local_path = await dl_task
                if not local_path:
                    continue

                is_first = (i == 0)
                is_last  = (i == len(files) - 1)
                ss       = max(0.0, (start - af.start_dt).total_seconds()) if is_first else 0.0
                outpoint = (end - af.start_dt).total_seconds() if is_last else None

                # Per-file concat list — reuses inpoint/outpoint trim logic.
                seg_concat = Path(tmpdir) / f"concat_{i:04d}.txt"
                with seg_concat.open("w", encoding="utf-8") as f:
                    f.write("ffconcat version 1.0\n")
                    f.write(f"file '{local_path.replace(chr(92), '/').replace(chr(39), chr(92)+chr(39))}'\n")
                    if ss > 0:
                        f.write(f"inpoint {ss}\n")
                    if outpoint is not None:
                        f.write(f"outpoint {outpoint}\n")

                cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0",
                       "-i", str(seg_concat), "-vn"]
                if copy_mode:
                    cmd += ["-c", "copy", "-f", _native_format(channel)]
                else:
                    if sample_rate:
                        cmd += ["-ar", str(sample_rate)]
                    if out_format == "wav":
                        cmd += ["-acodec", "pcm_s16le", "-f", "wav"]
                    elif out_format == "aac":
                        cmd += ["-acodec", "aac", "-b:a", bitrate, "-f", "adts"]
                    else:
                        cmd += ["-acodec", "libmp3lame", "-b:a", bitrate, "-f", "mp3"]
                cmd += ["pipe:1"]

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                if proc.stdout is None:
                    raise RuntimeError("ffmpeg subprocess stdout is None")
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

        finally:
            # Cancel any downloads still in progress (e.g. client disconnected).
            for task in dl_tasks:
                task.cancel()


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
                safe_p = p.replace("\\", "/").replace("'", "\\'")
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


async def _stage_one(
    i: int, af: AudioFile, total: int, tmpdir: str, channel: ChannelConfig,
) -> str | None:
    """Download a single audio file to tmpdir (or return its local path).

    Returns the local filesystem path on success, None on failure.
    """
    if not af.is_smb or channel.local_path:
        return af.path

    loop = asyncio.get_event_loop()
    local_copy = str(Path(tmpdir) / f"seg_{i:04d}.{channel.file_extension}")
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
            i + 1, total, rel, size_kb, elapsed,
            size_kb / elapsed if elapsed > 0 else 0,
            channel.smb.host if channel.smb else "local",
        )
        with open(local_copy, "wb") as f:
            f.write(data)
        return local_copy
    except Exception as exc:
        log.error("stage [%d/%d] failed %s: %s", i + 1, total, rel, exc)
        return None


async def _stage_files(
    files: list[AudioFile], tmpdir: str, channel: ChannelConfig
) -> list[str]:
    """Download all files in parallel. Used by export_audio."""
    results = await asyncio.gather(
        *(_stage_one(i, af, len(files), tmpdir, channel) for i, af in enumerate(files))
    )
    return [p for p in results if p is not None]


def _rel_from_af(af: AudioFile, channel: ChannelConfig) -> str:
    """Reconstruct the relative path for an AudioFile on an SMB source."""
    d = af.start_dt.date()
    folder = d.strftime(channel.folder_format)
    name = af.start_dt.strftime(channel.file_format) + f".{channel.file_extension}"
    return f"{folder}/{name}"


def _parse_range_header(range_header: str | None) -> tuple[int, int] | None:
    """Parse HTTP Range header (bytes=start-end). Returns (start, end) or None."""
    if not range_header or not range_header.startswith("bytes="):
        return None
    try:
        range_spec = range_header[6:].strip()
        start_str, end_str = range_spec.split("-", 1)
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else None
        return (start, end)
    except Exception:
        return None


def _format_content_range(start: int, end: int, total: int) -> str:
    """Format Content-Range header value: bytes start-end/total"""
    return f"bytes {start}-{end}/{total}"


def _bytes_to_seconds(byte_offset: int, bitrate_kbps: int) -> float:
    """Convert byte offset to seconds based on bitrate (kbps)."""
    return byte_offset / (bitrate_kbps * 1000 / 8)


async def stream_audio_single_file(
    channel: ChannelConfig,
    af: AudioFile,
    file_start_offset: float,
    file_end_offset: float,
    out_format: str = "mp3",
    bitrate: str = "192k",
    sample_rate: int | None = None,
    copy_mode: bool = False,
    range_start: int | None = None,
    range_end: int | None = None,
) -> tuple[AsyncGenerator[bytes, None], dict]:
    """
    Stream a single audio file with optional HTTP Range support.

    Returns (async_generator, metadata_dict) where metadata contains:
    - content_length: total bytes in response
    - content_range: Content-Range header value (if Range request)
    - accept_ranges: "bytes"
    - file_size: total file size in bytes
    """
    # Determine if this is a Range request
    is_range_request = range_start is not None

    # Get file size for Content-Length
    file_size = smb.getsize(None, channel.smb, af.rel_path) if af.is_smb else Path(af.path).stat().st_size

    # Determine bitrate for byte<->second conversion
    native_ext = channel.file_extension.lower()
    kbps = None
    if channel.bitrate:
        try:
            kbps = float(channel.bitrate.lower().rstrip("k"))
        except ValueError:
            pass
    if kbps is None:
        kbps = 128  # fallback

    # If Range request, convert byte range to time offset
    if is_range_request:
        # Convert range bytes to seconds offset from file start
        range_start_sec = _bytes_to_seconds(range_start, kbps)
        range_end_sec = _bytes_to_seconds(range_end, kbps) if range_end is not None else None

        # Clamp to the requested file segment
        file_start_offset = max(file_start_offset, range_start_sec)
        if range_end_sec is not None:
            file_end_offset = min(file_end_offset, range_end_sec)

    # Calculate actual byte range for response
    response_start_byte = int(file_start_offset * kbps * 1000 / 8)
    if file_end_offset:
        response_end_byte = min(int(file_end_offset * kbps * 1000 / 8), file_size - 1)
    else:
        response_end_byte = file_size - 1

    content_length = response_end_byte - response_start_byte + 1
    content_range = _format_content_range(response_start_byte, response_end_byte, file_size) if is_range_request else None

    # Build ffmpeg command
    native_fmt = _native_format(channel)

    # Auto copy-mode
    if not copy_mode and out_format == native_ext and native_ext in ("mp3", "aac"):
        copy_mode = True

    if af.is_smb:
        input_path = smb.full_path(None, channel.smb, af.rel_path)
    else:
        input_path = af.path

    # Use -ss before -i for fast seeking (input seeking)
    ss_offset = file_start_offset
    duration = (file_end_offset - file_start_offset) if file_end_offset else None

    cmd = [FFMPEG, "-y", "-ss", str(ss_offset)]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-i", input_path, "-vn"]

    if copy_mode:
        cmd += ["-c", "copy", "-f", native_fmt]
    else:
        if sample_rate:
            cmd += ["-ar", str(sample_rate)]
        if out_format == "wav":
            cmd += ["-acodec", "pcm_s16le", "-f", "wav"]
        elif out_format == "aac":
            cmd += ["-acodec", "aac", "-b:a", bitrate, "-f", "adts"]
        else:
            cmd += ["-acodec", "libmp3lame", "-b:a", bitrate, "-f", "mp3"]
    cmd += ["pipe:1"]

    log.info(
        "stream_single_file: channel=%s file=%s ss=%.3f dur=%s copy_mode=%s range=%s-%s",
        channel.id, af.rel_path, ss_offset, duration, copy_mode, range_start, range_end,
    )

    async def gen() -> AsyncGenerator[bytes, None]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdout is None:
            raise RuntimeError("ffmpeg subprocess stdout is None")
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    metadata = {
        "content_length": content_length,
        "content_range": content_range,
        "accept_ranges": "bytes",
        "file_size": file_size,
    }

    return gen, metadata
