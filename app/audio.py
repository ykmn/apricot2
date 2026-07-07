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
import sys
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


def _is_local_path_usable(local_path: str) -> bool:
    """Return True if local_path is actually accessible on the current OS.

    A Windows UNC path (starts with \\) is unusable on Linux/macOS even if
    the channel config has it set — in that case we must fall back to SMB.
    """
    if sys.platform == "win32":
        return True
    # On POSIX: backslash-style UNC paths (\\server\share) are not filesystem paths
    return not local_path.startswith("\\\\")


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


async def _drain_stderr_local(proc: asyncio.subprocess.Process, label: str) -> None:
    """Drain ffmpeg stderr for staging-path invocations to prevent pipe buffer deadlock."""
    try:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if any(kw in text for kw in ("Error", "error", "Invalid", "invalid", "Could not", "No such")):
                log.info("ffmpeg stage [%s]: %s", label, text)
            else:
                log.debug("ffmpeg stage [%s]: %s", label, text)
    except Exception:
        pass


async def _direct_smb_segment(
    af: AudioFile,
    channel: ChannelConfig,
    ss: float,
    duration: float | None,
) -> AsyncGenerator[bytes, None]:
    """Stream SMB file bytes directly to the caller — no ffmpeg involved.

    Used for native-format copy mode (MP3→MP3, AAC→AAC).  Both formats are
    self-framing, so raw bytes are playable by browsers without any container
    header.  Start/end trimming is approximate to the nearest CBR frame; for
    audio monitoring this is indistinguishable from precise trimming.

    Eliminates ffmpeg process startup (~100 ms), pipe setup, and the
    analyzeduration probe delay, giving near-instant first audio.
    """
    rel = af.rel_path
    loop = asyncio.get_running_loop()
    est_offset = _byte_offset_for_seek(ss, channel)

    # Bytes to send: bitrate * duration / 8, or None = read to EOF.
    byte_count: int | None = None
    if duration is not None:
        ext = channel.file_extension.lower()
        if ext == "wav":
            byte_count = int(channel.sample_rate * 2 * 2 * duration)
        else:
            bps = _parse_bitrate_bps(channel.bitrate)
            if bps > 0:
                byte_count = int(bps / 8 * duration)

    def _open_direct():
        file_size = -1
        if est_offset > 0:
            try:
                file_size = smb.getsize(None, channel.smb, rel)
            except Exception:
                pass
        fh = smb.open_file(None, channel.smb, rel)
        actual_offset = 0
        if est_offset > 0:
            try:
                if file_size < 0 or est_offset < file_size:
                    fh.seek(est_offset)
                    actual_offset = est_offset
            except Exception:
                pass
        return fh, actual_offset

    try:
        fh, actual_offset = await loop.run_in_executor(None, _open_direct)
    except Exception as exc:
        log.error("direct open error %s: %s", rel, exc)
        return

    log.debug(
        "direct segment: %s  byte_off=%d  byte_count=%s",
        rel, actual_offset, byte_count,
    )

    # First chunk is small so the browser receives audio immediately even on
    # slow SMB links; subsequent chunks are large to reduce round-trips.
    FIRST_READ = 32768   # 32 KB  — arrives in ~0.25 s at 1 Mbit/s
    READ_SIZE  = 524288  # 512 KB — efficient bulk reads after start
    remaining = byte_count
    chunks_yielded = 0
    try:
        while True:
            size = FIRST_READ if chunks_yielded == 0 else READ_SIZE
            to_read = min(size, remaining) if remaining is not None else size
            chunk = await loop.run_in_executor(None, fh.read, to_read)
            if not chunk:
                break
            chunks_yielded += 1
            yield chunk
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    finally:
        await loop.run_in_executor(None, fh.close)
        log.debug("direct segment done: %s  chunks=%d", rel, chunks_yielded)


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
    # Use af.rel_path (the path found during scanning) rather than reconstructing
    # it from the channel format templates — the two can diverge if the channel
    # config was updated after old files were indexed.
    rel = af.rel_path
    loop = asyncio.get_running_loop()

    # Open the SMB file and determine the actual seek position.
    # We check the file size before seeking so a stale channel.bitrate value
    # (auto-detected from recent files) cannot cause the offset to overshoot EOF
    # on older files recorded at a different bitrate — which would make fh.read()
    # return empty bytes immediately and cause ffmpeg to produce no output.
    est_offset = _byte_offset_for_seek(ss, channel)

    def _open_file():
        # Get file size via stat (one SMB QueryInfo call) before opening,
        # so we can validate the byte offset without seek(0,2) which may
        # read through the entire file on some SMB implementations.
        file_size = -1
        if est_offset > 0:
            try:
                file_size = smb.getsize(None, channel.smb, rel)
            except Exception:
                pass

        fh = smb.open_file(None, channel.smb, rel)
        if est_offset <= 0:
            return fh, 0, file_size
        try:
            if file_size > 0 and est_offset < file_size:
                fh.seek(est_offset)
                return fh, est_offset, file_size
            # Offset overshoots or size unknown: start from 0, let ffmpeg -ss handle it
            return fh, 0, file_size
        except Exception:
            # seek failed on this SMB implementation — start from 0
            return fh, 0, file_size

    try:
        fh, byte_offset, file_size = await loop.run_in_executor(None, _open_file)
    except Exception as exc:
        log.error("pipe open error %s: %s", rel, exc)
        return

    if file_size == 0:
        log.warning("pipe segment skipped — file is empty: %s", rel)
        return

    if byte_offset == 0 and est_offset > 0:
        log.info(
            "pipe seek overshoot for %s: est_offset=%d file_size=%d ss=%.1f — ffmpeg -ss fallback",
            rel, est_offset, file_size, ss,
        )

    log.debug(
        "pipe segment: %s  size=%d  byte_off=%d  ss=%.1f  dur=%s",
        rel, file_size, byte_offset, ss,
        f"{duration:.1f}" if duration is not None else "EOF",
    )

    # If byte seek covered ss, no additional ffmpeg -ss needed.
    # If we fell back to offset=0, pass ss to ffmpeg for internal decode-seek.
    fine_ss = 0.0 if (byte_offset > 0 or ss <= 0) else ss

    # ffmpeg's demuxer for raw ADTS AAC streams is registered as "aac", not
    # "adts" — "adts" is only the name of the ADTS *muxer* (output format).
    # Passing "adts" as an input -f value fails with "Unknown input format".
    # Some HLS-TS captures are still MPEG-TS-wrapped under a ".aac" extension;
    # channel.aac_input_format holds whichever demuxer audio_probe detected
    # for this channel's files ("aac" or "mpegts"), falling back to "aac".
    ext = channel.file_extension.lower()
    if ext == "aac":
        in_fmt = channel.aac_input_format or "aac"
    else:
        in_fmt = {"wav": "wav"}.get(ext, "mp3")

    cmd = [FFMPEG, "-y", "-f", in_fmt]
    # Disable ffmpeg's default 5-second probe buffer for pipe inputs.
    # We declare the format explicitly with -f, so no analysis is needed.
    cmd += ["-analyzeduration", "0", "-probesize", "32"]
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
                text = line.decode("utf-8", errors="replace").rstrip()
                # Log actual ffmpeg errors/warnings at INFO so they appear in the console;
                # progress lines (size=, time=, speed=) are noisy — suppress them.
                if any(kw in text for kw in ("Error", "error", "Invalid", "invalid", "Could not", "No such")):
                    log.info("ffmpeg [%s]: %s", rel, text)
                else:
                    log.debug("ffmpeg [%s]: %s", rel, text)
        except Exception:
            pass

    feed_task   = asyncio.create_task(feed_stdin())
    stderr_task = asyncio.create_task(drain_stderr())
    chunks_yielded = 0
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            chunks_yielded += 1
            yield chunk
    finally:
        feed_task.cancel()
        stderr_task.cancel()
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        rc = await proc.wait()
        if chunks_yielded == 0:
            log.warning(
                "pipe segment produced no output: %s  rc=%d  byte_off=%d  fine_ss=%.1f",
                rel, rc, byte_offset, fine_ss,
            )
        else:
            log.debug("pipe segment done: %s  chunks=%d  rc=%d", rel, chunks_yielded, rc)


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
    #
    # Also use pipe mode when local_path is a Windows UNC path (\\server\share)
    # running on a non-Windows OS — such paths are not usable as local filesystem
    # paths and the SMB protocol must be used instead.
    _use_pipe = (
        channel.smb is not None
        and (not channel.local_path or not _is_local_path_usable(channel.local_path))
    )
    # For native copy mode (MP3→MP3, AAC→AAC), stream bytes directly without
    # ffmpeg — eliminates process startup, pipe setup, and probe buffering.
    # WAV is excluded: browsers require a valid RIFF header at offset 0.
    # AAC files whose detected container is MPEG-TS are also excluded: raw
    # TS bytes are not a playable ADTS elementary stream, so they must go
    # through ffmpeg (pipe mode) to be demuxed/remuxed to ADTS.
    _use_direct = (
        _use_pipe and copy_mode and native_ext in ("mp3", "aac")
        and channel.aac_input_format != "mpegts"
    )

    if _use_pipe:
        first_chunk = True
        for i, af in enumerate(files):
            is_first = (i == 0)
            is_last  = (i == len(files) - 1)
            ss       = max(0.0, (start - af.start_dt).total_seconds()) if is_first else 0.0
            outpoint = (end - af.start_dt).total_seconds() if is_last else None
            duration = (outpoint - ss) if outpoint is not None else None

            log.debug(
                "stream_audio %s [%d/%d] %s ss=%.1f dur=%s byte_off=%d",
                "direct" if _use_direct else "pipe",
                i + 1, len(files), af.rel_path, ss,
                f"{duration:.1f}" if duration is not None else "EOF",
                _byte_offset_for_seek(ss, channel),
            )

            seg_gen = (
                _direct_smb_segment(af, channel, ss, duration)
                if _use_direct else
                _pipe_smb_segment(af, channel, ss, duration, copy_mode, out_format, bitrate, sample_rate)
            )
            async for chunk in seg_gen:
                if first_chunk:
                    log.info(
                        "stream_audio first_chunk (%s): channel=%s in %.2fs",
                        "direct" if _use_direct else "pipe",
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

                log.debug(
                    "stream_audio stage [%d/%d] %s ss=%.1f out=%s",
                    i + 1, len(files), local_path, ss,
                    f"{outpoint:.1f}" if outpoint is not None else "EOF",
                )

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                if proc.stdout is None:
                    raise RuntimeError("ffmpeg subprocess stdout is None")

                seg_stderr = asyncio.create_task(
                    _drain_stderr_local(proc, local_path)
                )
                seg_chunks = 0
                try:
                    while True:
                        chunk = await proc.stdout.read(65536)
                        if not chunk:
                            break
                        seg_chunks += 1
                        if first_chunk:
                            log.info("stream_audio first_chunk: channel=%s in %.2fs total",
                                     channel.id, time.monotonic() - t0)
                            first_chunk = False
                        yield chunk
                finally:
                    seg_stderr.cancel()
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    rc = await proc.wait()
                    if seg_chunks == 0:
                        log.warning(
                            "stage segment produced no output: %s  rc=%d  ss=%.1f  out=%s",
                            local_path, rc, ss,
                            f"{outpoint:.1f}" if outpoint is not None else "EOF",
                        )

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
        # Guard against stale disk-cache entries that have Windows UNC paths
        # (e.g. \\server\share\...) on a Linux/macOS server.  Such entries were
        # written when the server last ran on Windows; the local_path in the
        # channel config is now a proper POSIX mount, but the cached af.path
        # still carries the old Windows UNC style.  Fall through to SMB download
        # when channel.smb is available and the stored path is unusable here.
        if not channel.smb or _is_local_path_usable(af.path):
            return af.path
        log.debug(
            "_stage_one: cached path not usable on this OS (%s), falling back to SMB", af.path
        )

    loop = asyncio.get_running_loop()
    local_copy = str(Path(tmpdir) / f"seg_{i:04d}.{channel.file_extension}")
    rel = af.rel_path
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
