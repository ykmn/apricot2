#!/usr/bin/env python3
"""Entry point: start the Radio Monitor web server."""
import sys
from pathlib import Path

# Make sure the project root is on the path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings

settings = load_settings()
server = settings.get("server", {})
host = server.get("host", "0.0.0.0")
port = int(server.get("port", 8765))

import uvicorn

if __name__ == "__main__":
    print(f"Starting Radio Monitor on http://{host}:{port}")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
