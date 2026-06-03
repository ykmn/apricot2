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


def probe(local_path: str | None, smb_cfg, rel_path: str, ext: str) -> dict | None:
    """
    Read audio metadata from the first READ_BYTES of a file.

    Returns a dict with some of:
        bitrate (str):     e.g. "64k"
        sample_rate (int): e.g. 44100
        channels (int):    1 or 2
        mode (str):        "Stereo" / "Joint Stereo" / "Mono" / "Dual Channel"

    Returns None on any error.
    """
    if not HAS_MUTAGEN:
        return None

    from . import smb_client as smb

    ext_lower = ext.lower()
    if ext_lower not in ("mp3", "wav"):
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

    except Exception as exc:
        log.debug("probe: mutagen failed for %s: %s", rel_path, exc)

    return None
