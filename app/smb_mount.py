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
import tempfile
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

# Text sudo itself prints when the sudoers NOPASSWD entry is missing/wrong —
# distinct from an SMB-level auth failure (e.g. "mount error(13): Permission
# denied"), which must NOT be relabeled as a sudoers problem or the real
# cause (wrong SMB credentials) gets hidden from whoever reads the log.
_SUDOERS_HINTS = (_FSTAB_ERR, "a password is required", "no tty present")


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
        # Can't verify — assume mounted rather than fail open to "not
        # mounted". _cleanup_stale_mounts() lazily unmounts anything
        # reported as unmounted, so returning False here on a transient
        # read error could tear down an actively-used mount out from under
        # connected clients. Worst case with "assume mounted" instead:
        # mount_all() skips a re-mount that was actually needed, which
        # surfaces as a clear "channel unreachable" error rather than
        # silently breaking in-flight SMB access.
        return True
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

# Resolved once at import time so sudo commands and sudoers hint use the same path.
_CIFS_BIN = shutil.which("mount.cifs") or "/sbin/mount.cifs"


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

    Password is passed via a temporary credentials file to avoid exposing
    it in the process argument list (visible via ``ps aux``).

    sudoers entry required for non-root users:
        <user> ALL=(root) NOPASSWD: /sbin/mount.cifs
    Falls back through SMB versions 3.0→2.1→2.0→1.0 on error(95).
    """
    cifs_bin = _CIFS_BIN
    unc = f"//{smb.host}/{smb.share}"

    # Determine security mode.
    # auth_protocol="kerberos" → sec=krb5 (requires kinit or keytab).
    # auth_protocol="ntlm" / None → sec=ntlmssp (NTLMv2, works on modern kernels).
    # Legacy sec=ntlm (NTLMv1) is disabled in Ubuntu 22.04+ kernels.
    if smb.auth_protocol == "kerberos":
        sec = "krb5"
    else:
        sec = "ntlmssp"

    # Write credentials to a temp file so the password is not visible in the
    # process argument list (ps aux). mkstemp already creates the file 0600.
    creds_fd, creds_path = tempfile.mkstemp(prefix=".smb_creds_", suffix="")
    try:
        with os.fdopen(creds_fd, "w") as f:
            f.write(f"username={smb.username}\n")
            f.write(f"password={smb.password or ''}\n")
            if smb.domain:
                f.write(f"domain={smb.domain}\n")

        base_options = [
            f"credentials={creds_path}",
            f"uid={os.getuid()}",
            f"gid={os.getgid()}",
            "iocharset=utf8",
            f"sec={sec}",
        ]

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
    finally:
        try:
            os.unlink(creds_path)
        except OSError:
            pass


def _mount_macos(smb: SMBConfig, mount_point: Path) -> str:
    """Mount via mount_smbfs (does not require sudo for regular users).

    NOTE: mount_smbfs has no credentials-file option and its ``-N`` flag
    (read password from stdin) is unreliable across macOS versions, so the
    password is embedded in the URL.  It is still visible in ``ps`` output
    on macOS — this is an OS-level limitation.
    """
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


def _cleanup_stale_mounts() -> None:
    """Lazy-unmount directories in MOUNTS_DIR that exist but are not in /proc/mounts.

    These are left over from a previous run that was killed or crashed without
    a clean unmount.  Cleaning them up before mounting avoids error(16).
    """
    if not MOUNTS_DIR.exists():
        return
    for host_dir in MOUNTS_DIR.iterdir():
        if not host_dir.is_dir():
            continue
        for share_dir in host_dir.iterdir():
            if share_dir.is_dir() and not _is_mounted(share_dir):
                # Directory exists but nothing is mounted — stale stub.
                _umount_lazy(share_dir)


def mount_all(smb_configs: list[SMBConfig]) -> list[MountResult]:
    """Mount all unique SMB shares. Returns results for logging."""
    if not SUPPORTED or not smb_configs:
        return []

    MOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_mounts()

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
            if any(h in err for h in _SUDOERS_HINTS) or err.lower().startswith("sudo:"):
                needs_sudoers = True
                err = "нет прав (sudoers)"
            results.append(MountResult(smb.host, smb.share, mp, False, err))
        else:
            results.append(MountResult(smb.host, smb.share, mp, True, "mounted"))

    # Print sudoers hint once at the end if any mount failed due to permissions
    if needs_sudoers and IS_LINUX:
        user = _current_user()
        print(
            f"[smb_mount] Для авто-монтирования добавьте в /etc/sudoers (visudo):\n"
            f"[smb_mount]   {user} ALL=(root) NOPASSWD: {_CIFS_BIN}",
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
