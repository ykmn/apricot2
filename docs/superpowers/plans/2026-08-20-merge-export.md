# «Скачать всё»: экспорт-диалог с режимами «отдельно»/«склеить» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the immediate no-dialog behavior of "Скачать всё" with a modal offering per-fragment download (current behavior) or merging all selected fragments into one audio file with chapter markers at each fragment's original start time.

**Architecture:** Backend gets one new pure-Python/ffmpeg orchestration function (`export_audio_merged`) reusing the existing per-item `export_audio()`, plus one new REST endpoint. Frontend gets one new modal (`#export-all-modal`) and its own JS controller, replacing the old one-line `exportAll()`.

**Tech Stack:** FastAPI, ffmpeg (subprocess), mutagen (already a dependency, used for post-encode duration measurement), vanilla JS/HTML/CSS (no build step).

## Global Constraints

- No automated test suite exists in this project (see `CLAUDE.md`) — every task's "test" step is a manual verification command (ffmpeg/ffprobe/curl/browser), not pytest.
- Do not commit — this repo requires an explicit user command before every commit (`CLAUDE.md`: "Do not commit without special user's command"). Every task's last step is "stage for review", not "commit".
- Marker title format: `ДД.ММ.ГГГГ ЧЧ:ММ:СС` of the fragment's start time — exact format already used in `static/app.js:777`.
- Merged filename: `{Название канала|Несколько каналов} склейка с {YYYY-MM-DD HH-MM-SS}.{ext}`, sanitized with the same regex already used in `main.py:audio_export` (`re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', ...)`).
- MP3 gets real ID3v2 chapters via ffmpeg. WAV gets markers via a hand-written RIFF `cue `/`LIST`/`adtl`/`labl` chunk (ffmpeg's WAV muxer silently drops chapters — verified with real ffmpeg during design). AAC gets no markers (raw ADTS has no container for metadata) — verified with real ffmpeg.
- Merge mode always transcodes every fragment to the requested format/bitrate/sample_rate (never native-format copy-through) so cross-channel/cross-format fragments can always be concatenated safely.

---

### Task 1: `export_audio()` gets an `allow_copy_mode` escape hatch

**Files:**
- Modify: `app/audio.py:818-827` (signature), `app/audio.py:860-862` (auto-copy-mode logic)

**Interfaces:**
- Produces: `export_audio(..., allow_copy_mode: bool = True)` — when `False`, disables the automatic "native format → copy_mode=True" upgrade, forcing a real transcode even when `out_format` matches the channel's native extension.

- [ ] **Step 1: Add the parameter and guard the auto-upgrade**

In `app/audio.py`, change the signature at line 818:

```python
async def export_audio(
    channel: ChannelConfig,
    start: datetime,
    end: datetime,
    out_format: str = "mp3",
    bitrate: str = "192k",
    sample_rate: int | None = None,
    out_path: str | None = None,
    copy_mode: bool = False,
    allow_copy_mode: bool = True,
) -> str:
```

And change the auto-upgrade block around line 860:

```python
        native_ext = channel.file_extension.lower()
        if allow_copy_mode and not copy_mode and out_format == native_ext and native_ext in ("mp3", "aac"):
            copy_mode = True
```

- [ ] **Step 2: Manually verify existing callers are unaffected**

Run: `python -c "import ast; ast.parse(open('app/audio.py').read())"` — confirms no syntax error.
Grep for other callers to confirm none pass 8 positional args that would now collide with the new parameter:

Run: `grep -n "export_audio(" app/main.py`
Expected: the one call in `main.py:audio_export` passes exactly `channel_cfg, start_dt, end_dt, fmt, bitrate, sample_rate, out_path, copy_mode` (8 positional args) — matches the unchanged existing parameter order; `allow_copy_mode` stays at its default (`True`), so single-item export behavior is unchanged.

- [ ] **Step 3: Stage for review (no commit)**

```bash
git add app/audio.py
git status
```

---

### Task 2: Chapter-metadata and WAV-marker helpers in `app/audio.py`

**Files:**
- Modify: `app/audio.py` (add `import struct`, `import mutagen.aac`, `import mutagen.mp3`, `import mutagen.wave` near the top; add new functions near the bottom, after `export_audio` and before `_stage_one`)

**Interfaces:**
- Produces:
  - `_format_chapter_title(dt: datetime) -> str`
  - `_build_chapters_metadata(chapters: list[tuple[float, float, str]]) -> str`
  - `_write_wav_markers(path: str, markers: list[tuple[int, str]]) -> None`
  - `_probe_duration(path: str, out_format: str) -> float`

- [ ] **Step 1: Add imports**

At the top of `app/audio.py`, alongside the existing imports (after `import time`):

```python
import struct

import mutagen.aac
import mutagen.mp3
import mutagen.wave
```

`mutagen` is already a hard dependency (`requirements.txt`), used the same way in `app/audio_probe.py` — no optional-import guard needed here.

- [ ] **Step 2: Add the four helper functions**

Insert after `export_audio()` (after its closing `return out_path`, before `async def _stage_one`):

```python
def _format_chapter_title(dt: datetime) -> str:
    """Chapter/marker title — matches the fragment timestamp format already
    shown in the log-list panel (see static/app.js's `fmt` helper)."""
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def _build_chapters_metadata(chapters: list[tuple[float, float, str]]) -> str:
    """Build an ffmpeg ;FFMETADATA1 chapters file.

    chapters: (start_seconds, end_seconds, title) tuples in playback order.
    Special characters ffmpeg's metadata parser treats specially (\\, =, ;,
    #, newline) are escaped per its documented metadata format.
    """
    lines = [";FFMETADATA1"]
    for start_s, end_s, title in chapters:
        safe_title = (
            title.replace("\\", "\\\\")
                 .replace("=", "\\=")
                 .replace(";", "\\;")
                 .replace("#", "\\#")
                 .replace("\n", " ")
        )
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={int(round(start_s * 1000))}")
        lines.append(f"END={int(round(end_s * 1000))}")
        lines.append(f"title={safe_title}")
    return "\n".join(lines) + "\n"


def _write_wav_markers(path: str, markers: list[tuple[int, str]]) -> None:
    """Append RIFF cue points + LIST/adtl/labl labels to a PCM WAV file.

    ffmpeg's WAV muxer accepts -map_chapters but silently drops them from
    the written file (verified: ffprobe shows nothing after such a mux).
    This writes the same 'cue '/'LIST'/'adtl'/'labl' chunk layout Sound
    Forge/Audition produce — ffmpeg's own WAV *demuxer* already parses that
    layout back as chapters, so it's the reliable path for WAV markers.

    markers: (sample_position, title) tuples in order.
    """
    if not markers:
        return
    with open(path, "r+b") as f:
        header = f.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise RuntimeError(f"Not a RIFF/WAVE file: {path}")
        riff_size = struct.unpack_from("<I", header, 4)[0]

        cue_body = struct.pack("<I", len(markers))
        for i, (pos, _title) in enumerate(markers, start=1):
            cue_body += struct.pack("<II4sIII", i, pos, b"data", 0, 0, pos)
        cue_chunk = b"cue " + struct.pack("<I", len(cue_body)) + cue_body
        if len(cue_body) % 2:
            cue_chunk += b"\x00"

        adtl_body = b"adtl"
        for i, (_pos, title) in enumerate(markers, start=1):
            text = title.encode("utf-8", errors="replace") + b"\x00"
            labl_body = struct.pack("<I", i) + text
            labl_chunk = b"labl" + struct.pack("<I", len(labl_body)) + labl_body
            if len(labl_body) % 2:
                labl_chunk += b"\x00"
            adtl_body += labl_chunk
        list_chunk = b"LIST" + struct.pack("<I", len(adtl_body)) + adtl_body
        if len(adtl_body) % 2:
            list_chunk += b"\x00"

        appended = cue_chunk + list_chunk
        f.seek(0, 2)
        f.write(appended)
        f.seek(4)
        f.write(struct.pack("<I", riff_size + len(appended)))


def _probe_duration(path: str, out_format: str) -> float:
    """Read a just-encoded fragment's real duration in seconds via mutagen —
    used instead of the requested (end - start) to avoid marker-position
    drift from trim/encode rounding."""
    if out_format == "wav":
        audio = mutagen.wave.WAVE(path)
    elif out_format == "aac":
        audio = mutagen.aac.AAC(path)
    else:
        audio = mutagen.mp3.MP3(path)
    return float(audio.info.length)
```

- [ ] **Step 3: Manually verify the WAV marker writer against the known-good sample**

The project root has `Sound1.wav` (Sound Forge output, 3 markers) used during design to reverse-engineer this exact chunk layout. Verify the new writer produces bytes ffmpeg reads back the same way:

```bash
python - << 'EOF'
import shutil, sys
sys.path.insert(0, ".")
from app.audio import _write_wav_markers

shutil.copyfile("Sound1.wav", "/tmp/marker_test.wav")
# Sound1.wav already has markers baked in by Sound Forge; this just proves
# the writer runs without corrupting a real WAV file and appends parseable
# chunks. A cleaner positive-path check follows in Task 3's manual test
# (round-trip through the real merge endpoint).
_write_wav_markers("/tmp/marker_test.wav", [(0, "Test Marker"), (44100, "Second")])
EOF
ffprobe -v error -show_chapters /tmp/marker_test.wav
```

Expected: two `[CHAPTER]` blocks reported by `ffprobe`, titled "Test Marker" and "Second" (in addition to whatever Sound Forge originally wrote, now followed by a second, malformed `cue`/`LIST` pair — this is just a smoke test that the byte-writing logic doesn't crash and produces parseable chunks; don't worry about the file being semantically double-chaptered, it's a throwaway copy in `/tmp`).

- [ ] **Step 4: Stage for review (no commit)**

```bash
git add app/audio.py
git status
```

---

### Task 3: `export_audio_merged()` orchestration function

**Files:**
- Modify: `app/audio.py` (add after the Task 2 helpers)

**Interfaces:**
- Consumes: `export_audio()` (Task 1, with `allow_copy_mode=False`), `_build_chapters_metadata`, `_write_wav_markers`, `_probe_duration`, `_format_chapter_title` (Task 2)
- Produces: `async def export_audio_merged(items: list[tuple[ChannelConfig, datetime, datetime]], out_format: str, bitrate: str, sample_rate: int | None, out_path: str) -> str`

- [ ] **Step 1: Implement the function**

```python
async def export_audio_merged(
    items: list[tuple[ChannelConfig, datetime, datetime]],
    out_format: str,
    bitrate: str,
    sample_rate: int | None,
    out_path: str,
) -> str:
    """Export multiple (possibly cross-channel) fragments concatenated into
    a single file, with a marker at each fragment boundary titled by that
    fragment's original start date-time.

    Every fragment is always transcoded to the requested format/bitrate/
    sample_rate (export_audio(..., allow_copy_mode=False)) so fragments
    from different channels/native formats concatenate safely.
    """
    if not items:
        raise RuntimeError("No items to merge")

    with tempfile.TemporaryDirectory() as tmpdir:
        seg_paths: list[str] = []
        for i, (channel, start, end) in enumerate(items):
            seg_path = str(Path(tmpdir) / f"part_{i:04d}.{out_format}")
            await export_audio(
                channel, start, end, out_format, bitrate, sample_rate,
                out_path=seg_path, copy_mode=False, allow_copy_mode=False,
            )
            seg_paths.append(seg_path)

        durations = [_probe_duration(p, out_format) for p in seg_paths]

        concat_list = Path(tmpdir) / "merge_concat.txt"
        with concat_list.open("w", encoding="utf-8") as f:
            f.write("ffconcat version 1.0\n")
            for p in seg_paths:
                safe_p = p.replace("\\", "/").replace("'", "\\'")
                f.write(f"file '{safe_p}'\n")

        if out_format == "mp3":
            cumulative = 0.0
            chapters = []
            for (_channel, start, _end), dur in zip(items, durations):
                chapters.append((cumulative, cumulative + dur, _format_chapter_title(start)))
                cumulative += dur
            chapters_path = Path(tmpdir) / "chapters.txt"
            chapters_path.write_text(_build_chapters_metadata(chapters), encoding="utf-8")

            cmd = [
                FFMPEG, "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-i", str(chapters_path),
                "-map_metadata", "1", "-map_chapters", "1",
                "-c", "copy", "-id3v2_version", "3", "-write_id3v2", "1",
                out_path,
            ]
        else:
            cmd = [
                FFMPEG, "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-c", "copy",
                out_path,
            ]

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
            last_lines = "\n".join(stderr_text.splitlines()[-10:])
            try:
                Path(out_path).unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}:\n{last_lines}")

        if out_format == "wav":
            sr = sample_rate or items[0][0].sample_rate or 44100
            cumulative = 0.0
            markers = []
            for (_channel, start, _end), dur in zip(items, durations):
                markers.append((int(round(cumulative * sr)), _format_chapter_title(start)))
                cumulative += dur
            _write_wav_markers(out_path, markers)

    return out_path
```

- [ ] **Step 2: Manually verify end-to-end against real channel data**

This requires a running server with at least one configured channel that has indexed audio (see `CLAUDE.md` — "Run the server", `python apricot2.py`). Deferred to Task 5's manual verification, which exercises this function through the HTTP endpoint and the browser UI — the most representative test available without a unit-test framework in this project.

- [ ] **Step 3: Stage for review (no commit)**

```bash
git add app/audio.py
git status
```

---

### Task 4: `POST /api/audio/export_merge` endpoint

**Files:**
- Modify: `app/main.py:28` (import), `app/main.py` (new route, placed after the existing `audio_export` route, before `audio_download`, i.e. after line 1062)

**Interfaces:**
- Consumes: `export_audio_merged` (Task 3), `channels_map`, `EXPORT_DIR`, `_broadcast_raw` (all already module-level in `main.py`)
- Produces: `POST /api/audio/export_merge` — `{filename, download_url}`, reusing the existing `/api/audio/download/{filename}` route unchanged.

- [ ] **Step 1: Add the import**

Change `app/main.py:28`:

```python
from .audio import export_audio, export_audio_merged, set_ffmpeg_path, stream_audio
```

- [ ] **Step 2: Add the endpoint**

Insert after the existing `audio_export` route (after its `return {"filename": fname, "download_url": download_url}` at line 1062), before `audio_download`:

```python
@app.post("/api/audio/export_merge")
async def audio_export_merge(body: dict) -> dict:
    raw_items = body.get("items") or []
    if not raw_items:
        raise HTTPException(400, "No items provided")
    fmt         = body.get("format", "mp3")
    bitrate     = body.get("bitrate") or "192k"
    sample_rate = body.get("sample_rate")
    if sample_rate is not None:
        sample_rate = int(sample_rate)

    resolved: list[tuple] = []
    channel_names: set[str] = set()
    for raw in raw_items:
        channel_id = raw.get("channel_id", "")
        channel_cfg = channels_map.get(channel_id)
        if channel_cfg is None:
            raise HTTPException(404, f"Unknown channel: {channel_id}")
        start_dt = datetime.fromtimestamp(float(raw.get("start", 0)))
        end_dt   = datetime.fromtimestamp(float(raw.get("end", 0)))
        resolved.append((channel_cfg, start_dt, end_dt))
        channel_names.add(channel_cfg.name)

    channel_label = channel_names.pop() if len(channel_names) == 1 else "Несколько каналов"
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', channel_label)
    safe_name = ' '.join(safe_name.split())
    now = datetime.now()
    fname = f"{safe_name} склейка с {now.strftime('%Y-%m-%d %H-%M-%S')}.{fmt}"
    out_path = str(EXPORT_DIR / fname)

    log.info("Export merge: %d items fmt=%s br=%s -> %s", len(resolved), fmt, bitrate, fname)

    _broadcast_raw(json.dumps({
        "type":       "export_progress",
        "channel_id": "_merged",
        "filename":   fname,
    }))

    try:
        await export_audio_merged(resolved, fmt, bitrate, sample_rate, out_path)
    except Exception as exc:
        log.error("Merge export failed: %s", exc)
        _broadcast_raw(json.dumps({
            "type":       "export_error",
            "channel_id": "_merged",
            "filename":   fname,
            "error":      str(exc),
        }))
        raise HTTPException(500, str(exc))

    download_url = f"/api/audio/download/{quote(fname)}"
    _broadcast_raw(json.dumps({
        "type":         "export_done",
        "channel_id":   "_merged",
        "filename":     fname,
        "download_url": download_url,
    }))
    return {"filename": fname, "download_url": download_url}
```

- [ ] **Step 3: Manually verify the route starts without error**

Run: `python -c "import ast; ast.parse(open('app/main.py').read())"` — confirms no syntax error. Full functional verification happens in Task 5 once the frontend can drive it (or via `curl` against a running server with a real configured channel — see Task 5).

- [ ] **Step 4: Stage for review (no commit)**

```bash
git add app/main.py
git status
```

---

### Task 5: Frontend — new "Export All" modal (HTML + CSS)

**Files:**
- Modify: `static/index.html` (insert new modal block after the existing `#export-modal`, i.e. after line 231)
- Modify: `static/style.css` (add styling for the new radio-group, after the existing `.modal-buttons`/`#export-progress` rules around line 761)

**Interfaces:**
- Produces DOM ids consumed by Task 6: `export-all-modal`, `expall-mode-separate`, `expall-mode-merge`, `expall-format`, `expall-samplerate`, `expall-bitrate-row`, `expall-bitrate`, `expall-wav-note`, `expall-copy-note`, `expall-order-block`, `expall-order`, `expall-aac-warning`, `expall-ok`, `expall-cancel`.

- [ ] **Step 1: Insert the modal markup**

In `static/index.html`, after the closing `</div>` of `#export-modal` (line 231), insert:

```html

  <!-- ── Export-all modal ─────────────────────────────────────────── -->
  <div id="export-all-modal" class="modal hidden">
    <div class="modal-box">
      <h3 data-i18n="export.all_title">Экспорт выбранных фрагментов</h3>

      <div class="expall-radio-group">
        <label class="expall-radio-opt">
          <input type="radio" name="expall-mode" id="expall-mode-separate" value="separate" checked>
          <div class="expall-radio-label" data-i18n="export.merge_mode_separate">Скачать отдельными файлами</div>
        </label>
        <label class="expall-radio-opt">
          <input type="radio" name="expall-mode" id="expall-mode-merge" value="merge">
          <div class="expall-radio-label" data-i18n="export.merge_mode_merge">Склеить фрагменты в один файл</div>
        </label>
      </div>

      <label><span data-i18n="export.format_label">Формат:</span>
        <select id="expall-format">
          <option value="mp3">MP3</option>
          <option value="wav">WAV PCM</option>
          <option value="aac">AAC</option>
        </select>
      </label>
      <label><span data-i18n="export.samplerate_label">Частота дискретизации:</span>
        <select id="expall-samplerate">
          <option value="32000">32 000 Гц</option>
          <option value="44100" selected>44 100 Гц</option>
          <option value="48000">48 000 Гц</option>
        </select>
      </label>
      <label id="expall-bitrate-row"><span data-i18n="export.bitrate_label">Битрейт:</span>
        <select id="expall-bitrate">
          <option value="320k">320 kbps</option>
          <option value="192k" selected>192 kbps</option>
          <option value="128k">128 kbps</option>
          <option value="64k">64 kbps</option>
        </select>
      </label>
      <div id="expall-wav-note"  class="hidden" style="font-size:11px;color:var(--text2)" data-i18n="export.wav_note">WAV экспортируется как PCM 16-bit без сжатия</div>
      <div id="expall-copy-note" class="hidden" style="font-size:11px;color:var(--accent2)" data-i18n="export.copy_note">⚡ Оригинальный формат канала — экспорт без перекодирования</div>

      <div id="expall-order-block" class="hidden">
        <label><span data-i18n="export.merge_order_label">Порядок склейки:</span>
          <select id="expall-order">
            <option value="chrono" selected data-i18n="export.merge_order_chrono">По времени начала фрагмента</option>
            <option value="list" data-i18n="export.merge_order_list">Как в списке «Выбранные фрагменты»</option>
          </select>
        </label>
        <div id="expall-aac-warning" class="hidden" style="font-size:11px;color:#e2b13c" data-i18n="export.merge_aac_warning">⚠ Для AAC маркеры невозможны технически — файл будет склеен без меток</div>
      </div>

      <div class="modal-buttons">
        <button id="expall-ok"     class="btn-green" data-i18n="btn.export">Экспортировать</button>
        <button id="expall-cancel" class="btn-danger" data-i18n="btn.cancel">Отмена</button>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: Add CSS for the radio group**

In `static/style.css`, after the `#export-progress` rule (around line 761), add:

```css
.expall-radio-group { display: flex; flex-direction: column; gap: 8px; }
.expall-radio-opt {
  display: flex; align-items: center; gap: 8px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
  cursor: pointer; background: var(--bg3);
}
.expall-radio-label { color: var(--text); }
```

- [ ] **Step 3: Manually verify markup loads**

Run the server (`python apricot2.py`) and open the app in a browser; open devtools console — confirm no HTML parse errors and that `document.getElementById('export-all-modal')` returns a non-null element. This is a static/markup check only — behavior is wired in Task 6.

- [ ] **Step 4: Stage for review (no commit)**

```bash
git add static/index.html static/style.css
git status
```

---

### Task 6: Frontend — Export-All modal controller (JS)

**Files:**
- Modify: `static/app.js`:
  - `initLogList()` at line 741 (change the `btn-export-all` listener target)
  - DOMContentLoaded handler at line 112 (register the new modal init)
  - `_isCopyMode`/`_buildExportBody` (around line 914-934) — parameterize
  - `doExport()` (around line 936-953) — pass explicit params to `_buildExportBody`
  - Replace `exportAll()` and `doExportItem()` (lines 955-990) with the new controller functions

**Interfaces:**
- Consumes: DOM ids from Task 5; `_getChannelById`, `_triggerDownload`, `api()`, `logItems`, `I18n.t` (all pre-existing in `app.js`)
- Produces: `openExportAllModal()` (wired to `#btn-export-all` click), `doExportItem(item, fmt, bitrate, samplerate)` (new signature, replaces the old parameterless one)

- [ ] **Step 1: Parameterize `_isCopyMode` / `_buildExportBody`**

Replace (around line 914-934):

```javascript
function _isCopyMode(item) {
  const ch  = _getChannelById(item.channel_id);
  const fmt = document.getElementById('exp-format').value;
  return !!(ch && ch.file_extension.toLowerCase() === fmt);
}

function _buildExportBody(item) {
  const fmt        = document.getElementById('exp-format').value;
  const bitrate    = document.getElementById('exp-bitrate').value;
  const samplerate = parseInt(document.getElementById('exp-samplerate').value, 10);
  const copyMode   = _isCopyMode(item);
  return {
    channel_id:  item.channel_id,
    start:       item.start,
    end:         item.end,
    format:      fmt,
    bitrate:     (fmt === 'wav' || copyMode) ? null : bitrate,
    sample_rate: copyMode ? null : samplerate,
    copy_mode:   copyMode,
  };
}
```

with:

```javascript
function _isCopyMode(item, fmt) {
  const ch = _getChannelById(item.channel_id);
  return !!(ch && ch.file_extension.toLowerCase() === fmt);
}

function _buildExportBody(item, fmt, bitrate, samplerate) {
  const copyMode = _isCopyMode(item, fmt);
  return {
    channel_id:  item.channel_id,
    start:       item.start,
    end:         item.end,
    format:      fmt,
    bitrate:     (fmt === 'wav' || copyMode) ? null : bitrate,
    sample_rate: copyMode ? null : samplerate,
    copy_mode:   copyMode,
  };
}
```

- [ ] **Step 2: Update `doExport()` to pass explicit params**

Replace `doExport()` (around line 936-953):

```javascript
async function doExport() {
  if (!exportTarget) return;
  const prog = document.getElementById('export-progress');
  prog.classList.remove('hidden');
  prog.textContent = I18n.t('export.in_progress');
  const fmt        = document.getElementById('exp-format').value;
  const bitrate    = document.getElementById('exp-bitrate').value;
  const samplerate = parseInt(document.getElementById('exp-samplerate').value, 10);
  try {
    const result = await api('/api/audio/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_buildExportBody(exportTarget, fmt, bitrate, samplerate)),
    });
    prog.textContent = I18n.t('export.done');
    _triggerDownload(result.download_url, result.filename);
    setTimeout(() => document.getElementById('export-modal').classList.add('hidden'), 1200);
  } catch (e) {
    prog.textContent = I18n.t('export.error', { msg: e.message });
  }
}
```

- [ ] **Step 3: Replace `exportAll()`/`doExportItem()` with the new controller**

Replace the whole block from `async function exportAll()` through the end of `async function doExportItem(item) {...}` (originally lines 955-990) with:

```javascript
function _expAllMode() {
  return document.getElementById('expall-mode-merge').checked ? 'merge' : 'separate';
}

function _expAllSingleChannel() {
  if (logItems.length === 0) return null;
  const firstId = logItems[0].channel_id;
  if (!logItems.every(i => i.channel_id === firstId)) return null;
  return _getChannelById(firstId);
}

function initExportAllModal() {
  document.getElementById('expall-cancel').addEventListener('click', () => {
    document.getElementById('export-all-modal').classList.add('hidden');
  });
  document.getElementById('expall-ok').addEventListener('click', doExportAll);
  document.getElementById('expall-format').addEventListener('change', _updateExportAllFields);
  document.getElementById('expall-mode-separate').addEventListener('change', _updateExportAllFields);
  document.getElementById('expall-mode-merge').addEventListener('change', _updateExportAllFields);
}

function _updateExportAllFields() {
  const fmt        = document.getElementById('expall-format').value;
  const mode       = _expAllMode();
  const bitrateRow = document.getElementById('expall-bitrate-row');
  const wavNote    = document.getElementById('expall-wav-note');
  const copyNote   = document.getElementById('expall-copy-note');
  const orderBlock = document.getElementById('expall-order-block');
  const aacWarning = document.getElementById('expall-aac-warning');
  const srSel      = document.getElementById('expall-samplerate');
  const brSel      = document.getElementById('expall-bitrate');

  if (fmt === 'wav') {
    bitrateRow.classList.add('hidden');
    wavNote.classList.remove('hidden');
  } else {
    bitrateRow.classList.remove('hidden');
    wavNote.classList.add('hidden');
  }

  orderBlock.classList.toggle('hidden', mode !== 'merge');
  aacWarning.classList.toggle('hidden', !(mode === 'merge' && fmt === 'aac'));

  const singleChannel = _expAllSingleChannel();
  const isCopy = mode === 'separate' && !!singleChannel && singleChannel.file_extension.toLowerCase() === fmt;
  copyNote.classList.toggle('hidden', !isCopy);
  srSel.disabled = isCopy;
  brSel.disabled = isCopy;
}

function openExportAllModal() {
  if (logItems.length === 0) return;
  document.getElementById('expall-mode-separate').checked = true;

  const ch     = _expAllSingleChannel();
  const fmtSel = document.getElementById('expall-format');
  const srSel  = document.getElementById('expall-samplerate');
  const brSel  = document.getElementById('expall-bitrate');
  if (ch) {
    const fmtMap = { mp3: 'mp3', wav: 'wav', aac: 'aac' };
    const ext = ch.file_extension.toLowerCase();
    if (fmtMap[ext]) fmtSel.value = fmtMap[ext];
    const srOpt = [...srSel.options].find(o => parseInt(o.value) === ch.sample_rate);
    if (srOpt) srSel.value = srOpt.value;
    if (ch.bitrate) {
      const brOpt = [...brSel.options].find(o => o.value === ch.bitrate);
      if (brOpt) brSel.value = brOpt.value;
    }
  } else {
    fmtSel.value = 'mp3';
    srSel.value = '44100';
    brSel.value = '192k';
  }

  _updateExportAllFields();
  document.getElementById('export-all-modal').classList.remove('hidden');
}

async function doExportAll() {
  const mode = _expAllMode();
  document.getElementById('export-all-modal').classList.add('hidden');
  if (mode === 'merge') {
    await doExportMerge();
  } else {
    await exportAllSeparate();
  }
}

async function exportAllSeparate() {
  if (logItems.length === 0) return;
  const fmt        = document.getElementById('expall-format').value;
  const bitrate    = document.getElementById('expall-bitrate').value;
  const samplerate = parseInt(document.getElementById('expall-samplerate').value, 10);
  const total = logItems.length;
  const el = document.getElementById('play-loading');
  let failed = 0;
  for (let i = 0; i < total; i++) {
    el.textContent = I18n.t('export.all_progress', { n: i + 1, total });
    el.classList.remove('hidden');
    const ok = await doExportItem(logItems[i], fmt, bitrate, samplerate);
    if (!ok) failed++;
  }
  el.textContent = failed
    ? I18n.t('export.all_errors', { ok: total - failed, total, failed })
    : I18n.t('export.all_done', { total });
  setTimeout(() => el.classList.add('hidden'), 3000);
}

async function doExportItem(item, fmt, bitrate, samplerate) {
  try {
    const result = await api('/api/audio/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_buildExportBody(item, fmt, bitrate, samplerate)),
    });
    _triggerDownload(result.download_url, result.filename);
    await new Promise(r => setTimeout(r, 500));
    return true;
  } catch (e) {
    console.error('Export failed for', item, e);
    return false;
  }
}

async function doExportMerge() {
  const fmt         = document.getElementById('expall-format').value;
  const bitrate      = document.getElementById('expall-bitrate').value;
  const samplerate   = parseInt(document.getElementById('expall-samplerate').value, 10);
  const orderChrono  = document.getElementById('expall-order').value === 'chrono';

  let ordered = logItems.slice();
  if (orderChrono) ordered.sort((a, b) => a.start - b.start);

  const el = document.getElementById('play-loading');
  el.textContent = I18n.t('export.merge_progress');
  el.classList.remove('hidden');

  try {
    const result = await api('/api/audio/export_merge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: ordered.map(i => ({ channel_id: i.channel_id, start: i.start, end: i.end })),
        format: fmt,
        bitrate: fmt === 'wav' ? null : bitrate,
        sample_rate: samplerate,
      }),
    });
    el.textContent = I18n.t('export.merge_done');
    _triggerDownload(result.download_url, result.filename);
    setTimeout(() => el.classList.add('hidden'), 3000);
  } catch (e) {
    el.textContent = I18n.t('export.merge_error', { msg: e.message });
    setTimeout(() => el.classList.add('hidden'), 5000);
  }
}
```

- [ ] **Step 4: Wire the button and modal init**

At `static/app.js:741`, replace:

```javascript
  document.getElementById('btn-export-all').addEventListener('click', exportAll);
```

with:

```javascript
  document.getElementById('btn-export-all').addEventListener('click', openExportAllModal);
```

At `static/app.js:112`, after `initExportModal();`, add:

```javascript
  initExportAllModal();
```

- [ ] **Step 5: Manually verify in the browser — separate mode (regression check)**

Run `python apricot2.py`, open the app, select a channel with indexed audio, add 2+ fragments to the log list (via timeline selection + "Добавить фрагмент"), click "Скачать всё".
Expected: modal opens with "Скачать отдельными файлами" pre-selected, format pre-filled to the channel's native format (single-channel case), no order selector visible. Click "Экспортировать" — same per-fragment downloads as before this change (verify browser downloads N files).

- [ ] **Step 6: Manually verify in the browser — merge mode, MP3**

In the same modal, select "Склеить фрагменты в один файл", format MP3, click "Экспортировать".
Expected: one file downloads named `{канал} склейка с {дата}.mp3`. Open it in any player that shows chapters (or verify via `ffprobe -show_chapters <file>` from a terminal) — one chapter per fragment, titled with each fragment's start date-time in `ДД.ММ.ГГГГ ЧЧ:ММ:СС` format, in the chosen order.

- [ ] **Step 7: Manually verify in the browser — merge mode, WAV**

Repeat with format WAV. Expected: one merged WAV file downloads; `ffprobe -show_chapters <file>` shows the same markers (via the manually-written cue/LIST/adtl chunks).

- [ ] **Step 8: Manually verify in the browser — merge mode, AAC**

Repeat with format AAC. Expected: the "маркеры невозможны для AAC" note appears in the modal; one merged AAC file downloads; `ffprobe -show_chapters <file>` shows nothing (expected — no crash, no error).

- [ ] **Step 9: Manually verify cross-channel merge**

Add fragments from two different channels to the log list, merge with any format.
Expected: no crash; output filename uses "Несколько каналов" instead of a channel name; audio from both channels is audible in the merged file in the chosen order.

- [ ] **Step 10: Stage for review (no commit)**

```bash
git add static/app.js
git status
```

---

### Task 7: i18n — new translation keys

**Files:**
- Modify: `static/languages/ru.json`, `static/languages/en.json`, `static/languages/fr.json` — all in the existing `"export"` object (after `"all_errors"`)

**Interfaces:** none (leaf content used by Task 6 via `data-i18n` attributes and `I18n.t()` calls)

- [ ] **Step 1: Add Russian keys**

In `static/languages/ru.json`, inside the `"export"` object, after `"all_errors": "⚠ Экспорт: {ok} из {total} — ошибки: {failed}"`, add:

```json
    "all_title": "Экспорт выбранных фрагментов",
    "merge_mode_separate": "Скачать отдельными файлами",
    "merge_mode_merge": "Склеить фрагменты в один файл",
    "merge_order_label": "Порядок склейки:",
    "merge_order_chrono": "По времени начала фрагмента",
    "merge_order_list": "Как в списке «Выбранные фрагменты»",
    "merge_aac_warning": "⚠ Для AAC маркеры невозможны технически — файл будет склеен без меток",
    "merge_progress": "⬇ Склейка фрагментов…",
    "merge_done": "✓ Склейка готова! Скачивание…",
    "merge_error": "Ошибка склейки: {msg}"
```

(remember the trailing comma on the now-not-last preceding line, and valid JSON — no trailing comma after the last key in the object)

- [ ] **Step 2: Add English keys**

In `static/languages/en.json`, same location:

```json
    "all_title": "Export selected fragments",
    "merge_mode_separate": "Download as separate files",
    "merge_mode_merge": "Merge fragments into one file",
    "merge_order_label": "Merge order:",
    "merge_order_chrono": "By fragment start time",
    "merge_order_list": "As listed in \"Selected fragments\"",
    "merge_aac_warning": "⚠ Chapter markers are not possible for AAC — the file will be merged without them",
    "merge_progress": "⬇ Merging fragments…",
    "merge_done": "✓ Merge complete! Downloading…",
    "merge_error": "Merge error: {msg}"
```

- [ ] **Step 3: Add French keys**

In `static/languages/fr.json`, same location:

```json
    "all_title": "Exporter les fragments sélectionnés",
    "merge_mode_separate": "Télécharger des fichiers séparés",
    "merge_mode_merge": "Fusionner les fragments en un seul fichier",
    "merge_order_label": "Ordre de fusion :",
    "merge_order_chrono": "Par heure de début du fragment",
    "merge_order_list": "Comme dans la liste « Fragments sélectionnés »",
    "merge_aac_warning": "⚠ Les marqueurs sont techniquement impossibles en AAC — le fichier sera fusionné sans eux",
    "merge_progress": "⬇ Fusion des fragments…",
    "merge_done": "✓ Fusion terminée ! Téléchargement…",
    "merge_error": "Erreur de fusion : {msg}"
```

- [ ] **Step 4: Manually verify all three files are valid JSON**

Run: `python -c "import json; [json.load(open(f'static/languages/{l}.json', encoding='utf-8')) for l in ('ru','en','fr')]; print('OK')"`
Expected: `OK` with no exception.

- [ ] **Step 5: Stage for review (no commit)**

```bash
git add static/languages/ru.json static/languages/en.json static/languages/fr.json
git status
```

---

### Task 8: Documentation — README and dev-history

**Files:**
- Modify: `README.md` (`### Экспорт` section, around line 792-804)
- Modify: `docs/dev-history.md` (append a new dated entry, update the "Последнее обновление" line and table of contents)

**Interfaces:** none

- [ ] **Step 1: Update README's Export section**

In `README.md`, replace the line:

```
- **Скачать всё** — экспортирует и скачивает все фрагменты из списка последовательно
```

with:

```
- **Скачать всё** — открывает диалог с выбором:
  - **Скачать отдельными файлами** — экспортирует и скачивает все фрагменты из списка последовательно (текущее поведение по умолчанию)
  - **Склеить фрагменты в один файл** — объединяет все выбранные фрагменты в один аудиофайл; порядок склейки — по времени начала фрагмента или как в списке (на выбор). Внутри файла расставляются маркеры с датой-временем начала каждого исходного фрагмента:
    - MP3 — главы (ID3v2), читаются большинством плееров
    - WAV — маркеры (RIFF cue-points), читаются DAW/аудиоредакторами (Sound Forge, Audition и т.п.)
    - AAC — маркеры технически невозможны (сырой поток без контейнера), файл склеивается без меток
```

- [ ] **Step 2: Append a dev-history entry**

In `docs/dev-history.md`, update line 4 (`Последнее обновление`) to the new version once `scripts/bump_version.py` has run at commit time — leave a placeholder note for the committer rather than guessing the exact patch number, since `VERSION` in `app/main.py` only bumps via the pre-commit hook (per `CLAUDE.md`, "Do not manually edit VERSION"). Add a new numbered entry (24) after entry 23 in the table of contents and a matching section at the end of the file:

Add to the table of contents (after line 27):

```
24. [«Скачать всё»: диалог экспорта с режимами «отдельно»/«склеить», маркеры в MP3/WAV](#24-скачать-всё-диалог-экспорта-с-режимами-отдельносклеить-маркеры-в-mp3wav)
```

Append at the end of the file:

```markdown

---

## 24. «Скачать всё»: диалог экспорта с режимами «отдельно»/«склеить», маркеры в MP3/WAV

> По нажатию на «Скачать всё» должно появляться GUI-окно с предложением конвертировать (как при нажатии на «Скачать», по умолчанию без конвертации) и опциями: «Склеить фрагменты в один» и «Скачать отдельными файлами». При выборе «склеить» внутри аудиофайла между фрагментами должны расстанавливаться маркеры с названием по дате-времени начала фрагмента.

- Новая модалка `#export-all-modal` (`static/index.html`, `static/app.js`) заменяет прежний однокликовый `exportAll()`: выбор режима (отдельно/склеить), формат/частота/битрейт (как в одиночном экспорте), порядок склейки (по времени начала / как в списке) — виден только при склейке.
- Новый эндпоинт `POST /api/audio/export_merge` и функция `export_audio_merged()` (`app/audio.py`): каждый фрагмент транскодируется отдельно (`export_audio(..., allow_copy_mode=False)` — новый параметр, отключающий авто-copy-mode для гарантии совместимой конкатенации фрагментов с разных каналов), затем склеивается через `ffmpeg -f concat -c copy`.
- Маркеры по формату (проверено вручную на реальном ffmpeg):
  - **MP3** — реальные ID3v2-главы через `;FFMETADATA1` + `-map_chapters`.
  - **WAV** — ffmpeg молча не пишет главы в WAV-мьюксер (`ffprobe` после экспорта ничего не показывает), поэтому маркеры дописываются вручную как RIFF `cue `/`LIST`/`adtl`/`labl` чанки — тот же байтовый формат, что пишет Sound Forge (сверено с образцом `Sound1.wav`), и который ffmpeg сам умеет читать обратно.
  - **AAC** — маркеры невозможны технически (сырой ADTS без контейнера), файл склеивается без меток.
- Название маркера — дата-время начала фрагмента в формате `ДД.ММ.ГГГГ ЧЧ:ММ:СС` (как в лог-листе).
- Имя склеенного файла: `{Название канала|Несколько каналов} склейка с {YYYY-MM-DD HH-MM-SS}.{ext}`.
```

- [ ] **Step 3: Manually verify docs render sensibly**

Open `README.md` and `docs/dev-history.md` in a Markdown preview (or just re-read the diff) — confirm headings/anchors are well-formed and the new TOC entry's anchor slug matches the new section heading exactly.

- [ ] **Step 4: Stage for review (no commit)**

```bash
git add README.md docs/dev-history.md
git status
```

---

## Final check (all tasks complete)

- [ ] Run `git status` and `git diff --stat` to confirm the full changeset: `app/audio.py`, `app/main.py`, `static/index.html`, `static/style.css`, `static/app.js`, `static/languages/{ru,en,fr}.json`, `README.md`, `docs/dev-history.md`.
- [ ] Do **not** commit — report the staged changeset to the user and wait for an explicit commit instruction (per `CLAUDE.md`).
