"""
Probe audio file parameters using mutagen.
Reads the first 64 KB into a BytesIO buffer to avoid SMB seek issues.
"""
from __future__ import annotations

import io
from typing import Optional

from .app_logger import get_logger

log = get_logger("audio_probe")

try:
    import mutagen.aac
    import mutagen.mp3
    import mutagen.wave
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False
    log.warning("mutagen not available — audio auto-detection disabled")

# MP3 mode constants → human-readable string
_MP3_MODES: dict = {}
if HAS_MUTAGEN:
    _MP3_MODES = {
        mutagen.mp3.STEREO:       "Stereo",
        mutagen.mp3.JOINTSTEREO:  "Joint Stereo",
        mutagen.mp3.DUALCHANNEL:  "Dual Channel",
        mutagen.mp3.MONO:         "Mono",
    }

READ_BYTES = 65536   # 64 KB — enough for ID3 tag + first MP3 frame


def _detect_aac_container(raw: bytes) -> str | None:
    """Sniff the actual container of a ".aac" file.

    Files captured from an HLS-TS source are sometimes saved as raw ADTS AAC
    and sometimes as still-MPEG-TS-wrapped segments under a ".aac" extension
    — ffmpeg needs a different demuxer for each ("aac" vs "mpegts"), and
    guessing wrong fails with "Unknown input format" / no output.

    Returns the ffmpeg -f value to use for this file, or None if neither
    pattern is recognized in the given bytes.
    """
    offset = 0
    if raw[:3] == b"ID3" and len(raw) >= 10:
        # ID3v2 header: 3 bytes "ID3" + 2 version + 1 flags + 4 synchsafe size
        size = ((raw[6] & 0x7F) << 21) | ((raw[7] & 0x7F) << 14) | \
               ((raw[8] & 0x7F) << 7) | (raw[9] & 0x7F)
        offset = 10 + size

    if len(raw) >= offset + 2 and raw[offset] == 0xFF and (raw[offset + 1] & 0xF0) == 0xF0:
        return "aac"        # ADTS sync word (12 set bits)
    if len(raw) >= offset + 3 * 188 and all(
        raw[offset + i] == 0x47 for i in (0, 188, 376)
    ):
        return "mpegts"     # TS sync byte (0x47) repeating every 188 bytes
    return None


def probe(local_path: str | None, smb_cfg, rel_path: str, ext: str) -> dict | None:
    """
    Read audio metadata from the first READ_BYTES of a file.

    Returns a dict with some of:
        bitrate (str):     e.g. "64k"
        sample_rate (int): e.g. 44100
        channels (int):    1 or 2
        mode (str):        "Stereo" / "Joint Stereo" / "Mono" / "Dual Channel"
        in_format (str):   ffmpeg demuxer name for .aac sources ("aac" | "mpegts")

    Returns None on any error.
    """
    if not HAS_MUTAGEN:
        return None

    from . import smb_client as smb

    ext_lower = ext.lower()
    if ext_lower not in ("mp3", "wav", "aac"):
        return None

    try:
        with smb.open_file(local_path, smb_cfg, rel_path) as fh:
            raw = fh.read(READ_BYTES)
    except Exception as exc:
        log.debug("probe: open failed for %s: %s", rel_path, exc)
        return None

    buf = io.BytesIO(raw)

    try:
        if ext_lower == "mp3":
            audio = mutagen.mp3.MP3(fileobj=buf)
            bitrate_k = round(audio.info.bitrate / 1000)
            return {
                "bitrate":     f"{bitrate_k}k",
                "sample_rate": audio.info.sample_rate,
                "channels":    audio.info.channels,
                "mode":        _MP3_MODES.get(audio.info.mode, "Unknown"),
            }

        if ext_lower == "wav":
            audio = mutagen.wave.WAVE(fileobj=buf)
            return {
                "sample_rate": audio.info.sample_rate,
                "channels":    audio.info.channels,
                "mode":        "Mono" if audio.info.channels == 1 else "Stereo",
            }

        if ext_lower == "aac":
            result: dict = {}
            in_fmt = _detect_aac_container(raw)
            if in_fmt:
                result["in_format"] = in_fmt
                log.info("Detected AAC container for %s: %s", rel_path, in_fmt)
            if in_fmt != "mpegts":
                # Raw ADTS parses as AAC frames; MPEG-TS-wrapped bytes never
                # will, so skip the attempt (and its inevitable debug-log noise).
                try:
                    audio = mutagen.aac.AAC(fileobj=buf)
                    bitrate_k = round(audio.info.bitrate / 1000)
                    result.update({
                        "bitrate":     f"{bitrate_k}k",
                        "sample_rate": audio.info.sample_rate,
                        "channels":    audio.info.channels,
                        "mode":        "Mono" if audio.info.channels == 1 else "Stereo",
                    })
                except Exception as exc:
                    log.debug("probe: mutagen aac failed for %s: %s", rel_path, exc)
            return result or None

    except Exception as exc:
        log.debug("probe: mutagen failed for %s: %s", rel_path, exc)

    return None
