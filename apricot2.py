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
ssl_enabled  = bool(ssl_cfg.get("enabled", False))
ssl_certfile = None
ssl_keyfile  = None

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
if sys.platform in ("linux", "darwin") and port < 1024 and os.getuid() != 0:
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

import uvicorn

if __name__ == "__main__":
    print(f"Starting Абрикос 2 on {protocol}://{host}:{port}")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=False,
        # Uvicorn's own access log is redundant — we have our own middleware.
        # Set to WARNING to keep only startup/error messages from uvicorn itself.
        log_level="warning",
        access_log=False,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
