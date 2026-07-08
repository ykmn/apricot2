"""FastAPI application — Абрикос 2"""
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
from contextlib import asynccontextmanager
from typing import Any

import aiofiles
import urllib.request
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth as _auth
from . import ui_state as _ui_state

from . import app_logger as _app_logger
from .app_logger import get_logger
from .audio import export_audio, set_ffmpeg_path, stream_audio
from .config import (
    invalidate_secrets_cache, load_playlists, load_settings, load_stations,
)
from .file_index import file_index
from .playlist import get_entries
from .smb_mount import MOUNTS_DIR, SUPPORTED as _SMB_SUPPORTED, _is_mounted

VERSION = "1.2.076"
PROJECT_ROOT = Path(__file__).parent.parent
EXPORT_DIR = PROJECT_ROOT / "export"
EXPORT_DIR.mkdir(exist_ok=True)

log = get_logger("apricot2")


def _apply_smb_mounts(channels: list, playlists_map: dict) -> None:
    """For every SMB source whose share is already mounted, replace smb: with local_path."""
    if not _SMB_SUPPORTED:
        return

    # Build case-insensitive index of existing mount dirs: (host_lower, share_lower) → Path
    _mount_index: dict[tuple[str, str], Path] = {}
    if MOUNTS_DIR.exists():
        for host_dir in MOUNTS_DIR.iterdir():
            if not host_dir.is_dir():
                continue
            for share_dir in host_dir.iterdir():
                if _is_mounted(share_dir):
                    _mount_index[(host_dir.name.lower(), share_dir.name.lower())] = share_dir

    def _patch(obj) -> None:
        """Set local_path on obj if its smb share is mounted and local_path not already set."""
        if not obj.smb or obj.local_path:
            return
        key = (obj.smb.host.lower(), obj.smb.share.lower())
        mp = _mount_index.get(key)
        if mp is None:
            return
        local = mp / obj.smb.path if obj.smb.path else mp
        obj.local_path = str(local)
        log.info("Using local mount for //%s/%s/%s → %s",
                 obj.smb.host, obj.smb.share, obj.smb.path, obj.local_path)

    for ch in channels:
        _patch(ch)
    for pl in playlists_map.values():
        for src in pl.sources:
            _patch(src)


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
playlog_refresh_interval: int = 1800   # seconds; overridden by settings.yaml
rescan_all_on_startup: bool = False    # overridden by settings.yaml
_playlog_status: list = []   # last check result, returned by /api/playlog_status

ws_clients: set[WebSocket] = set()


def _load_config() -> None:
    global settings, stations_map, playlists_map, all_channels, channels_map, \
           poll_interval, playlog_refresh_interval, rescan_all_on_startup
    # settings.yaml → secret.yaml/users.yaml/ldap.yaml → станции → плейлоги.
    # Станции и плейлоги резолвят secret-id при парсинге, поэтому кеш секретов
    # обязан быть сброшен (и auth-кеш ldap.yaml — тоже) до их загрузки.
    settings = load_settings()
    invalidate_secrets_cache()
    _auth.invalidate_auth_cache()
    stations_map = load_stations()
    playlists_map = load_playlists()
    if ffmpeg := settings.get("ffmpeg_path"):
        set_ffmpeg_path(ffmpeg)
    all_channels = [ch for st in stations_map.values() for ch in st.channels]
    _apply_smb_mounts(all_channels, playlists_map)
    channels_map = {ch.id: ch for ch in all_channels}
    poll_interval = settings.get("watcher", {}).get("poll_interval", 10)
    playlog_refresh_interval = int(
        settings.get("playlogs", {}).get("today_refresh_interval", 1800)
    )
    rescan_all_on_startup = bool(
        settings.get("indexing", {}).get("rescan_all_on_startup", False)
    )
    _auth.configure(
        session_ttl=int(settings.get("server", {}).get("session_ttl", 7 * 24 * 3600)),
    )
    _srv = settings.get("server", {})
    _app_logger.configure(
        screen_level=str(_srv.get("log_screen", "INFO")),
        file_level=str(_srv.get("log_file", "DEBUG")),
    )
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


async def _safe_send(ws: WebSocket, msg: str) -> None:
    """Send a WebSocket message; silently remove the client on any error."""
    try:
        await ws.send_text(msg)
    except Exception:
        ws_clients.discard(ws)


def _broadcast_raw(msg: str) -> None:
    for ws in list(ws_clients):
        asyncio.create_task(_safe_send(ws, msg))


def _progress_callback(data: dict) -> None:
    _broadcast_raw(json.dumps(data))


file_index.setup(all_channels, poll_interval=poll_interval, broadcast=_broadcast)

@asynccontextmanager
async def _lifespan(application: FastAPI):
    log.info("*" * 72)
    log.info("* Абрикос 2 v%s  —  %s", VERSION,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("*" * 72)
    log.info("Абрикос 2 v%s starting up", VERSION)
    _auth.load_sessions()
    asyncio.create_task(_background_startup())
    yield


app = FastAPI(title="Абрикос 2", version=VERSION, lifespan=_lifespan)

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
        log.debug(
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


async def _background_startup() -> None:
    try:
        await file_index.initial_scan(progress=_progress_callback,
                                       force_full=rescan_all_on_startup)
    except Exception as exc:
        log.error("Initial scan failed: %s", exc)
    file_index.start_polling()
    asyncio.create_task(_export_cleanup_loop())
    asyncio.create_task(_preload_playlogs())
    asyncio.create_task(_playlog_today_refresh_loop())


async def _preload_playlogs() -> None:
    """Preload and cache playlogs for the last PRELOAD_DAYS days, then broadcast source status."""
    global _playlog_status
    from datetime import timedelta
    from .playlist import check_sources, preload, PRELOAD_DAYS

    configs = list(playlists_map.values())
    if not configs:
        return

    _broadcast_raw(json.dumps({"type": "playlog_checking"}))

    def _run_preload():
        preload(configs, days_back=PRELOAD_DAYS)

    await asyncio.to_thread(_run_preload)

    # Check per-source folder health for the status bar
    results = []
    for pl_cfg in configs:
        src = await asyncio.to_thread(check_sources, pl_cfg)
        results.append({"id": pl_cfg.id, "name": pl_cfg.name, "sources": src})

    _playlog_status = results
    _broadcast_raw(json.dumps({"type": "playlog_status", "playlogs": results}))
    ok  = sum(1 for pl in results for s in pl["sources"] if s["ok"])
    bad = sum(1 for pl in results for s in pl["sources"] if not s["ok"])
    log.info("Playlog preload done (%d days): %d ok, %d unavailable", PRELOAD_DAYS, ok, bad)


async def _playlog_today_refresh_loop() -> None:
    """Periodically drop today's in-memory playlog cache so the next request re-reads the source."""
    from .playlist import invalidate_today
    while True:
        await asyncio.sleep(playlog_refresh_interval)
        configs = list(playlists_map.values())
        if not configs:
            continue
        for cfg in configs:
            await asyncio.to_thread(invalidate_today, cfg.id)
        log.info("Playlog today cache refreshed (%d playlist(s))", len(configs))


# ──────────────────────────────────────────────────────────────────────────────
# Auth endpoints
# ──────────────────────────────────────────────────────────────────────────────

def _html_response(filename: str) -> HTMLResponse:
    """Serve an HTML file with ?v=VERSION cache-busting on static assets.

    The HTML itself is sent with Cache-Control: no-store so the browser
    always fetches a fresh copy, picks up the new ?v= query string, and
    therefore reloads JS/CSS/JSON when the version changes.
    """
    text = (STATIC_DIR / filename).read_text(encoding="utf-8")
    # Inject app version so JS can use it for cache-busting fetch() calls.
    develop = bool(settings.get("develop", False))
    text = text.replace(
        "</head>",
        f'<script>window.__APP_VERSION__="{VERSION}";window.__BUILD_DATE__="{BUILD_DATE}";'
        f'window.__DEVELOP__={"true" if develop else "false"};</script>\n</head>',
    )
    text = re.sub(r'((?:href|src)=["\'])([^"\']+\.(?:css|js))(["\'])',
                  rf'\g<1>\g<2>?v={VERSION}\g<3>', text)
    return HTMLResponse(
        content=text,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/login", include_in_schema=False)
async def login_page() -> HTMLResponse:
    return _html_response("login.html")


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
        log.warning(
            "Login failed: user=%s ip=%s reason=%s",
            username,
            request.client.host if request.client else "—",
            exc,
        )
        raise HTTPException(status_code=401, detail=str(exc))
    except RuntimeError as exc:
        log.error("Auth service error for '%s': %s", username, exc)
        raise HTTPException(status_code=503, detail=str(exc))

    if not user:
        log.warning("Login failed: user=%s ip=%s reason=no user returned", username,
                    request.client.host if request.client else "—")
        raise HTTPException(status_code=401, detail="Неверное имя пользователя или пароль")

    client_ip = request.client.host if request.client else ""
    token = _auth.create_session(user["username"], user["is_admin"], user["auth_type"], user.get("domain", ""), client_ip)
    log.info(
        "Login: user=%s auth=%s domain=%s admin=%s ip=%s",
        user["username"],
        user["auth_type"],
        user.get("domain", "") or "—",
        user["is_admin"],
        request.client.host if request.client else "—",
    )
    resp  = JSONResponse({
        "username":  user["username"],
        "is_admin":  user["is_admin"],
        "auth_type": user["auth_type"],
        "domain":    user.get("domain", ""),
    })
    _ssl_enabled = bool(settings.get("server", {}).get("ssl", {}).get("enabled", False))
    resp.set_cookie(
        _auth.COOKIE_NAME, token,
        max_age=_auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_ssl_enabled,
    )
    return resp


@app.post("/api/auth/logout")
async def api_logout(request: Request) -> JSONResponse:
    token = request.cookies.get(_auth.COOKIE_NAME)
    if token:
        session = _auth.get_session(token)
        if session:
            log.info(
                "Logout: user=%s ip=%s",
                session["username"],
                request.client.host if request.client else "—",
            )
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
        "domain":    session.get("domain", ""),
    }


# ──────────────────────────────────────────────────────────────────────────────
# UI state (per-user persistence)
# ──────────────────────────────────────────────────────────────────────────────

def _current_username(request: Request) -> str | None:
    """Return username from session cookie, or None when auth is disabled."""
    if not _auth.auth_required():
        return None
    token = request.cookies.get(_auth.COOKIE_NAME)
    session = _auth.get_session(token) if token else None
    return session["username"] if session else None


@app.get("/api/ui-state")
async def get_ui_state(request: Request) -> dict:
    return _ui_state.load(_current_username(request))


@app.put("/api/ui-state")
async def put_ui_state(request: Request) -> dict:
    body = await request.json()
    state: dict = {}
    if "channel_id" in body:
        state["channel_id"] = str(body["channel_id"]) if body["channel_id"] else None
    if "timeline_time" in body:
        state["timeline_time"] = float(body["timeline_time"]) if body["timeline_time"] is not None else None
    if "sel_start" in body:
        state["sel_start"] = float(body["sel_start"]) if body["sel_start"] is not None else None
    if "sel_end" in body:
        state["sel_end"] = float(body["sel_end"]) if body["sel_end"] is not None else None
    if "log_items" in body:
        state["log_items"] = list(body["log_items"]) if body["log_items"] is not None else []
    _ui_state.save(_current_username(request), state)
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    return _html_response("index.html")


# ──────────────────────────────────────────────────────────────────────────────
# REST API — meta
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/version")
async def get_version() -> dict:
    return {"version": VERSION, "name": "Абрикос 2", "build_date": BUILD_DATE}


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

    asyncio.create_task(_preload_playlogs())
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


def _require_admin(request: Request) -> None:
    """Raise 403 if auth is enabled and the current session is not admin."""
    if not _auth.auth_required():
        return
    session = _auth.get_session(request.cookies.get(_auth.COOKIE_NAME))
    if not session or not session.get("is_admin"):
        raise HTTPException(403, "Admin access required")


@app.post("/api/reload")
async def reload_config(request: Request) -> dict:
    """Reload stations, playlists and settings from YAML files without restart."""
    _require_admin(request)
    log.info("Config reload requested")
    _load_config()
    file_index.setup(all_channels, poll_interval=poll_interval, broadcast=_broadcast)
    asyncio.create_task(_background_startup())
    _broadcast_raw(json.dumps({"type": "config_reloaded"}))
    return {
        "ok":       True,
        "stations": len(stations_map),
        "channels": len(channels_map),
        "playlogs": len(playlists_map),
    }


@app.post("/api/restart")
async def restart_server(request: Request) -> dict:
    """Restart the server process.

    When running under systemd (Restart=on-failure or Restart=always) just
    exit — systemd will relaunch the process with the correct environment
    (authbind, venv, working directory).  Spawning a child process would
    bypass authbind and escape systemd supervision.
    """
    _require_admin(request)
    log.info("Server restart requested")
    async def _do_restart() -> None:
        await asyncio.sleep(0.3)
        os._exit(0)
    asyncio.create_task(_do_restart())
    return {"ok": True}


@app.get("/api/admin/sessions")
async def admin_list_sessions(request: Request) -> dict:
    """List all active sessions for admin UI."""
    _require_admin(request)
    current_token = request.cookies.get(_auth.COOKIE_NAME)
    return {"sessions": _auth.list_sessions(current_token)}


@app.delete("/api/admin/sessions/{session_id}")
async def admin_terminate_session(session_id: str, request: Request) -> dict:
    """Forcibly terminate a session by its hashed ID."""
    _require_admin(request)
    current_token = request.cookies.get(_auth.COOKIE_NAME)
    current_sid = None
    if current_token:
        import hashlib as _hl
        current_sid = _hl.sha256(current_token.encode()).hexdigest()[:16]
    if session_id == current_sid:
        raise HTTPException(status_code=400, detail="Cannot terminate your own session via this endpoint")
    found = _auth.terminate_session_by_id(session_id)
    if not found:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@app.get("/api/check_updates")
async def check_updates(request: Request) -> dict:
    """Compare local git HEAD with the latest commit on the configured GitHub repo."""
    _require_admin(request)
    repo_url: str = settings.get("update", {}).get("repo_url", "").rstrip("/")
    if not repo_url:
        raise HTTPException(status_code=501, detail="update.repo_url not configured")

    # Extract owner/repo from https://github.com/owner/repo[/...]
    import re as _re
    m = _re.search(r"github\.com/([^/]+/[^/]+)", repo_url)
    if not m:
        raise HTTPException(status_code=400, detail="Cannot parse GitHub repo URL")
    owner_repo = m.group(1)

    # Local HEAD
    try:
        local_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        local_date = subprocess.check_output(
            ["git", "log", "-1", "--format=%ci"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        local_msg = subprocess.check_output(
            ["git", "log", "-1", "--format=%s"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"git error: {exc}")

    # Remote HEAD via GitHub API
    api_url = f"https://api.github.com/repos/{owner_repo}/commits/HEAD"
    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json",
                                                        "User-Agent": f"Apricot2/{VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            remote_data = json.loads(resp.read().decode())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API error: {exc}")

    remote_sha = remote_data.get("sha", "")
    remote_date = remote_data.get("commit", {}).get("committer", {}).get("date", "")
    remote_msg = remote_data.get("commit", {}).get("message", "").split("\n")[0]

    return {
        "up_to_date": local_sha == remote_sha,
        "local":  {"sha": local_sha[:10],  "date": local_date,  "message": local_msg},
        "remote": {"sha": remote_sha[:10], "date": remote_date, "message": remote_msg},
        "repo_url": repo_url,
    }


@app.post("/api/update")
async def update_server(request: Request) -> dict:
    """Run git pull, then restart the server."""
    _require_admin(request)
    log.info("Server update (git pull) requested")
    try:
        result = subprocess.run(
            ["git", "pull"],
            capture_output=True, text=True, timeout=60
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"git pull failed: {output}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="git pull timed out")

    log.info("git pull output: %s", output)

    async def _do_restart() -> None:
        await asyncio.sleep(0.5)
        os._exit(0)
    asyncio.create_task(_do_restart())
    return {"ok": True, "output": output}


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
            next_run = next_run + timedelta(days=1)
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
                    "playlogs":        ch.playlogs,
                    "playlogs_offset": ch.playlogs_offset,
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
    delay_s  = channel.playlogs_offset / 1000.0
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
                "timestamp": e.timestamp.timestamp() + delay_s,
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
        inner = stream_audio(channel_cfg, start_dt, end_dt, format, bitrate, sample_rate)
        try:
            async for chunk in inner:
                yield chunk
        finally:
            # aclose() must not be called while the generator frame is still
            # on the stack (e.g. when the client disconnects mid-chunk and
            # uvicorn cancels the task).  Shield the call so it runs after
            # the current frame unwinds; suppress errors if already closed.
            try:
                await asyncio.shield(inner.aclose())
            except Exception:
                pass

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
    path = (EXPORT_DIR / filename).resolve()
    if not path.is_relative_to(EXPORT_DIR.resolve()):
        raise HTTPException(403, "Access denied")
    if not path.exists():
        raise HTTPException(404)
    log.info("Download: %s", filename)
    return FileResponse(str(path), filename=path.name, media_type="application/octet-stream")


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
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ws_clients.discard(ws)
        log.debug("WebSocket disconnected (total: %d)", len(ws_clients))
