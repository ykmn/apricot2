#!/usr/bin/env python3
"""SMB version diagnostics for unreachable hosts.

Usage (from project root, with venv active):
    python tools/smb_diag.py

Reads hosts from config/stations/*.yaml and config/playlogs/*.yaml,
attempts SMB negotiation and reports supported protocol versions.
"""
from __future__ import annotations

import socket
import struct
import sys
import subprocess
import shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ── Collect hosts from config ─────────────────────────────────────────────────

def _collect_hosts() -> list[str]:
    import yaml
    hosts: set[str] = set()
    for pattern in ("config/stations/*.yaml", "config/playlogs/*.yaml"):
        for path in sorted(ROOT.glob(pattern)):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            for ch in data.get("channels", []):
                if smb := ch.get("smb"):
                    hosts.add(smb["host"])
            for src in data.get("sources", []):
                if smb := src.get("smb"):
                    hosts.add(smb["host"])
    return sorted(hosts, key=str.lower)


# ── TCP reachability ──────────────────────────────────────────────────────────

def _tcp_open(host: str, port: int = 445, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Raw SMB1 negotiate (no credentials needed) ───────────────────────────────

_SMB1_NEG = (
    b"\x00\x00\x00\x54"          # NetBIOS session length
    b"\xffSMB"                    # SMB magic
    b"\x72"                       # Command: Negotiate Protocol
    b"\x00\x00\x00\x00"          # NT Status
    b"\x18"                       # Flags
    b"\x01\x28"                   # Flags2
    b"\x00\x00"                   # PID High
    b"\x00\x00\x00\x00\x00\x00\x00\x00"  # Signature
    b"\x00\x00"                   # Reserved
    b"\xff\xff"                   # TID
    b"\xfe\xff"                   # PID
    b"\x00\x00"                   # UID
    b"\x40\x00"                   # MID
    b"\x00"                       # WordCount
    b"\x26\x00"                   # ByteCount = 38
    b"\x02NT LM 0.12\x00"         # dialect
    b"\x02SMB 2.002\x00"          # dialect
    b"\x02SMB 2.???\x00"          # dialect
)


def _smb1_negotiate(host: str, timeout: float = 4.0) -> str | None:
    """Send SMB1 negotiate and return server response summary."""
    try:
        s = socket.create_connection((host, 445), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(_SMB1_NEG)
        resp = s.recv(256)
        s.close()
        if len(resp) < 9:
            return None
        if resp[4:8] == b"\xfeSMB":
            # SMB2 response to our negotiate
            return "SMBv2/v3 (ответил на SMB2 negotiate)"
        if resp[4:8] == b"\xffSMB" and resp[8] == 0x72:
            # SMB1 negotiate response
            nt_status = struct.unpack_from("<I", resp, 9)[0]
            if nt_status == 0:
                return "SMBv1 (сервер принял SMB1 negotiate)"
            return f"SMBv1 negotiate — NT Status 0x{nt_status:08x}"
        return f"Неизвестный ответ: {resp[:8].hex()}"
    except socket.timeout:
        return None
    except OSError:
        return None


# ── smbprotocol probe (SMBv2/3) ───────────────────────────────────────────────

def _smb2_probe(host: str) -> str:
    try:
        import smbclient
        import smbclient._os
        smbclient.register_session(host, username="guest", password="",
                                   auth_protocol="ntlm")
        return "SMBv2/v3 — сессия зарегистрирована (guest)"
    except Exception as exc:
        msg = str(exc)
        if "STATUS_LOGON_FAILURE" in msg or "Logon Failure" in msg:
            return "SMBv2/v3 доступен — ошибка аутентификации (неверные credentials)"
        if "STATUS_ACCESS_DENIED" in msg:
            return "SMBv2/v3 доступен — доступ запрещён для guest"
        if "Connection refused" in msg or "timed out" in msg.lower():
            return f"SMBv2/v3 — нет соединения: {msg[:80]}"
        return f"SMBv2/v3 — ошибка: {msg[:120]}"


# ── nmap probe ────────────────────────────────────────────────────────────────

def _nmap_probe(host: str) -> str | None:
    if not shutil.which("nmap"):
        return None
    try:
        r = subprocess.run(
            ["nmap", "-p", "445", "--script", "smb-protocols",
             "--script-args", "unsafe=1", "-T4", host],
            capture_output=True, text=True, timeout=20,
        )
        for line in r.stdout.splitlines():
            line = line.strip()
            if "SMB" in line or "smb" in line or "dialect" in line.lower():
                return line
    except Exception:
        pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    hosts = _collect_hosts()
    print(f"Диагностика SMB — {len(hosts)} хостов\n{'─'*60}")

    for host in hosts:
        print(f"\n{'─'*60}")
        print(f"Хост: {host}")

        if not _tcp_open(host):
            print("  TCP 445: ЗАКРЫТ / недоступен")
            continue

        print("  TCP 445: открыт")

        raw = _smb1_negotiate(host)
        if raw:
            print(f"  Raw negotiate: {raw}")
        else:
            print("  Raw negotiate: нет ответа")

        smb2 = _smb2_probe(host)
        print(f"  smbprotocol:   {smb2}")

        nmap = _nmap_probe(host)
        if nmap:
            print(f"  nmap:          {nmap}")

    print(f"\n{'─'*60}")
    print("Готово.")


if __name__ == "__main__":
    main()
