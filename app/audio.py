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
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from . import smb_client as smb
from .file_index import file_index
from .models import AudioFile, ChannelConfig

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

    # Build ffmpeg input list
    # For SMB files we need to download to temp first if not locally accessible
    with tempfile.TemporaryDirectory() as tmpdir:
        input_paths = await _stage_files(files, tmpdir, channel)
        if not input_paths:
            return

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
        # This is correct unlike -ss before -i (which concat demuxer ignores).
        last_file_duration = (end - last_file.start_dt).total_seconds()
        with concat_list.open("w") as f:
            f.write("ffconcat version 1.0\n")
            for idx_f, p in enumerate(input_paths):
                f.write(f"file '{p}'\n")
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
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None

        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
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
    """Export audio segment to a file. Returns the output file path."""
    if out_path is None:
        ts = start.strftime("%Y%m%d_%H%M%S")
        fname = f"{channel.id}_{ts}.{out_format}"
        out_path = str(Path(tempfile.gettempdir()) / fname)

    with open(out_path, "wb") as fout:
        async for chunk in stream_audio(
            channel, start, end, out_format, bitrate, sample_rate, copy_mode
        ):
            fout.write(chunk)

    return out_path


async def _stage_files(
    files: list[AudioFile], tmpdir: str, channel: ChannelConfig
) -> list[str]:
    """
    Make audio files accessible on the local filesystem.
    If they're already local, return their paths as-is.
    If SMB, copy to tmpdir.
    """
    paths = []
    loop = asyncio.get_event_loop()
    for i, af in enumerate(files):
        if not af.is_smb or channel.local_path:
            paths.append(af.path)
        else:
            ext = channel.file_extension
            local_copy = str(Path(tmpdir) / f"seg_{i:04d}.{ext}")
            try:
                data = await loop.run_in_executor(
                    None, lambda: smb.read_bytes(None, channel.smb, _rel_from_af(af, channel))
                )
                with open(local_copy, "wb") as f:
                    f.write(data)
                paths.append(local_copy)
            except Exception as exc:
                print(f"[audio] failed to stage {af.path}: {exc}")
    return paths


def _rel_from_af(af: AudioFile, channel: ChannelConfig) -> str:
    """Reconstruct the relative path for an AudioFile on an SMB source."""
    d = af.start_dt.date()
    folder = d.strftime(channel.folder_format)
    name = af.start_dt.strftime(channel.file_format) + f".{channel.file_extension}"
    return f"{folder}/{name}"
