#!/usr/bin/env python3
"""SMB version diagnostics for unreachable hosts.

Usage (from project root, with venv active):
    python tools/smb_diag.py
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import subprocess
import shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Force NTLM — same as smb_client.py
if os.name != "nt" and "KRB5CCNAME" not in os.environ:
    os.environ["KRB5CCNAME"] = "MEMORY:"


# ── Collect hosts + credentials from config ───────────────────────────────────

def _collect_smb_configs() -> list:
    """Return list of SMBConfig objects (unique host+share pairs)."""
    from app.config import load_stations, load_playlists
    from app.smb_mount import collect_smb_configs, _collect_unique
    stations  = load_stations()
    playlists = load_playlists()
    return _collect_unique(collect_smb_configs(stations, playlists))


# ── TCP reachability ──────────────────────────────────────────────────────────

def _tcp_open(host: str, port: int = 445, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Raw SMB negotiate (no credentials) ───────────────────────────────────────

_SMB1_NEG = (
    b"\x00\x00\x00\x54"
    b"\xffSMB"
    b"\x72"
    b"\x00\x00\x00\x00"
    b"\x18"
    b"\x01\x28"
    b"\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00"
    b"\xff\xff"
    b"\xfe\xff"
    b"\x00\x00"
    b"\x40\x00"
    b"\x00"
    b"\x26\x00"
    b"\x02NT LM 0.12\x00"
    b"\x02SMB 2.002\x00"
    b"\x02SMB 2.???\x00"
)


def _raw_negotiate(host: str, timeout: float = 4.0) -> str:
    try:
        s = socket.create_connection((host, 445), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(_SMB1_NEG)
        resp = s.recv(256)
        s.close()
        if len(resp) < 9:
            return "нет ответа (слишком короткий)"
        if resp[4:8] == b"\xfeSMB":
            return "SMBv2/v3"
        if resp[4:8] == b"\xffSMB" and resp[8] == 0x72:
            st = struct.unpack_from("<I", resp, 9)[0]
            return f"SMBv1 (NT Status 0x{st:08x})"
        return f"неизвестный ответ: {resp[:8].hex()}"
    except socket.timeout:
        return "нет ответа (timeout)"
    except OSError as e:
        return f"ошибка сокета: {e}"


# ── smbprotocol probe with real credentials ───────────────────────────────────

def _smb2_probe(host: str, username: str, password: str, domain: str | None) -> str:
    try:
        import smbclient
        kwargs: dict = {"username": username, "password": password or "",
                        "auth_protocol": "ntlm"}
        if domain:
            kwargs["auth_protocol"] = "ntlm"
        smbclient.register_session(host, **kwargs)
        return "OK — сессия NTLM установлена"
    except Exception as exc:
        msg = str(exc)
        if "already been registered" in msg.lower():
            return "уже зарегистрирован (из предыдущей попытки)"
        if "STATUS_LOGON_FAILURE" in msg or "Logon Failure" in msg:
            return "ОШИБКА АУТЕНТИФИКАЦИИ — неверный логин/пароль"
        if "STATUS_ACCOUNT_DISABLED" in msg or "0xc0000072" in msg:
            return "учётная запись отключена на сервере"
        if "STATUS_ACCESS_DENIED" in msg:
            return "доступ запрещён"
        if "spnego" in msg.lower() or "kerberos" in msg.lower():
            return f"SPNEGO/Kerberos ошибка (NTLM не принят?): {msg[:120]}"
        if "Connection refused" in msg or "timed out" in msg.lower():
            return f"нет соединения: {msg[:80]}"
        return f"ошибка: {msg[:120]}"


# ── smbprotocol list shares ───────────────────────────────────────────────────

def _list_shares(host: str, share: str, path: str,
                 username: str, password: str) -> str:
    try:
        import smbclient
        unc = f"\\\\{host}\\{share}"
        if path:
            unc += "\\" + path.replace("/", "\\").strip("\\")
        entries = smbclient.listdir(unc)
        sample = entries[:5]
        return f"{len(entries)} элементов, первые: {sample}"
    except Exception as exc:
        msg = str(exc)
        if "STATUS_OBJECT_NAME_NOT_FOUND" in msg or "STATUS_OBJECT_PATH_NOT_FOUND" in msg:
            return f"путь {path!r} не найден в шаре"
        if "STATUS_ACCESS_DENIED" in msg:
            return "доступ к шаре запрещён"
        return f"listdir ошибка: {msg[:120]}"


# ── mount.cifs dry-run probe ─────────────────────────────────────────────────

def _cifs_probe(smb) -> str:
    """Attempt an actual mount.cifs to a temp directory, then unmount immediately.

    This catches issues invisible to smbprotocol (kernel CIFS driver quirks,
    sec= negotiation, sudoers, missing cifs-utils, etc.).
    """
    import tempfile, os
    if not sys.platform.startswith("linux"):
        return "н/д (только Linux)"
    cifs_bin = shutil.which("mount.cifs") or "/sbin/mount.cifs"
    if not Path(cifs_bin).exists():
        return "mount.cifs не найден — apt install cifs-utils"

    sec = "krb5" if smb.auth_protocol == "kerberos" else "ntlmssp"
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = [
            f"username={smb.username}",
            f"password={smb.password or ''}",
            f"uid={os.getuid()}",
            f"gid={os.getgid()}",
            "iocharset=utf8",
            f"sec={sec}",
        ]
        if smb.domain:
            opts.append(f"domain={smb.domain}")
        # Try SMB versions from newest to oldest
        for vers in ("3.0", "2.1", "2.0"):
            cmd_mount = ["sudo", cifs_bin,
                         f"//{smb.host}/{smb.share}", tmpdir,
                         "-o", ",".join(opts + [f"vers={vers}"])]
            try:
                r = subprocess.run(cmd_mount, capture_output=True, text=True, timeout=15)
                if r.returncode == 0:
                    # Mounted — unmount immediately
                    subprocess.run(["sudo", "umount", "-l", tmpdir],
                                   capture_output=True, timeout=10)
                    return f"OK (sec={sec}, vers={vers})"
                err = (r.stderr or r.stdout).strip()
                if "error(95)" not in err:
                    # Not a version mismatch — report and stop
                    return f"ОШИБКА (sec={sec}, vers={vers}): {err[:120]}"
            except subprocess.TimeoutExpired:
                return f"timeout (sec={sec}, vers={vers})"
            except Exception as exc:
                return str(exc)
        return f"все версии SMB отклонены (sec={sec})"


# ── Kerberos ticket check ─────────────────────────────────────────────────────

def _krb5_check() -> str:
    """Check if there's a valid Kerberos ticket cache."""
    klist = shutil.which("klist")
    if not klist:
        return "klist не найден — Kerberos не установлен"
    try:
        r = subprocess.run([klist, "-s"], capture_output=True, timeout=5)
        if r.returncode == 0:
            r2 = subprocess.run([klist], capture_output=True, text=True, timeout=5)
            # Show first principal line
            for line in r2.stdout.splitlines():
                if "principal" in line.lower() or "@" in line:
                    return f"тикет есть: {line.strip()}"
            return "тикет есть (детали недоступны)"
        return "тикетов нет (нужен kinit или keytab)"
    except Exception as exc:
        return f"ошибка klist: {exc}"


# ── nmap ──────────────────────────────────────────────────────────────────────

def _nmap_smb_protocols(host: str) -> str | None:
    if not shutil.which("nmap"):
        return None
    try:
        r = subprocess.run(
            ["nmap", "-p", "445", "--script", "smb-protocols",
             "--script-args", "unsafe=1", "-T4", host],
            capture_output=True, text=True, timeout=20,
        )
        lines = [l.strip() for l in r.stdout.splitlines()
                 if any(k in l for k in ("SMB", "smb", "dialect", "NT LM", "2.", "3."))]
        return "; ".join(lines) if lines else None
    except Exception:
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    configs = _collect_smb_configs()
    print(f"Диагностика SMB — {len(configs)} уникальных шар\n{'─'*64}")

    has_kerberos = any(c.auth_protocol == "kerberos" for c in configs)
    if has_kerberos and sys.platform.startswith("linux"):
        print(f"  Kerberos     : {_krb5_check()}\n")

    for smb in configs:
        host = smb.host
        sec  = "krb5" if smb.auth_protocol == "kerberos" else "ntlmssp"
        print(f"\n{'─'*64}")
        print(f"  //{host}/{smb.share}  (path: {smb.path!r})")
        print(f"  user: {smb.username!r}  domain: {smb.domain!r}  auth_protocol: {smb.auth_protocol!r}  → sec={sec}")

        if not _tcp_open(host):
            print("  TCP 445      : ЗАКРЫТ / недоступен")
            continue

        print(f"  TCP 445      : открыт")
        print(f"  Raw negotiate: {_raw_negotiate(host)}")

        nmap = _nmap_smb_protocols(host)
        if nmap:
            print(f"  nmap         : {nmap}")

        if smb.auth_protocol == "kerberos":
            print(f"  smbprotocol  : пропущено (kerberos — нужен тикет OS)")
        else:
            probe = _smb2_probe(host, smb.username, smb.password or "", smb.domain)
            print(f"  smbprotocol  : {probe}")
            if "OK" in probe:
                ls = _list_shares(host, smb.share, smb.path, smb.username, smb.password or "")
                print(f"  Listdir      : {ls}")

        cifs = _cifs_probe(smb)
        print(f"  mount.cifs   : {cifs}")

    print(f"\n{'─'*64}\nГотово.")


if __name__ == "__main__":
    main()
