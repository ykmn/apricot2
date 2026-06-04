"""FastAPI application — Авокадо v1.0.000"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiofiles
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth as _auth

from .app_logger import get_logger
from .audio import export_audio, set_ffmpeg_path, stream_audio
from .config import load_playlists, load_settings, load_stations
from .file_index import file_index
from .playlist import get_entries

VERSION = "1.0.000"
PROJECT_ROOT = Path(__file__).parent.parent
EXPORT_DIR = PROJECT_ROOT / "export"
EXPORT_DIR.mkdir(exist_ok=True)

log = get_logger("avocado")


def _build_date() -> str:
    """Return mtime of the most recently modified tracked file in the project."""
    try:
        r = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True,
            cwd=str(PROJECT_ROOT), timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            latest = max(
                (PROJECT_ROOT / f).stat().st_mtime
                for f in r.stdout.splitlines()
                if (PROJECT_ROOT / f).exists()
            )
            return datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d %H:%M")


BUILD_DATE = _build_date()

# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap (mutable globals — replaced on /api/reload)
# ──────────────────────────────────────────────────────────────────────────────

settings: dict = {}
stations_map: dict = {}
playlists_map: dict = {}
all_channels: list = []
channels_map: dict = {}
poll_interval: int = 10
_playlog_status: list = []   # last check result, returned by /api/playlog_status

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
    log.info(
        "Config loaded — %d station(s), %d channel(s), %d playlist(s)",
        len(stations_map), len(channels_map), len(playlists_map),
    )


_load_config()


def _broadcast(channel_id: str, added: list[dict], removed: list[dict]) -> None:
    msg = json.dumps({
        "type":       "availability_update",
        "channel_id": channel_id,
        "added":      added,
        "removed":    removed,
    })
    _broadcast_raw(msg)


def _broadcast_raw(msg: str) -> None:
    dead = set()
    for ws in ws_clients:
        try:
            asyncio.create_task(ws.send_text(msg))
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)   # in-place — не создаёт локальную переменную


def _progress_callback(data: dict) -> None:
    _broadcast_raw(json.dumps(data))


file_index.setup(all_channels, poll_interval=poll_interval, broadcast=_broadcast)

app = FastAPI(title="Авокадо", version=VERSION)

STATIC_DIR = PROJECT_ROOT / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ──────────────────────────────────────────────────────────────────────────────
# Request logging middleware
# ──────────────────────────────────────────────────────────────────────────────
# Auth middleware  (defined first → runs INSIDE the logging wrapper)
# ──────────────────────────────────────────────────────────────────────────────

_AUTH_SKIP_PREFIXES = ("/static/", "/api/auth/")
_AUTH_SKIP_EXACT    = {"/login"}

@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path

    # Paths that never require auth
    if path in _AUTH_SKIP_EXACT or any(path.startswith(p) for p in _AUTH_SKIP_PREFIXES):
        return await call_next(request)

    if not _auth.auth_required():
        request.state.user = None
        return await call_next(request)

    token   = request.cookies.get(_auth.COOKIE_NAME)
    session = _auth.get_session(token) if token else None

    if not session:
        is_api = path.startswith("/api/") or path.startswith("/ws")
        if is_api:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        # Preserve original URL so login can redirect back
        next_url = path + (f"?{request.url.query}" if request.url.query else "")
        from urllib.parse import quote as _quote
        return RedirectResponse(f"/login?next={_quote(next_url)}", status_code=302)

    request.state.user = session
    return await call_next(request)


# ──────────────────────────────────────────────────────────────────────────────
# Request logging middleware (defined second → runs OUTSIDE, first on request)
# ──────────────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def _log_requests(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        log.error("Unhandled error %s %s: %s", request.method, request.url.path, exc)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    path = request.url.path
    # Skip static files and WebSocket upgrades from the log
    if not path.startswith("/static") and not path.startswith("/ws"):
        log.info(
            "%s %s%s -> %d (%.0f ms)",
            request.method,
            path,
            f"?{request.url.query}" if request.url.query else "",
            response.status_code,
            elapsed_ms,
        )
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    log.info("*" * 72)
    log.info("* Авокадо v%s  —  %s", VERSION,
             __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("*" * 72)
    log.info("Авокадо v%s starting up", VERSION)
    asyncio.create_task(_background_startup())


async def _background_startup() -> None:
    try:
        await file_index.initial_scan(progress=_progress_callback)
    except Exception as exc:
        log.error("Initial scan failed: %s", exc)
    file_index.start_polling()
    asyncio.create_task(_export_cleanup_loop())
    asyncio.create_task(_check_playlogs())


async def _check_playlogs() -> None:
    """Check each playlog source for yesterday's file and broadcast results."""
    global _playlog_status
    from datetime import timedelta
    from .playlist import check_sources
    check_date = (datetime.now() - timedelta(days=1)).date()
    _broadcast_raw(json.dumps({"type": "playlog_checking"}))
    results = []
    for pl_cfg in playlists_map.values():
        src = await asyncio.to_thread(check_sources, pl_cfg, check_date)
        results.append({"id": pl_cfg.id, "name": pl_cfg.name, "sources": src})
    _playlog_status = results
    _broadcast_raw(json.dumps({"type": "playlog_status", "playlogs": results}))
    ok  = sum(1 for pl in results for s in pl["sources"] if s["ok"])
    bad = sum(1 for pl in results for s in pl["sources"] if not s["ok"])
    log.info("Playlog check done: %d ok, %d unavailable (date=%s)", ok, bad, check_date)


# ──────────────────────────────────────────────────────────────────────────────
# Auth endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.post("/api/auth/login")
async def api_login(request: Request) -> JSONResponse:
    data     = await request.json()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if not username:
        raise HTTPException(status_code=400, detail="Введите имя пользователя")

    try:
        user = _auth.authenticate(username, password)
    except _auth.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if not user:
        raise HTTPException(status_code=401, detail="Неверное имя пользователя или пароль")

    token = _auth.create_session(user["username"], user["is_admin"], user["auth_type"])
    resp  = JSONResponse({
        "username":  user["username"],
        "is_admin":  user["is_admin"],
        "auth_type": user["auth_type"],
    })
    resp.set_cookie(
        _auth.COOKIE_NAME, token,
        max_age=_auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/auth/logout")
async def api_logout(request: Request) -> JSONResponse:
    token = request.cookies.get(_auth.COOKIE_NAME)
    if token:
        _auth.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_auth.COOKIE_NAME)
    return resp


@app.get("/api/auth/me")
async def api_me(request: Request) -> dict:
    if not _auth.auth_required():
        return {"auth_required": False, "username": None, "is_admin": True}
    token   = request.cookies.get(_auth.COOKIE_NAME)
    session = _auth.get_session(token) if token else None
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "auth_required": True,
        "username":  session["username"],
        "is_admin":  session["is_admin"],
        "auth_type": session["auth_type"],
    }


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
    return {"version": VERSION, "name": "Авокадо", "build_date": BUILD_DATE}


@app.post("/api/rescan_playlogs/{channel_id}")
async def rescan_playlogs(channel_id: str) -> dict:
    """Clear recent playlog cache for the channel's station and recheck all sources."""
    channel = channels_map.get(channel_id)
    if channel is None:
        raise HTTPException(404, f"Channel {channel_id!r} not found")

    # Collect unique playlist IDs for the whole station
    station = next((s for s in stations_map.values()
                    if any(ch.id == channel_id for ch in s.channels)), None)
    pl_ids: set[str] = set()
    if station:
        for ch in station.channels:
            pl_ids.update(ch.playlogs)
    else:
        pl_ids.update(channel.playlogs)

    from .playlist import invalidate_recent
    for pl_id in pl_ids:
        invalidate_recent(pl_id)
    log.info("Playlog cache invalidated for %d playlist(s) (station of %s)", len(pl_ids), channel_id)

    asyncio.create_task(_check_playlogs())
    return {"ok": True, "playlists": list(pl_ids)}


@app.get("/api/playlog_status")
async def get_playlog_status() -> list:
    """Last playlog source check result (for page reload without WS history)."""
    return _playlog_status


@app.get("/api/index_status")
async def get_index_status() -> dict:
    """Current file-index state: scanning progress or final file counts."""
    return file_index.get_state()


@app.post("/api/rescan/{channel_id}")
async def rescan_channel(channel_id: str) -> dict:
    """Force a rescan of one channel and broadcast the result."""
    idx = file_index.get_index(channel_id)
    if idx is None:
        raise HTTPException(404, f"Channel {channel_id!r} not found")

    ch = idx.channel
    log.info("Manual rescan requested for %s (%s)", ch.name, ch.id)

    async def _do_rescan() -> None:
        # Notify scan start
        _broadcast_raw(json.dumps({
            "type":         "index_scanning",
            "done":         0,
            "total":        1,
            "channel_id":   ch.id,
            "channel_name": ch.name,
            "rescan":       True,
        }))
        # Re-probe audio params (bitrate may have been wrong)
        await file_index.probe_channel(idx)
        # Always clear for a manual rescan — ensures stale durations
        # (e.g. files that were still recording at last scan time) are refreshed.
        idx.clear()
        success, err_msg = await file_index._try_scan(idx)
        n_files = len(idx._files)

        # Update state
        for entry in file_index.index_channels:
            if entry["id"] == ch.id:
                entry["files"]  = n_files
                entry["done"]   = True
                entry["failed"] = not success
                entry["error"]  = err_msg

        if success:
            from . import cache as disk_cache
            disk_cache.save(ch.id, list(idx._files))
            log.info("Manual rescan done for %s: %d files", ch.id, n_files)
            _broadcast_raw(json.dumps({
                "type":         "index_progress",
                "done":         1,
                "total":        1,
                "channel_id":   ch.id,
                "channel_name": ch.name,
                "files":        n_files,
                "rescan":       True,
            }))
        else:
            log.error("Manual rescan failed for %s", ch.id)
            _broadcast_raw(json.dumps({
                "type":         "index_error",
                "done":         1,
                "total":        1,
                "channel_id":   ch.id,
                "channel_name": ch.name,
                "error":        err_msg,
                "rescan":       True,
            }))

    asyncio.create_task(_do_rescan())
    return {"ok": True, "channel_id": channel_id, "channel_name": ch.name}


@app.post("/api/reload")
async def reload_config() -> dict:
    """Reload stations, playlists and settings from YAML files without restart."""
    log.info("Config reload requested")
    _load_config()
    file_index.setup(all_channels, poll_interval=poll_interval, broadcast=_broadcast)
    asyncio.create_task(_background_startup())
    msg = json.dumps({"type": "config_reloaded"})
    for ws in list(ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            pass
    return {
        "ok":       True,
        "stations": len(stations_map),
        "channels": len(channels_map),
        "playlogs": len(playlists_map),
    }


@app.post("/api/restart")
async def restart_server() -> dict:
    """Restart the server process (replaces current process with a fresh one)."""
    log.info("Server restart requested")
    async def _do_restart() -> None:
        await asyncio.sleep(0.3)
        # Start a new process then exit the current one
        subprocess.Popen([sys.executable] + sys.argv)
        os._exit(0)
    asyncio.create_task(_do_restart())
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# Export cleanup
# ──────────────────────────────────────────────────────────────────────────────

def _cleanup_exports() -> int:
    """Delete export files older than retention_days. Returns number deleted."""
    export_cfg   = settings.get("export", {})
    retention    = int(export_cfg.get("retention_days", 3))
    cutoff       = datetime.now() - timedelta(days=retention)
    deleted      = 0
    for f in EXPORT_DIR.iterdir():
        if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            try:
                f.unlink()
                deleted += 1
                log.debug("Export cleanup: removed %s", f.name)
            except Exception as exc:
                log.warning("Export cleanup: could not remove %s: %s", f.name, exc)
    if deleted:
        log.info("Export cleanup: deleted %d expired file(s) (retention=%dd)", deleted, retention)
    return deleted


async def _export_cleanup_loop() -> None:
    """Run export cleanup on startup and then daily at the configured time."""
    # Run once at startup
    _cleanup_exports()

    export_cfg   = settings.get("export", {})
    cleanup_time = export_cfg.get("cleanup_time", "03:00")
    try:
        hh, mm = map(int, cleanup_time.split(":"))
    except Exception:
        hh, mm = 3, 0

    while True:
        now  = datetime.now()
        next_run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if next_run <= now:
            next_run = next_run.replace(day=next_run.day + 1)
        await asyncio.sleep((next_run - now).total_seconds())
        _cleanup_exports()


# ──────────────────────────────────────────────────────────────────────────────
# REST API — data
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/stations")
async def get_stations() -> list[dict]:
    return [
        {
            "id":   st.id,
            "name": st.name,
            "channels": [
                {
                    "id":             ch.id,
                    "name":           ch.name,
                    "file_extension": ch.file_extension,
                    "sample_rate":    ch.sample_rate,
                    "bitrate":        ch.bitrate,
                    "playlogs":       ch.playlogs,
                    "local_path":     ch.local_path,
                    "smb": {
                        "host":  ch.smb.host,
                        "share": ch.smb.share,
                        "path":  ch.smb.path,
                    } if ch.smb else None,
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
    end:   float = Query(..., description="Unix timestamp"),
) -> list[dict]:
    idx = file_index.get_index(channel_id)
    if idx is None:
        raise HTTPException(404, f"Channel {channel_id!r} not found")
    return idx.get_intervals(datetime.fromtimestamp(start), datetime.fromtimestamp(end))


@app.get("/api/playlist/{channel_id}")
async def get_playlist(
    channel_id: str,
    start: float = Query(...),
    end:   float = Query(...),
) -> list[dict]:
    channel = channels_map.get(channel_id)
    if channel is None:
        raise HTTPException(404)
    start_dt = datetime.fromtimestamp(start)
    end_dt   = datetime.fromtimestamp(end)
    entries  = []
    for pl_id in channel.playlogs:
        pl_cfg = playlists_map.get(pl_id)
        if pl_cfg is None:
            continue
        for e in get_entries(pl_cfg, start_dt, end_dt):
            cl_colors = pl_cfg.class_colors
            cl_names  = pl_cfg.class_names
            entries.append({
                "timestamp": e.timestamp.timestamp(),
                "title":     e.title,
                "cls":       e.cls,
                "duration":  e.duration,
                "color":     cl_colors.get(e.cls, cl_colors.get("default", "#e0e0e0")),
                "cls_name":  cl_names.get(e.cls, e.cls),
                "elem_id":   e.elem_id,
            })
    entries.sort(key=lambda x: x["timestamp"])
    return entries


@app.get("/api/playlist_config/{channel_id}")
async def get_playlist_config(channel_id: str) -> dict:
    channel = channels_map.get(channel_id)
    if channel is None:
        raise HTTPException(404)
    colors: dict = {}
    names:  dict = {}
    for pl_id in channel.playlogs:
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
    channel:     str   = Query(...),
    start:       float = Query(...),
    end:         float = Query(...),
    format:      str   = Query("mp3"),
    bitrate:     str   = Query("192k"),
    sample_rate: int   = Query(None),
):
    channel_cfg = channels_map.get(channel)
    if channel_cfg is None:
        raise HTTPException(404)
    start_dt = datetime.fromtimestamp(start)
    end_dt   = datetime.fromtimestamp(end)
    media_types = {"mp3": "audio/mpeg", "wav": "audio/wav", "aac": "audio/aac"}
    media_type  = media_types.get(format, "audio/mpeg")

    log.info(
        "Stream %s  %s → %s  fmt=%s br=%s",
        channel, start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        end_dt.strftime("%H:%M:%S"), format, bitrate,
    )

    async def gen():
        async for chunk in stream_audio(channel_cfg, start_dt, end_dt, format, bitrate, sample_rate):
            yield chunk

    return StreamingResponse(gen(), media_type=media_type)


@app.post("/api/audio/export")
async def audio_export(body: dict) -> dict:
    channel_id  = body.get("channel_id", "")
    start       = float(body.get("start", 0))
    end         = float(body.get("end", 0))
    fmt         = body.get("format", "mp3")
    bitrate     = body.get("bitrate") or "192k"
    sample_rate = body.get("sample_rate")
    copy_mode   = bool(body.get("copy_mode", False))
    if sample_rate is not None:
        sample_rate = int(sample_rate)

    channel_cfg = channels_map.get(channel_id)
    if channel_cfg is None:
        raise HTTPException(404)

    start_dt = datetime.fromtimestamp(start)
    end_dt   = datetime.fromtimestamp(end)
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', channel_cfg.name)
    safe_name = ' '.join(safe_name.split())
    date_str  = start_dt.strftime("%Y-%m-%d")
    time_str  = start_dt.strftime("%H-%M-%S")
    fname     = f"{safe_name} {date_str} {time_str}.{fmt}"
    out_path  = str(EXPORT_DIR / fname)

    log.info(
        "Export %s  %s → %s  fmt=%s%s → %s",
        channel_id, start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        end_dt.strftime("%H:%M:%S"), fmt,
        " [copy]" if copy_mode else f" br={bitrate}",
        fname,
    )

    try:
        await export_audio(
            channel_cfg, start_dt, end_dt, fmt, bitrate, sample_rate, out_path, copy_mode
        )
    except Exception as exc:
        log.error("Export failed for %s: %s", channel_id, exc)
        raise HTTPException(500, str(exc))

    return {"filename": fname, "download_url": f"/api/audio/download/{quote(fname)}"}


@app.get("/api/audio/download/{filename}")
async def audio_download(filename: str) -> FileResponse:
    path = EXPORT_DIR / filename
    if not path.exists():
        raise HTTPException(404)
    log.info("Download: %s", filename)
    return FileResponse(str(path), filename=filename, media_type="application/octet-stream")


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket
# ──────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    ws_clients.add(ws)
    log.debug("WebSocket connected (total: %d)", len(ws_clients))
    try:
        while True:
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)
        log.debug("WebSocket disconnected (total: %d)", len(ws_clients))
