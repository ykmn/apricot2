"""Auto-mount SMB shares on Linux and macOS.

Called at startup when sys.platform is 'linux' or 'darwin'.
Each unique host+share pair is mounted under <project_root>/mounts/<host>/<share>/.
Duplicate mounts are detected before attempting to mount.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
import os
from pathlib import Path
from typing import NamedTuple

from .models import SMBConfig

# Project root is two levels up from this file (app/smb_mount.py → project/)
_PROJECT_ROOT = Path(__file__).parent.parent
MOUNTS_DIR = _PROJECT_ROOT / "mounts"

IS_LINUX  = sys.platform == "linux"
IS_MACOS  = sys.platform == "darwin"
SUPPORTED = IS_LINUX or IS_MACOS


class MountResult(NamedTuple):
    host: str
    share: str
    mount_point: Path
    ok: bool
    message: str   # "mounted", "already mounted", or error text


def _mount_point(host: str, share: str) -> Path:
    safe_host  = host.replace(":", "_")
    safe_share = share.replace("\\", "_").replace("/", "_")
    return MOUNTS_DIR / safe_host / safe_share


def _is_mounted_linux(path: Path) -> bool:
    """Check /proc/mounts for an existing mount at *path*."""
    target = str(path)
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == target:
                    return True
    except OSError:
        pass
    return False


def _is_mounted_macos(path: Path) -> bool:
    """Run `mount` and check if *path* appears as a mount point."""
    target = str(path)
    try:
        result = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            # Format: "//user@host/share on /path/to/point (smbfs, ...)"
            if f" on {target} " in line or line.endswith(f" on {target}"):
                return True
    except Exception:
        pass
    return False


def _is_mounted(path: Path) -> bool:
    if IS_LINUX:
        return _is_mounted_linux(path)
    if IS_MACOS:
        return _is_mounted_macos(path)
    return False


def _mount_linux(smb: SMBConfig, mount_point: Path) -> str:
    """Mount via mount.cifs. Returns error string or empty string on success."""
    options = [
        f"username={smb.username}",
        f"password={smb.password or ''}",
        f"uid={os.getuid()}",
        f"gid={os.getgid()}",
        "vers=3.0",
        "iocharset=utf8",
    ]
    if smb.domain:
        options.append(f"domain={smb.domain}")

    unc = f"//{smb.host}/{smb.share}"
    cmd = ["mount", "-t", "cifs", unc, str(mount_point), "-o", ",".join(options)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return (r.stderr or r.stdout).strip() or f"exit code {r.returncode}"
        return ""
    except FileNotFoundError:
        return "mount.cifs not found; install cifs-utils: sudo apt install cifs-utils"
    except subprocess.TimeoutExpired:
        return "mount timed out (15 s)"
    except Exception as exc:
        return str(exc)


def _mount_macos(smb: SMBConfig, mount_point: Path) -> str:
    """Mount via mount_smbfs. Returns error string or empty string on success."""
    password = smb.password or ""
    user = smb.username or ""
    # URL-encode special chars in password to avoid shell issues
    from urllib.parse import quote as _q
    user_info = f"{_q(user, safe='')}:{_q(password, safe='')}@" if user else ""
    url = f"smb://{user_info}{smb.host}/{smb.share}"
    cmd = ["mount_smbfs", url, str(mount_point)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return (r.stderr or r.stdout).strip() or f"exit code {r.returncode}"
        return ""
    except FileNotFoundError:
        return "mount_smbfs not found"
    except subprocess.TimeoutExpired:
        return "mount timed out (15 s)"
    except Exception as exc:
        return str(exc)


def _collect_unique(smb_configs: list[SMBConfig]) -> list[SMBConfig]:
    """Deduplicate by (host, share) — keep first occurrence."""
    seen: set[tuple[str, str]] = set()
    result: list[SMBConfig] = []
    for smb in smb_configs:
        key = (smb.host.lower(), smb.share.lower())
        if key not in seen:
            seen.add(key)
            result.append(smb)
    return result


def mount_all(smb_configs: list[SMBConfig]) -> list[MountResult]:
    """Mount all unique SMB shares. Returns results for logging."""
    if not SUPPORTED:
        return []
    if not smb_configs:
        return []

    MOUNTS_DIR.mkdir(parents=True, exist_ok=True)

    results: list[MountResult] = []
    for smb in _collect_unique(smb_configs):
        mp = _mount_point(smb.host, smb.share)

        if _is_mounted(mp):
            results.append(MountResult(smb.host, smb.share, mp, True, "already mounted"))
            continue

        mp.mkdir(parents=True, exist_ok=True)

        if IS_LINUX:
            err = _mount_linux(smb, mp)
        else:
            err = _mount_macos(smb, mp)

        if err:
            results.append(MountResult(smb.host, smb.share, mp, False, err))
        else:
            results.append(MountResult(smb.host, smb.share, mp, True, "mounted"))

    return results


def collect_smb_configs(stations_map: dict, playlists_map: dict) -> list[SMBConfig]:
    """Gather all SMBConfig objects from stations and playlists."""
    configs: list[SMBConfig] = []
    for station in stations_map.values():
        for ch in station.channels:
            if ch.smb:
                configs.append(ch.smb)
    for pl in playlists_map.values():
        for src in pl.sources:
            if src.smb:
                configs.append(src.smb)
    return configs
