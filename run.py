#!/usr/bin/env python3
"""Entry point: start the Avocado web server."""
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

import uvicorn

if __name__ == "__main__":
    print(f"Starting Avocado on {protocol}://{host}:{port}")
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
