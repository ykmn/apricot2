#!/usr/bin/env python3
"""Entry point: start the Avocado web server."""
import sys
from pathlib import Path

# Make sure the project root is on the path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

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
