"""
Audio streaming and export via ffmpeg.
Handles multi-file concatenation for ranges that span several audio files.

SMB streaming uses pipe mode: files are opened as SMB streams, seeked to the
correct byte offset, and fed to ffmpeg stdin — no full-file download required.
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


def _parse_bitrate_bps(bitrate_str: str | None) -> int:
    """Parse '192k' → 192000. Returns 0 if unknown."""
    if not bitrate_str:
        return 0
    s = bitrate_str.lower().strip()
    try:
        return int(float(s.rstrip("k")) * (1000 if s.endswith("k") else 1))
    except ValueError:
        return 0


def _byte_offset_for_seek(ss: float, channel: ChannelConfig) -> int:
    """Approximate byte offset for seeking ss seconds into a CBR audio file.

    For CBR MP3/AAC: offset = bitrate_bps/8 * ss
    For WAV: offset = 44-byte header + PCM bytes

    Returns 0 when unknown — safe fallback, ffmpeg will decode from stream start.
    The SMB seek() translates to a direct SMB READ at that offset, so we skip
    transmitting the unneeded prefix entirely.
    """
    if ss <= 0:
        return 0
    ext = channel.file_extension.lower()
    if ext == "wav":
        return 44 + int(channel.sample_rate * 2 * 2 * ss)   # 16-bit stereo PCM
    bps = _parse_bitrate_bps(channel.bitrate)
    if bps > 0:
        return int(bps / 8 * ss)
    return 0


async def _pipe_smb_segment(
    af: AudioFile,
    channel: ChannelConfig,
    ss: float,
    duration: float | None,
    copy_mode: bool,
    out_format: str,
    bitrate: str,
    sample_rate: int | None,
) -> AsyncGenerator[bytes, None]:
    """Stream one SMB audio file to ffmpeg via stdin pipe, without a temp copy.

    ss       — seconds to skip from the beginning of this file
    duration — seconds to output (None = play until EOF)

    Opens the file and validates the byte offset against the actual file size
    before starting ffmpeg.  If the estimated offset would overshoot EOF
    (e.g. old file recorded at a different bitrate), falls back to ffmpeg -ss
    so ffmpeg decodes-and-discards the prefix instead — slower but always correct.
    """
    rel = _rel_from_af(af, channel)
    loop = asyncio.get_event_loop()

    # Open the SMB file and determine the actual seek position.
    # We check the file size before seeking so a stale channel.bitrate value
    # (auto-detected from recent files) cannot cause the offset to overshoot EOF
    # on older files recorded at a different bitrate — which would make fh.read()
    # return empty bytes immediately and cause ffmpeg to produce no output.
    est_offset = _byte_offset_for_seek(ss, channel)

    def _open_file():
        fh = smb.open_file(None, channel.smb, rel)
        if est_offset <= 0:
            return fh, 0
        try:
            fh.seek(0, 2)                   # move to end → current pos = file size
            file_size = fh.tell()
            if est_offset < file_size:
                fh.seek(est_offset)
                return fh, est_offset
            # Offset overshoots: old file with different bitrate
            log.debug(
                "pipe seek %d >= file_size %d for %s — using ffmpeg -ss fallback",
                est_offset, file_size, rel,
            )
            fh.seek(0)
            return fh, 0
        except Exception:
            # seek(0,2) failed on this SMB implementation — just start from 0
            try:
                fh.seek(0)
            except Exception:
                pass
            return fh, 0

    try:
        fh, byte_offset = await loop.run_in_executor(None, _open_file)
    except Exception as exc:
        log.error("pipe open error %s: %s", rel, exc)
        return

    # If byte seek covered ss, no additional ffmpeg -ss needed.
    # If we fell back to offset=0, pass ss to ffmpeg for internal decode-seek.
    fine_ss = 0.0 if (byte_offset > 0 or ss <= 0) else ss

    ext = channel.file_extension.lower()
    in_fmt = {"wav": "wav", "aac": "adts"}.get(ext, "mp3")

    cmd = [FFMPEG, "-y", "-f", in_fmt]
    if fine_ss > 0:
        cmd += ["-ss", f"{fine_ss:.3f}"]
    cmd += ["-i", "pipe:0", "-vn"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]

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
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def feed_stdin() -> None:
        try:
            while True:
                chunk = await loop.run_in_executor(None, fh.read, 65536)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except Exception as exc:
            log.error("pipe feed error %s: %s", rel, exc)
        finally:
            await loop.run_in_executor(None, fh.close)
            try:
                proc.stdin.close()
            except Exception:
                pass

    async def drain_stderr() -> None:
        # ffmpeg stderr MUST be continuously read; if the OS pipe buffer (~64 KB)
        # fills up, ffmpeg blocks on the write, which in turn prevents it from
        # reading stdin or writing stdout — causing a deadlock.
        try:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                log.debug("ffmpeg stderr: %s", line.decode("utf-8", errors="replace").rstrip())
        except Exception:
            pass

    feed_task   = asyncio.create_task(feed_stdin())
    stderr_task = asyncio.create_task(drain_stderr())
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            yield chunk
    finally:
        feed_task.cancel()
        stderr_task.cancel()
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


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

    For SMB channels: each file is opened as a stream and seeked to the byte
    offset matching the start trim; no full-file download is required.  ffmpeg
    starts processing immediately and the browser receives the first audio chunk
    within seconds.

    For local/mounted channels: files are staged to a temp dir in parallel
    (pre-fetching file N+1 while ffmpeg processes file N).
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

    # ── SMB pipe mode ─────────────────────────────────────────────────────────
    # For pure SMB channels (no local mount), stream each file directly from the
    # share into ffmpeg stdin.  The SMB file handle is seeked to the byte offset
    # corresponding to the start trim, so the unneeded prefix is never fetched.
    # This lets the browser receive the first audio chunk almost immediately
    # instead of waiting for the whole file to download.
    if channel.smb is not None and not channel.local_path:
        first_chunk = True
        for i, af in enumerate(files):
            is_first = (i == 0)
            is_last  = (i == len(files) - 1)
            ss       = max(0.0, (start - af.start_dt).total_seconds()) if is_first else 0.0
            outpoint = (end - af.start_dt).total_seconds() if is_last else None
            duration = (outpoint - ss) if outpoint is not None else None

            log.debug(
                "stream_audio pipe [%d/%d] %s ss=%.1f dur=%s byte_off=%d",
                i + 1, len(files), af.rel_path, ss,
                f"{duration:.1f}" if duration is not None else "EOF",
                _byte_offset_for_seek(ss, channel),
            )

            async for chunk in _pipe_smb_segment(
                af, channel, ss, duration, copy_mode, out_format, bitrate, sample_rate
            ):
                if first_chunk:
                    log.info(
                        "stream_audio first_chunk (pipe): channel=%s in %.2fs",
                        channel.id, time.monotonic() - t0,
                    )
                    first_chunk = False
                yield chunk
        return

    # ── Local / mounted path ──────────────────────────────────────────────────
    # Launch all downloads in parallel immediately — do not await yet.
    with tempfile.TemporaryDirectory() as tmpdir:
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
