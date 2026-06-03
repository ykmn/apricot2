"""Unified file access layer: local paths or SMB via smbprotocol."""
from __future__ import annotations

import io
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Generator

from .models import SMBConfig

try:
    import smbclient
    import smbclient.path
    HAS_SMB = True
except ImportError:
    HAS_SMB = False


def _unc(smb: SMBConfig, *parts: str) -> str:
    """Build UNC path: \\host\share\path\parts..."""
    segments = [smb.path] + list(parts)
    joined = "\\".join(s.strip("\\/") for s in segments if s)
    return f"\\\\{smb.host}\\{smb.share}\\{joined}" if joined else f"\\\\{smb.host}\\{smb.share}"


def _register(smb: SMBConfig) -> None:
    if not HAS_SMB:
        raise RuntimeError("smbprotocol is not installed. Install it with: pip install smbprotocol")
    try:
        kwargs: dict = {
            "username": smb.username,
            "password": smb.password or "",
        }
        if smb.domain:
            kwargs["auth_protocol"] = "ntlm"
        smbclient.register_session(smb.host, **kwargs)
    except Exception as exc:
        # May already be registered — ignore duplicate registration errors
        if "already been registered" not in str(exc).lower():
            raise


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def listdir(local_path: str | None, smb: SMBConfig | None, rel: str = "") -> list[str]:
    """Return file/dir names inside *rel* (relative to root of this source)."""
    if local_path:
        p = Path(local_path) / rel if rel else Path(local_path)
        if not p.exists():
            return []
        return [e.name for e in p.iterdir()]
    if smb:
        _register(smb)
        unc = _unc(smb, rel)
        try:
            return smbclient.listdir(unc)
        except Exception:
            return []
    return []


def scandir(local_path: str | None, smb_cfg: SMBConfig | None, rel: str = "") -> list[tuple[str, int]]:
    """Return (name, size_bytes) pairs for files in *rel*.

    Uses a single directory query (no per-file stat round-trips).
    For SMB: smbclient.scandir returns DirEntry objects whose size is
    already embedded in the directory response; stat(follow_symlinks=False)
    reads it from that cached info without an extra network call.
    """
    if local_path:
        p = Path(local_path) / rel if rel else Path(local_path)
        if not p.exists():
            return []
        return [(e.name, e.stat().st_size) for e in p.iterdir() if e.is_file()]
    if smb_cfg:
        _register(smb_cfg)
        unc = _unc(smb_cfg, rel)
        try:
            result = []
            for e in smbclient.scandir(unc):
                # _dir_info.end_of_file is embedded in the SMB FIND response —
                # no extra round-trip per file (unlike stat()).
                try:
                    size = e._dir_info.end_of_file
                except AttributeError:
                    size = e.stat(follow_symlinks=False).st_size
                result.append((e.name, size))
            return result
        except Exception:
            return []
    return []


def exists(local_path: str | None, smb: SMBConfig | None, rel: str) -> bool:
    if local_path:
        return (Path(local_path) / rel).exists()
    if smb:
        _register(smb)
        try:
            smbclient.path.exists(_unc(smb, rel))
            return True
        except Exception:
            return False
    return False


def open_file(local_path: str | None, smb: SMBConfig | None, rel: str) -> io.RawIOBase:
    """Return a readable binary file-like object."""
    if local_path:
        return open(Path(local_path) / rel, "rb")
    if smb:
        _register(smb)
        return smbclient.open_file(_unc(smb, rel), mode="rb")
    raise ValueError("No path source configured")


def read_bytes(local_path: str | None, smb: SMBConfig | None, rel: str) -> bytes:
    with open_file(local_path, smb, rel) as fh:
        return fh.read()


def getsize(local_path: str | None, smb: SMBConfig | None, rel: str) -> int:
    if local_path:
        return (Path(local_path) / rel).stat().st_size
    if smb:
        _register(smb)
        return smbclient.stat(_unc(smb, rel)).st_size
    return 0


def full_path(local_path: str | None, smb: SMBConfig | None, rel: str) -> str:
    """Return an OS-readable path string (local) or UNC path (SMB)."""
    if local_path:
        return str(Path(local_path) / rel)
    if smb:
        return _unc(smb, rel)
    return rel
