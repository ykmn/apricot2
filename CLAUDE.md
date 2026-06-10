# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**Абрикос 2 (Apricot 2)** — FastAPI web application for navigating radio broadcast recordings: browsing an audio timeline, selecting fragments, and exporting to MP3/WAV/AAC. Backend is Python, frontend is vanilla JS (no build step).

## Running the server

```bash
# Create and activate venv (first time)
python3 -m venv .venv
source .venv/bin/activate      # Linux/macOS
.venv\Scripts\Activate.ps1     # Windows PowerShell

pip install -r requirements.txt

# Run
python apricot2.py
```

Default port is `8765` (configured in `config/settings.yaml`). The entry point validates all YAML configs and attempts SMB auto-mounts before starting uvicorn.

There are **no automated tests**. Verification is done manually by running the app.

## Pre-commit hook — version bump

Every commit automatically increments the patch version in `app/main.py` (`VERSION = "X.Y.ZZZ"`) via `scripts/bump_version.py`. The hook also stages the updated `app/main.py`, so both files are included in the same commit. Do not manually edit `VERSION`.

## Architecture

### Backend (`app/`)

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app, all HTTP routes, WebSocket `/ws`, auth middleware, `_require_admin()` guard |
| `auth.py` | Session management, local users (`config/users.yaml`), LDAP/AD authentication |
| `config.py` | YAML loading (`load_stations`, `load_playlists`, `load_settings`), secret resolution |
| `models.py` | Dataclasses: `ChannelConfig`, `StationConfig`, `PlaylistConfig`, `AudioFile`, etc. |
| `file_index.py` | In-memory index of audio files per channel (backed by `SortedList`); startup loads disk cache, then background-rescans for correct bitrate |
| `audio.py` | ffmpeg streaming (`stream_audio` → `AsyncGenerator`) and export (`export_audio`) for multi-file ranges |
| `playlist.py` | Reads CSV playlog files (local or SMB), caches per-day to `cache_playlogs/`, 5-min in-memory TTL for today |
| `smb_client.py` | Unified file access: local path or SMB via `smbprotocol`; abstracts `listdir`, `open`, `stat` |
| `smb_mount.py` | Linux/macOS `mount.cifs` / `mount_smbfs` auto-mount at startup |
| `cache.py` | Disk cache for file index (`cache/`) |
| `audio_probe.py` | ffprobe wrapper to detect bitrate/sample_rate for MP3 files |
| `app_logger.py` | Dual-sink logger: screen (INFO by default) + rotating file (DEBUG) |

### Config resolution order (important)

1. `config/secret.yaml` — all SMB credentials keyed by integer `id`
2. Station YAML references credentials via `secret: N`; password can also come from `password_env: ENV_VAR`
3. `config/ldap.yaml` — if absent, auth is **completely disabled**; if present, enables auth middleware
4. `config/users.yaml` — local users checked **before** LDAP; if absent, a random temporary password is generated on each startup (printed to stderr)

### Frontend (`static/`)

Three JS files, no framework, no bundler:

- **`app.js`** — application state (`stations`, `currentChannel`, `logItems`, `playlistEntries`), channel selection (`selectChannel`), audio playback, export modal, log list, WebSocket reconnect loop
- **`timeline.js`** — canvas-based multi-row timeline; exposes API (`setChannel`, `setTime`, `setSelStart/End`, `getSelection`, `refreshAvailability`, etc.)
- **`i18n.js`** — key-based translation loader; language files are `static/languages/{en,ru,fr}.json`

### Data flow for audio playback

1. `file_index` resolves which `AudioFile` objects cover the requested time range
2. `audio.py` builds an ffmpeg concat command (temp file list for multi-file spans) and streams `pipe:1` back through FastAPI `StreamingResponse`
3. For SMB sources: `smb_client` opens files via `smbprotocol`; on Linux/macOS they may instead be accessed as local mounts set up at startup

### WebSocket

The `/ws` endpoint broadcasts JSON messages to all connected clients:
- `availability_update` — new/removed audio intervals for a channel (triggers timeline repaint)
- `config_reloaded` — after `POST /api/reload`
- `index_progress` / `index_error` — file scan status
- `export_progress` / `export_done` / `export_error` — async export status
- `playlog_checking` / `playlog_status` — playlist scan results

### Admin-only endpoints

`POST /api/reload` and `POST /api/restart` require `is_admin=True` in the session (enforced by `_require_admin(request)` in `main.py`). When auth is disabled, all users are treated as admins.

### Security notes (recently fixed)

- `/api/audio/download/{filename}` resolves the path and checks `is_relative_to(EXPORT_DIR)` — never pass user input directly to `EXPORT_DIR / filename` without this check
- Default credentials are random per-startup when `users.yaml` is absent; do not revert to `admin`/`admin`
- Admin endpoints must call `_require_admin(request)` before any privileged action
