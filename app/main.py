"""FastAPI application entry point."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .audio import export_audio, set_ffmpeg_path, stream_audio
from .config import load_playlists, load_settings, load_stations
from .file_index import file_index
from .playlist import get_entries

# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ──────────────────────────────────────────────────────────────────────────────

settings = load_settings()
stations_map = load_stations()
playlists_map = load_playlists()

if ffmpeg := settings.get("ffmpeg_path"):
    set_ffmpeg_path(ffmpeg)

all_channels = [ch for st in stations_map.values() for ch in st.channels]
channels_map = {ch.id: ch for ch in all_channels}

poll_interval = settings.get("watcher", {}).get("poll_interval", 10)

# WebSocket connection manager
ws_clients: set[WebSocket] = set()


def _broadcast(channel_id: str, added: list[dict], removed: list[dict]) -> None:
    msg = json.dumps({
        "type": "availability_update",
        "channel_id": channel_id,
        "added": added,
        "removed": removed,
    })
    dead = set()
    for ws in ws_clients:
        try:
            asyncio.create_task(ws.send_text(msg))
        except Exception:
            dead.add(ws)
    ws_clients -= dead


file_index.setup(all_channels, poll_interval=poll_interval, broadcast=_broadcast)

app = FastAPI(title="Radio Monitor")

STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ──────────────────────────────────────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    # Run initial scan in background so server starts immediately
    asyncio.create_task(_background_startup())


async def _background_startup() -> None:
    try:
        await file_index.initial_scan()
    except Exception as exc:
        print(f"[startup] initial scan error: {exc}")
    file_index.start_polling()


# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


# ──────────────────────────────────────────────────────────────────────────────
# REST API
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/stations")
async def get_stations() -> list[dict]:
    result = []
    for st in stations_map.values():
        result.append({
            "id": st.id,
            "name": st.name,
            "channels": [
                {
                    "id": ch.id,
                    "name": ch.name,
                    "file_extension": ch.file_extension,
                    "playlists": ch.playlists,
                }
                for ch in st.channels
            ],
        })
    return result


@app.get("/api/availability/{channel_id}")
async def get_availability(
    channel_id: str,
    start: float = Query(..., description="Unix timestamp"),
    end: float = Query(..., description="Unix timestamp"),
) -> list[dict]:
    idx = file_index.get_index(channel_id)
    if idx is None:
        raise HTTPException(404, f"Channel {channel_id!r} not found")
    start_dt = datetime.fromtimestamp(start)
    end_dt = datetime.fromtimestamp(end)
    return idx.get_intervals(start_dt, end_dt)


@app.get("/api/playlist/{channel_id}")
async def get_playlist(
    channel_id: str,
    start: float = Query(...),
    end: float = Query(...),
) -> list[dict]:
    channel = channels_map.get(channel_id)
    if channel is None:
        raise HTTPException(404, f"Channel {channel_id!r} not found")
    start_dt = datetime.fromtimestamp(start)
    end_dt = datetime.fromtimestamp(end)

    entries = []
    for pl_id in channel.playlists:
        pl_cfg = playlists_map.get(pl_id)
        if pl_cfg is None:
            continue
        pl_entries = get_entries(pl_cfg, start_dt, end_dt)
        cl_colors = pl_cfg.class_colors
        cl_names = pl_cfg.class_names
        for e in pl_entries:
            entries.append({
                "timestamp": e.timestamp.timestamp(),
                "title": e.title,
                "cls": e.cls,
                "duration": e.duration,
                "color": cl_colors.get(e.cls, cl_colors.get("default", "#e0e0e0")),
                "cls_name": cl_names.get(e.cls, e.cls),
            })

    entries.sort(key=lambda x: x["timestamp"])
    return entries


@app.get("/api/playlist_config/{channel_id}")
async def get_playlist_config(channel_id: str) -> dict:
    """Return class colors/names for UI legend."""
    channel = channels_map.get(channel_id)
    if channel is None:
        raise HTTPException(404)
    colors: dict = {}
    names: dict = {}
    for pl_id in channel.playlists:
        pl_cfg = playlists_map.get(pl_id)
        if pl_cfg:
            colors.update(pl_cfg.class_colors)
            names.update(pl_cfg.class_names)
    return {"class_colors": colors, "class_names": names}


@app.get("/api/audio/stream")
async def audio_stream(
    channel: str = Query(...),
    start: float = Query(...),
    end: float = Query(...),
    format: str = Query("mp3"),
    bitrate: str = Query("192k"),
):
    channel_cfg = channels_map.get(channel)
    if channel_cfg is None:
        raise HTTPException(404)
    start_dt = datetime.fromtimestamp(start)
    end_dt = datetime.fromtimestamp(end)

    media_types = {"mp3": "audio/mpeg", "wav": "audio/wav", "aac": "audio/aac"}
    media_type = media_types.get(format, "audio/mpeg")

    async def gen():
        async for chunk in stream_audio(channel_cfg, start_dt, end_dt, format, bitrate):
            yield chunk

    return StreamingResponse(gen(), media_type=media_type)


@app.post("/api/audio/export")
async def audio_export(body: dict) -> dict:
    channel_id = body.get("channel_id", "")
    start = float(body.get("start", 0))
    end = float(body.get("end", 0))
    fmt = body.get("format", "mp3")
    bitrate = body.get("bitrate") or "192k"
    sample_rate = body.get("sample_rate")
    if sample_rate is not None:
        sample_rate = int(sample_rate)

    channel_cfg = channels_map.get(channel_id)
    if channel_cfg is None:
        raise HTTPException(404)

    start_dt = datetime.fromtimestamp(start)
    end_dt = datetime.fromtimestamp(end)
    ts = start_dt.strftime("%Y%m%d_%H%M%S")
    fname = f"{channel_id}_{ts}.{fmt}"
    out_path = str(Path(tempfile.gettempdir()) / fname)

    await export_audio(channel_cfg, start_dt, end_dt, fmt, bitrate, sample_rate, out_path)

    return {"filename": fname, "download_url": f"/api/audio/download/{fname}"}


@app.get("/api/audio/download/{filename}")
async def audio_download(filename: str) -> FileResponse:
    path = Path(tempfile.gettempdir()) / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path), filename=filename, media_type="application/octet-stream")


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket
# ──────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            # Keep-alive ping
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)
