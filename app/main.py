"""FastAPI application — Авокадо v0.1.002"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
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

VERSION = "0.1.002"
PROJECT_ROOT = Path(__file__).parent.parent
EXPORT_DIR = PROJECT_ROOT / "export"
EXPORT_DIR.mkdir(exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap (mutable globals — replaced on /api/reload)
# ──────────────────────────────────────────────────────────────────────────────

settings: dict = {}
stations_map: dict = {}
playlists_map: dict = {}
all_channels: list = []
channels_map: dict = {}
poll_interval: int = 10

ws_clients: set[WebSocket] = set()


def _load_config() -> None:
    global settings, stations_map, playlists_map, all_channels, channels_map, poll_interval
    settings = load_settings()
    stations_map = load_stations()
    playlists_map = load_playlists()
    if ffmpeg := settings.get("ffmpeg_path"):
        set_ffmpeg_path(ffmpeg)
    all_channels = [ch for st in stations_map.values() for ch in st.channels]
    channels_map = {ch.id: ch for ch in all_channels}
    poll_interval = settings.get("watcher", {}).get("poll_interval", 10)


_load_config()


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

app = FastAPI(title="Авокадо", version=VERSION)

STATIC_DIR = PROJECT_ROOT / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ──────────────────────────────────────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
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
# REST API — meta
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/version")
async def get_version() -> dict:
    return {"version": VERSION, "name": "Авокадо"}


@app.post("/api/reload")
async def reload_config() -> dict:
    """Reload stations, playlists and settings from YAML files without restart."""
    _load_config()
    # Re-setup file index with new channel list
    file_index.setup(all_channels, poll_interval=poll_interval, broadcast=_broadcast)
    asyncio.create_task(_background_startup())
    # Notify all clients
    msg = json.dumps({"type": "config_reloaded"})
    for ws in list(ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            pass
    return {
        "ok": True,
        "stations": len(stations_map),
        "channels": len(channels_map),
        "playlists": len(playlists_map),
    }


# ──────────────────────────────────────────────────────────────────────────────
# REST API — data
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/stations")
async def get_stations() -> list[dict]:
    return [
        {
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
        }
        for st in stations_map.values()
    ]


@app.get("/api/availability/{channel_id}")
async def get_availability(
    channel_id: str,
    start: float = Query(..., description="Unix timestamp"),
    end: float = Query(..., description="Unix timestamp"),
) -> list[dict]:
    idx = file_index.get_index(channel_id)
    if idx is None:
        raise HTTPException(404, f"Channel {channel_id!r} not found")
    return idx.get_intervals(datetime.fromtimestamp(start), datetime.fromtimestamp(end))


@app.get("/api/playlist/{channel_id}")
async def get_playlist(
    channel_id: str,
    start: float = Query(...),
    end: float = Query(...),
) -> list[dict]:
    channel = channels_map.get(channel_id)
    if channel is None:
        raise HTTPException(404)
    start_dt = datetime.fromtimestamp(start)
    end_dt = datetime.fromtimestamp(end)
    entries = []
    for pl_id in channel.playlists:
        pl_cfg = playlists_map.get(pl_id)
        if pl_cfg is None:
            continue
        for e in get_entries(pl_cfg, start_dt, end_dt):
            cl_colors = pl_cfg.class_colors
            cl_names = pl_cfg.class_names
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


# ──────────────────────────────────────────────────────────────────────────────
# REST API — audio
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/audio/stream")
async def audio_stream(
    channel: str = Query(...),
    start: float = Query(...),
    end: float = Query(...),
    format: str = Query("mp3"),
    bitrate: str = Query("192k"),
    sample_rate: int = Query(None),
):
    channel_cfg = channels_map.get(channel)
    if channel_cfg is None:
        raise HTTPException(404)
    start_dt = datetime.fromtimestamp(start)
    end_dt = datetime.fromtimestamp(end)
    media_types = {"mp3": "audio/mpeg", "wav": "audio/wav", "aac": "audio/aac"}
    media_type = media_types.get(format, "audio/mpeg")

    async def gen():
        async for chunk in stream_audio(channel_cfg, start_dt, end_dt, format, bitrate, sample_rate):
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
    out_path = str(EXPORT_DIR / fname)

    await export_audio(channel_cfg, start_dt, end_dt, fmt, bitrate, sample_rate, out_path)
    return {"filename": fname, "download_url": f"/api/audio/download/{fname}"}


@app.get("/api/audio/download/{filename}")
async def audio_download(filename: str) -> FileResponse:
    path = EXPORT_DIR / filename
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
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)
