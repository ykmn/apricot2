#!/usr/bin/env python3
"""Entry point: start the Apricot 2 web server."""
import os
import sys
from pathlib import Path

# Make sure the project root is on the path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# ── Pre-flight: validate all YAML config files before launching uvicorn ───────
def _validate_configs() -> None:
    """Parse every YAML file in config/ and report errors before server start."""
    import yaml
    from app.config import _open_yaml, ConfigError

    config_dir = ROOT / "config"
    if not config_dir.exists():
        print("[config] WARNING: config/ directory not found", file=sys.stderr)
        return

    patterns = [
        "settings.yaml",
        "secret.yaml",
        "ldap.yaml",
        "users.yaml",
        "stations/*.yaml",
        "playlogs/*.yaml",
    ]

    errors: list[str] = []
    checked = 0
    for pattern in patterns:
        for path in sorted(config_dir.glob(pattern)):
            checked += 1
            try:
                _open_yaml(path)
            except ConfigError as exc:
                errors.append(f"  ✗ config/{path.relative_to(config_dir)}: {exc}")
            except Exception as exc:
                errors.append(f"  ✗ config/{path.relative_to(config_dir)}: {exc}")

    if errors:
        print(f"[config] ОШИБКА — {len(errors)} из {checked} файлов не прошли проверку:",
              file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        print("\nИсправьте ошибки и перезапустите сервер.", file=sys.stderr)
        sys.exit(1)

    print(f"[config] OK — проверено {checked} файл(ов)")


_validate_configs()

# ── SMB auto-mount (Linux / macOS only) ───────────────────────────────────────
def _mount_smb_sources() -> None:
    import sys as _sys
    if _sys.platform not in ("linux", "darwin"):
        return
    try:
        from app.config import load_stations, load_playlists
        from app.smb_mount import collect_smb_configs, mount_all, MOUNTS_DIR
    except ImportError as exc:
        print(f"[smb_mount] WARNING: не удалось импортировать модуль: {exc}", file=sys.stderr)
        return

    stations  = load_stations()
    playlists = load_playlists()
    configs   = collect_smb_configs(stations, playlists)

    if not configs:
        return

    platform_name = "Linux" if _sys.platform == "linux" else "macOS"
    print(f"[smb_mount] Подключение SMB-источников ({platform_name})...")

    results = mount_all(configs)
    ok = sum(1 for r in results if r.ok)
    failed = [r for r in results if not r.ok]

    for r in results:
        status = "✓" if r.ok else "✗"
        print(f"[smb_mount]   {status} //{r.host}/{r.share}  →  {MOUNTS_DIR / r.host / r.share}  [{r.message}]")

    if failed:
        print(f"[smb_mount] Предупреждение: {len(failed)} из {len(results)} источников не удалось подключить.",
              file=sys.stderr)
        print("[smb_mount] Приложение продолжит работу через smbprotocol.", file=sys.stderr)
    else:
        print(f"[smb_mount] OK — подключено/подтверждено {ok} источников")


_mount_smb_sources()

from app.config import load_settings

settings = load_settings()
server   = settings.get("server", {})
host     = server.get("host", "0.0.0.0")
port     = int(server.get("port", 8765))

# ── SSL ───────────────────────────────────────────────────────────────────────
ssl_cfg      = server.get("ssl") or {}
ssl_enabled      = bool(ssl_cfg.get("enabled", False))
ssl_certfile     = None
ssl_keyfile      = None
http_redirect_port = int(ssl_cfg.get("http_redirect_port", 80)) if ssl_enabled else None

if ssl_enabled:
    cert = ssl_cfg.get("cert", "ssl/cert.crt")
    key  = ssl_cfg.get("key",  "ssl/server.key")
    ssl_certfile = str(ROOT / cert)
    ssl_keyfile  = str(ROOT / key)
    # Validate that the files exist before starting
    for label, path in (("cert", ssl_certfile), ("key", ssl_keyfile)):
        if not Path(path).exists():
            print(f"[ssl] ERROR: {label} file not found: {path}", file=sys.stderr)
            sys.exit(1)

protocol = "https" if ssl_enabled else "http"

# ── Privileged port check (Linux / macOS) ─────────────────────────────────────
# Проверяем только если порт < 1024 и нет root — authbind меняет uid на != 0,
# поэтому дополнительно проверяем, что процесс способен биндить порт.
if sys.platform in ("linux", "darwin") and port < 1024 and os.getuid() != 0:
    import socket as _socket
    _can_bind = False
    try:
        _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        _s.bind(("", port))
        _s.close()
        _can_bind = True
    except PermissionError:
        pass
    if not _can_bind:
        print(
            f"[server] ОШИБКА: порт {port} требует прав root на Linux/macOS.\n"
            f"  Варианты решения:\n"
            f"  1. Используйте порт >= 1024 (например, 8443) в config/settings.yaml\n"
            f"  2. Разрешите Python слушать привилегированные порты без root:\n"
            f"       sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python3))\n"
            f"  3. Запустите через authbind:\n"
            f"       sudo apt install authbind\n"
            f"       sudo touch /etc/authbind/byport/{port}\n"
            f"       sudo chmod 500 /etc/authbind/byport/{port}\n"
            f"       sudo chown $USER /etc/authbind/byport/{port}\n"
            f"       authbind --deep python3 apricot2.py",
            file=sys.stderr,
        )
        sys.exit(1)

import asyncio
import signal
import socket
import uvicorn


def _kill_port(p: int) -> None:
    """Kill any process listening on *p* (Linux/macOS only)."""
    import subprocess, re
    try:
        r = subprocess.run(
            ["ss", "-tlnp", f"sport = :{p}"],
            capture_output=True, text=True, timeout=5,
        )
        for pid in re.findall(r"pid=(\d+)", r.stdout):
            try:
                os.kill(int(pid), signal.SIGTERM)
                print(f"[server] Завершён предыдущий процесс pid={pid} на порту {p}")
            except ProcessLookupError:
                pass
    except Exception:
        pass


def _port_busy(h: str, p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.connect_ex((h if h != "0.0.0.0" else "127.0.0.1", p)) == 0


if sys.platform in ("linux", "darwin") and _port_busy(host, port):
    print(f"[server] Порт {port} занят — завершаю предыдущий процесс...")
    _kill_port(port)
    import time; time.sleep(1)
    if _port_busy(host, port):
        print(f"[server] ОШИБКА: порт {port} всё ещё занят после SIGTERM.", file=sys.stderr)
        sys.exit(1)

def _make_redirect_app(target_host: str, target_port: int) -> object:
    """Minimal ASGI app: redirect every HTTP request to HTTPS."""
    port_suffix = "" if target_port == 443 else f":{target_port}"

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        host_header = target_host
        for name, value in scope.get("headers", []):
            if name == b"host":
                # Strip any existing port from the Host header
                host_header = value.decode().split(":")[0]
                break
        path = scope.get("path", "/")
        qs   = scope.get("query_string", b"").decode()
        location = f"https://{host_header}{port_suffix}{path}" + (f"?{qs}" if qs else "")
        await send({
            "type":    "http.response.start",
            "status":  301,
            "headers": [(b"location", location.encode()), (b"content-length", b"0")],
        })
        await send({"type": "http.response.body", "body": b""})

    return app


async def _run_servers() -> None:
    main_cfg = uvicorn.Config(
        "app.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="warning",
        access_log=False,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
    servers = [uvicorn.Server(main_cfg)]

    if http_redirect_port is not None:
        # Check / clear port before starting redirect server
        if sys.platform in ("linux", "darwin") and _port_busy(host, http_redirect_port):
            print(f"[server] Порт {http_redirect_port} занят — завершаю предыдущий процесс...")
            _kill_port(http_redirect_port)
            import time; time.sleep(1)

        redir_cfg = uvicorn.Config(
            _make_redirect_app(host, port),
            host=host,
            port=http_redirect_port,
            reload=False,
            log_level="warning",
            access_log=False,
        )
        servers.append(uvicorn.Server(redir_cfg))
        print(f"[server] HTTP→HTTPS редирект: http://{host}:{http_redirect_port} → {protocol}://{host}:{port}")

    await asyncio.gather(*[s.serve() for s in servers])


if __name__ == "__main__":
    print(f"Starting Абрикос 2 on {protocol}://{host}:{port}")
    asyncio.run(_run_servers())
