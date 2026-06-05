"""Auto-mount SMB shares on Linux and macOS.

Called at startup when sys.platform is 'linux' or 'darwin'.
Each unique host+share pair is mounted under <project_root>/mounts/<host>/<share>/.
Duplicate mounts are detected before attempting to mount.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from .models import SMBConfig

_PROJECT_ROOT = Path(__file__).parent.parent
MOUNTS_DIR = _PROJECT_ROOT / "mounts"

IS_LINUX  = sys.platform == "linux"
IS_MACOS  = sys.platform == "darwin"
SUPPORTED = IS_LINUX or IS_MACOS

# fstab-related error means mount.cifs was called without sudo (or sudoers missing)
_FSTAB_ERR = "found in /etc/fstab"


class MountResult(NamedTuple):
    host: str
    share: str
    mount_point: Path
    ok: bool
    message: str   # "mounted", "already mounted", or short error text


def _mount_point(host: str, share: str) -> Path:
    safe_host  = host.replace(":", "_")
    safe_share = share.replace("\\", "_").replace("/", "_")
    return MOUNTS_DIR / safe_host / safe_share


def _is_mounted_linux(path: Path) -> bool:
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
    target = str(path)
    try:
        r = subprocess.run(["mount"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
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


def _current_user() -> str:
    """Return current username without relying on a controlling terminal."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER", str(os.getuid()))


_SMB_VERSIONS = ("3.0", "2.1", "2.0", "1.0")


def _umount_lazy(mount_point: Path) -> None:
    """Lazy-unmount a stale/busy mountpoint (best-effort)."""
    try:
        umount_bin = shutil.which("umount") or "/bin/umount"
        if os.getuid() == 0:
            cmd = [umount_bin, "-l", str(mount_point)]
        else:
            cmd = ["sudo", umount_bin, "-l", str(mount_point)]
        subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception:
        pass


def _mount_linux(smb: SMBConfig, mount_point: Path) -> str:
    """Mount via mount.cifs directly (called with sudo when not root).

    sudoers entry required for non-root users:
        <user> ALL=(root) NOPASSWD: /sbin/mount.cifs
    Falls back through SMB versions 3.0→2.1→2.0→1.0 on error(95).
    """
    cifs_bin = shutil.which("mount.cifs") or "/sbin/mount.cifs"
    unc = f"//{smb.host}/{smb.share}"
    base_options = [
        f"username={smb.username}",
        f"password={smb.password or ''}",
        f"uid={os.getuid()}",
        f"gid={os.getgid()}",
        "iocharset=utf8",
    ]
    if smb.domain:
        base_options.append(f"domain={smb.domain}")

    last_err = ""
    for vers in _SMB_VERSIONS:
        options = base_options + [f"vers={vers}"]
        if os.getuid() == 0:
            cmd = [cifs_bin, unc, str(mount_point), "-o", ",".join(options)]
        else:
            cmd = ["sudo", cifs_bin, unc, str(mount_point), "-o", ",".join(options)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return ""
            err = (r.stderr or r.stdout).strip() or f"exit code {r.returncode}"
            # error(16) = Device or resource busy — stale mount, try lazy umount once
            if "error(16)" in err:
                _umount_lazy(mount_point)
                r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if r2.returncode == 0:
                    return ""
                err = (r2.stderr or r2.stdout).strip() or err
            last_err = err
            # Only retry with lower version on "Operation not supported" (error 95)
            if "error(95)" not in err:
                break
        except FileNotFoundError:
            return "mount.cifs not found — установите: sudo apt install cifs-utils"
        except subprocess.TimeoutExpired:
            return "timeout (15 s)"
        except Exception as exc:
            return str(exc)
    return last_err


def _mount_macos(smb: SMBConfig, mount_point: Path) -> str:
    """Mount via mount_smbfs (does not require sudo for regular users)."""
    from urllib.parse import quote as _q
    user = smb.username or ""
    pwd  = smb.password or ""
    user_info = f"{_q(user, safe='')}:{_q(pwd, safe='')}@" if user else ""
    url = f"smb://{user_info}{smb.host}/{smb.share}"
    try:
        r = subprocess.run(
            ["mount_smbfs", url, str(mount_point)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return (r.stderr or r.stdout).strip() or f"exit code {r.returncode}"
        return ""
    except FileNotFoundError:
        return "mount_smbfs not found"
    except subprocess.TimeoutExpired:
        return "timeout (15 s)"
    except Exception as exc:
        return str(exc)


def _collect_unique(smb_configs: list[SMBConfig]) -> list[SMBConfig]:
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
    if not SUPPORTED or not smb_configs:
        return []

    MOUNTS_DIR.mkdir(parents=True, exist_ok=True)

    results: list[MountResult] = []
    needs_sudoers = False

    for smb in _collect_unique(smb_configs):
        mp = _mount_point(smb.host, smb.share)

        if _is_mounted(mp):
            results.append(MountResult(smb.host, smb.share, mp, True, "already mounted"))
            continue

        mp.mkdir(parents=True, exist_ok=True)

        err = _mount_linux(smb, mp) if IS_LINUX else _mount_macos(smb, mp)

        if err:
            if _FSTAB_ERR in err or "sudo" in err.lower() or "password" in err.lower():
                needs_sudoers = True
                err = "нет прав (sudoers)"
            results.append(MountResult(smb.host, smb.share, mp, False, err))
        else:
            results.append(MountResult(smb.host, smb.share, mp, True, "mounted"))

    # Print sudoers hint once at the end if any mount failed due to permissions
    if needs_sudoers and IS_LINUX:
        cifs_bin = shutil.which("mount.cifs") or "/sbin/mount.cifs"
        user = _current_user()
        print(
            f"[smb_mount] Для авто-монтирования добавьте в /etc/sudoers (visudo):\n"
            f"[smb_mount]   {user} ALL=(root) NOPASSWD: {cifs_bin}",
            file=sys.stderr,
        )

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
