'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let stations        = [];
let currentChannel  = null;    // {id, name, ...}
let playlistEntries = [];
let logItems        = [];      // [{id, channel_id, channel_name, start, end, label}]
let exportTarget    = null;    // log item being exported
let isPlaying       = false;
let playStartTime   = null;    // real Date.now() when playback started
let playFromTs      = null;    // timeline ts when playback started
let playAnimFrame   = null;
let _tlAnimUpdate   = false;   // true only during animation-frame setTime calls
let _seekPending    = false;   // true while user scrolled and audio hasn't started yet

const _LOG_KEY = 'apricot-logitems';  // legacy localStorage key — used only for migration

const audio = document.getElementById('audio-player');

// ── Bootstrap ──────────────────────────────────────────────────────────────
// ── Auth state (populated on load) ────────────────────────────────────────────
let currentUser = null;   // {username, is_admin, auth_required} or null

async function initAuth() {
  try {
    const data = await api('/api/auth/me');
    currentUser = data;
  } catch (e) {
    // 401 → middleware already redirected to /login before we got here,
    // but handle gracefully just in case.
    if (e.message && e.message.includes('401')) {
      location.href = '/login';
    }
    currentUser = { auth_required: false, is_admin: true, username: null };
  }
  _applyAuthUI();
}

function _applyAuthUI() {
  const isAdmin = currentUser?.is_admin !== false;

  // Show/hide admin-only menu items
  document.querySelectorAll('[data-admin-only]').forEach(el => {
    el.classList.toggle('hidden', !isAdmin);
  });

  // Show user info + logout if auth is active
  if (currentUser?.auth_required && currentUser?.username) {
    const info = document.getElementById('menu-userinfo');
    const sep  = document.getElementById('menu-sep-logout');
    const btn  = document.getElementById('menu-logout');
    if (info) {
      const displayName = currentUser.domain
        ? `${currentUser.domain}\\${currentUser.username}`
        : currentUser.username;
      info.textContent = `👤 ${displayName}`;
      info.classList.remove('hidden');
    }
    if (sep)  sep.classList.remove('hidden');
    if (btn)  btn.classList.remove('hidden');
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  await I18n.init();
  I18n.applyToDOM();

  await initAuth();

  Timeline.init({
    onTimeChange: ts => {
      refreshPlaylistForVisible();
      _scheduleHighlight();
      if (isPlaying && !_tlAnimUpdate) _debouncedSeek(ts);
      _updateBrandLink();
      _syncMobClock(ts);
    },
    onSelChange: (s, e) => {
      updateSelLabel(s, e);
      _scheduleUiStateSave();
    },
  });

  await loadStations();
  buildChannelDropdown();
  initChannelSearch();
  initTransport();

  // Load persisted UI state from server; migrate localStorage if first time
  const _uiState = await _fetchUiState();
  if (_uiState.log_items && _uiState.log_items.length > 0) {
    logItems = _uiState.log_items;
    // Clean up legacy localStorage entry if it still exists
    try { localStorage.removeItem(_LOG_KEY); } catch(_) {}
  } else {
    // Migration: first open after update — pull from localStorage → server
    try {
      const raw = localStorage.getItem(_LOG_KEY);
      if (raw) {
        logItems = JSON.parse(raw);
        localStorage.removeItem(_LOG_KEY);
        _scheduleUiStateSave();  // persist migrated data to server
      }
    } catch(_) {}
  }

  initLogList();
  renderLogList();
  initExportModal();
  initClockControls();
  initMobClock();
  initMobileTabs();
  initHamburgerMenu();
  initHotkeys();
  initStatusBar();
  connectWebSocket();
  loadVersion();

  // Restore channel and time: URL params take priority over saved state
  const _urlParams = new URLSearchParams(location.search);
  const _urlCh = _urlParams.get('ch');
  const _urlTs = _urlParams.get('t');

  const _restoreCh = _urlCh || _uiState.channel_id;
  if (_restoreCh) {
    for (const st of stations) {
      const ch = st.channels.find(c => c.id === _restoreCh);
      if (ch) { selectChannel(ch, st); break; }
    }
  }

  const _restoreTs = _urlTs ? parseFloat(_urlTs) : (_uiState.timeline_time || Date.now() / 1000);
  Timeline.setTime(_restoreTs);

  // Restore selection markers
  if (_uiState.sel_start != null) Timeline.setSelStart(_uiState.sel_start);
  if (_uiState.sel_end   != null) Timeline.setSelEnd(_uiState.sel_end);

  _updateBrandLink();

  // Copy URL button
  document.getElementById('btn-copy-url').addEventListener('click', () => {
    const ts = Math.round(Timeline.getCenterTime());
    const params = new URLSearchParams({ t: ts });
    if (currentChannel) params.set('ch', currentChannel.id);
    const url = `${location.origin}${location.pathname}?${params}`;
    navigator.clipboard.writeText(url).catch(() => {});
    const btn = document.getElementById('btn-copy-url');
    const prev = btn.textContent;
    btn.textContent = '✓';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = prev; btn.classList.remove('copied'); }, 1500);
  });
});

// ── API helpers ────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// ── Stations / channels ────────────────────────────────────────────────────
async function loadStations() {
  try {
    stations = await api('/api/stations');
  } catch (e) {
    stations = [];
    console.warn('Could not load stations:', e);
  }
}

function buildChannelDropdown() {
  const dd = document.getElementById('channel-dropdown');
  dd.innerHTML = '';
  stations.forEach(st => {
    const grp = document.createElement('div');
    grp.className = 'dropdown-station';
    grp.textContent = st.name;
    dd.appendChild(grp);

    st.channels.forEach(ch => {
      const item = document.createElement('div');
      item.className = 'dropdown-channel';
      item.dataset.id = ch.id;
      item.textContent = ch.name;
      item.addEventListener('mousedown', e => {
        e.preventDefault();
        selectChannel(ch, st);
      });
      dd.appendChild(item);
    });
  });
}

function filterChannelDropdown(query) {
  const dd = document.getElementById('channel-dropdown');
  const q  = query.toLowerCase();
  let anyVisible = false;
  dd.querySelectorAll('.dropdown-channel').forEach(el => {
    const match = el.textContent.toLowerCase().includes(q);
    el.style.display = match ? '' : 'none';
    if (match) anyVisible = true;
  });
  dd.querySelectorAll('.dropdown-station').forEach(el => {
    // Hide station header if all channels below are hidden
    let next = el.nextSibling;
    let stationVisible = false;
    while (next && !next.classList.contains('dropdown-station')) {
      if (next.style.display !== 'none') stationVisible = true;
      next = next.nextSibling;
    }
    el.style.display = stationVisible ? '' : 'none';
  });
  return anyVisible;
}

function initChannelSearch() {
  const inp  = document.getElementById('channel-search');
  const btn  = document.getElementById('channel-list-btn');
  const copy = document.getElementById('channel-copy-btn');
  const dd   = document.getElementById('channel-dropdown');

  copy.disabled = true;
  btn.classList.add('no-channel');
  document.getElementById('channel-label')?.classList.add('no-channel');

  copy.addEventListener('click', async () => {
    const path = _channelFolderPath(currentChannel || {});
    if (!path) {
      alert(I18n.t('channel.path_unavailable_alert'));
      return;
    }
    // Try modern clipboard API, fall back to execCommand
    let ok = false;
    try {
      await navigator.clipboard.writeText(path);
      ok = true;
    } catch {
      try {
        const ta = document.createElement('textarea');
        ta.value = path;
        ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
        document.body.appendChild(ta);
        ta.focus(); ta.select();
        ok = document.execCommand('copy');
        document.body.removeChild(ta);
      } catch { ok = false; }
    }
    if (ok) {
      const prev = copy.textContent;
      copy.textContent = '✓';
      setTimeout(() => { copy.textContent = prev; }, 1500);
    } else {
      prompt(I18n.t('channel.copy_manual_prompt'), path);
    }
  });
  let _ddHideTimer = null;

  function _showDropdown(resetFilter) {
    clearTimeout(_ddHideTimer);
    if (resetFilter) { inp.value = ''; }
    filterChannelDropdown(inp.value);
    dd.classList.remove('hidden');
  }

  function _scheduleHide() {
    _ddHideTimer = setTimeout(() => dd.classList.add('hidden'), 150);
  }

  // Button: always opens full list
  btn.addEventListener('mousedown', e => { e.preventDefault(); });
  btn.addEventListener('click', () => {
    if (!dd.classList.contains('hidden')) {
      dd.classList.add('hidden');
    } else {
      _showDropdown(true);
      inp.focus();
    }
  });

  // Input: open on focus/click and filter on type
  inp.addEventListener('focus', () => _showDropdown(false));
  inp.addEventListener('click', () => _showDropdown(false));
  inp.addEventListener('blur', _scheduleHide);
  btn.addEventListener('blur', _scheduleHide);
  inp.addEventListener('input', () => {
    filterChannelDropdown(inp.value);
    dd.classList.remove('hidden');
  });
}

function _channelFolderPath(ch) {
  // SMB-источники: всегда возвращаем UNC-путь (\\host\share\path),
  // даже если шара смонтирована локально — путь к точке монтирования
  // специфичен для сервера и бесполезен пользователю.
  if (ch.smb) {
    const parts = [ch.smb.host, ch.smb.share];
    if (ch.smb.path) parts.push(ch.smb.path);
    return '\\\\' + parts.join('\\');
  }
  // Только для каналов с явным local_path (без SMB) возвращаем локальный путь.
  if (ch.local_path) return ch.local_path;
  return '';
}

function selectChannel(ch, st) {
  currentChannel = ch;
  const inp  = document.getElementById('channel-search');
  const lbl  = document.getElementById('channel-label');
  const copy = document.getElementById('channel-copy-btn');
  inp.value = '';
  inp.placeholder = I18n.t('channel.filter_placeholder');
  lbl.removeAttribute('data-i18n');
  lbl.textContent = ch.name;
  lbl.title = I18n.t('channel.dblclick_copy_title');
  lbl.ondblclick = () => {
    navigator.clipboard.writeText(ch.name).then(() => {
      const prev = lbl.textContent;
      lbl.textContent = I18n.t('channel.copied');
      setTimeout(() => { lbl.textContent = prev; }, 1200);
    });
  };
  const folderPath = _channelFolderPath(ch);
  copy.title = folderPath
    ? I18n.t('channel.copy_path_title_fmt', { path: folderPath })
    : I18n.t('channel.copy_path_unavailable');
  copy.disabled = !folderPath;

  // Mark active
  document.querySelectorAll('.dropdown-channel').forEach(el => {
    el.classList.toggle('active', el.dataset.id === ch.id);
  });
  document.getElementById('channel-dropdown').classList.add('hidden');
  document.getElementById('channel-list-btn').classList.remove('no-channel');
  document.getElementById('channel-label').classList.remove('no-channel');

  const wasPlaying = isPlaying;
  if (wasPlaying) stopPlay();

  Timeline.setChannel(ch.id);
  loadPlaylist();
  _updateBrandLink();
  _scheduleUiStateSave();

  if (wasPlaying) startPlay();
}

// ── Playlist ───────────────────────────────────────────────────────────────
let _playlistDebounce = null;
function refreshPlaylistForVisible() {
  clearTimeout(_playlistDebounce);
  _playlistDebounce = setTimeout(loadPlaylist, 400);
}

async function loadPlaylist() {
  if (!currentChannel) return;
  const ct = Timeline.getCenterTime();
  const start = ct - 3 * 3600;
  const end   = ct + 3 * 3600;
  try {
    const entries = await api(`/api/playlist/${currentChannel.id}?start=${start}&end=${end}`);
    playlistEntries = entries;
    renderPlaylist(entries);
  } catch (e) {
    // Offline or no playlist configured
  }
}

function renderPlaylist(entries) {
  const list = document.getElementById('playlist-list');
  list.innerHTML = '';
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    const d = new Date(e.timestamp * 1000);
    const timeStr = _fmt2(d.getHours()) + ':' + _fmt2(d.getMinutes()) + ':' + _fmt2(d.getSeconds());

    const row = document.createElement('div');
    row.className = 'playlist-row';
    if (e.color) row.style.background = _hexToRgba(e.color, 0.25);

    const cls = document.createElement('span');
    cls.className = 'pl-cls';
    cls.textContent = e.cls;
    cls.style.background = e.color || '#555';
    cls.style.color = _contrastColor(e.color || '#555');

    const time = document.createElement('span');
    time.className = 'pl-time';
    time.textContent = timeStr;

    const title = document.createElement('span');
    title.className = 'pl-title';
    title.textContent = e.title;
    if (e.elem_id) title.title = e.elem_id;

    const addBtn = document.createElement('button');
    addBtn.className = 'pl-add-btn';
    addBtn.textContent = '↑';
    addBtn.title = I18n.t('playlist.select_title');
    addBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      const next     = entries[i + 1];
      const dur      = e.duration ?? (next ? next.timestamp - e.timestamp : 180);
      const itemEnd  = e.timestamp + dur;

      if (ev.shiftKey) {
        // Shift: extend current selection to include this item
        const { start: curS, end: curE } = Timeline.getSelection();
        if (curS === null && curE === null) {
          // No current selection — behave like normal click
          Timeline.setSelStart(e.timestamp);
          Timeline.setSelEnd(itemEnd);
        } else {
          const curStart = curS ?? curE;
          const curEnd   = curE ?? curS;
          if (e.timestamp >= curStart) {
            // Item is after (or within) current selection → extend end
            Timeline.setSelStart(curStart);
            Timeline.setSelEnd(Math.max(curEnd, itemEnd));
          } else {
            // Item is before current selection → extend start
            Timeline.setSelStart(e.timestamp);
            Timeline.setSelEnd(curEnd);
          }
        }
      } else {
        Timeline.setSelStart(e.timestamp);
        Timeline.setSelEnd(itemEnd);
      }
    });

    row.appendChild(time);
    row.appendChild(cls);
    row.appendChild(title);
    row.appendChild(addBtn);

    // Click → jump to time
    row.addEventListener('click', () => {
      Timeline.setTime(e.timestamp);
    });

    list.appendChild(row);
  }
  _updateCurrentEntry();
}

let _highlightTimer = null;
function _scheduleHighlight() {
  clearTimeout(_highlightTimer);
  _highlightTimer = setTimeout(_updateCurrentEntry, 80);
}

function _updateCurrentEntry() {
  const ts   = Timeline.getCenterTime();
  const list = document.getElementById('playlist-list');
  const rows = list.querySelectorAll('.playlist-row');
  if (!rows.length) return;

  // Find last entry with timestamp <= ts
  let idx = -1;
  for (let i = 0; i < playlistEntries.length; i++) {
    if (playlistEntries[i].timestamp <= ts) idx = i;
    else break;
  }

  rows.forEach((row, i) => row.classList.toggle('pl-current', i === idx));

  if (idx >= 0 && rows[idx]) {
    const row      = rows[idx];
    const rowRect  = row.getBoundingClientRect();
    const listRect = list.getBoundingClientRect();
    // row's position inside scroll content
    const relTop   = rowRect.top - listRect.top + list.scrollTop;
    list.scrollTop = Math.max(0, relTop - list.clientHeight / 3);
  }
}


// ── Transport ──────────────────────────────────────────────────────────────
function initTransport() {
  document.getElementById('btn-play').addEventListener('click', togglePlay);
  document.getElementById('btn-mark-in').addEventListener('click', () => Timeline.setSelStart());
  document.getElementById('btn-mark-out').addEventListener('click', () => Timeline.setSelEnd());
  document.getElementById('btn-clear-sel').addEventListener('click', () => Timeline.clearSelection());
  document.getElementById('btn-add-log').addEventListener('click', () => {
    const { start, end } = Timeline.getSelection();
    if (start === null || end === null) {
      alert(I18n.t('transport.no_selection_alert'));
      return;
    }
    if (!currentChannel) {
      alert(I18n.t('transport.no_channel_alert'));
      return;
    }
    const durSec = Math.abs(end - start);
    if (durSec > MAX_SEGMENT_SEC) {
      alert(I18n.t('transport.segment_too_large_alert'));
      return;
    }
    addLogItem({
      channel_id:   currentChannel.id,
      channel_name: currentChannel.name,
      start, end,
      label: '',
    });
  });

  document.getElementById('btn-now').addEventListener('click', () => {
    Timeline.setTime(Date.now() / 1000);
  });

  // When audio actually starts outputting sound — sync clock and clear loading
  audio.addEventListener('playing', () => {
    if (!isPlaying) return;
    playStartTime = Date.now();
    _seekPending  = false;
    _hidePlayLoading();
  });

  audio.addEventListener('ended',   () => stopPlay());
  audio.addEventListener('error',   () => { _seekPending = false; _hidePlayLoading(); });
}

function _showPlayLoading() {
  document.getElementById('play-loading').classList.remove('hidden');
}
function _hidePlayLoading() {
  document.getElementById('play-loading').classList.add('hidden');
}

function togglePlay() {
  if (isPlaying) { stopPlay(); return; }
  if (!currentChannel) return;
  startPlay();
}

function startPlay() {
  const ts = Timeline.getCenterTime();
  const { start: selS, end: selE } = Timeline.getSelection();
  // Always start from the current timeline position.
  // Use the selection end as the stop point only when the timeline is within
  // the selection; otherwise fall back to +1 hour.
  const startTs = ts;
  const endTs   = (selE !== null && ts <= selE) ? selE : startTs + 3600;

  isPlaying  = true;
  playFromTs = startTs;
  _seekPending = true;   // hold animation until audio fires 'playing'
  document.getElementById('btn-play').textContent = '⏸';
  _showPlayLoading();

  const url = `/api/audio/stream?channel=${currentChannel.id}&start=${startTs}&end=${endTs}&format=mp3&bitrate=192k`;
  audio.src = url;
  audio.play().catch(e => { console.warn('Playback failed:', e); stopPlay(); });
  _startPlayheadAnimation();
}

function stopPlay() {
  audio.pause();
  audio.src   = '';
  isPlaying   = false;
  _seekPending = false;
  document.getElementById('btn-play').textContent = '▶';
  cancelAnimationFrame(playAnimFrame);
  _hidePlayLoading();
}

function _startPlayheadAnimation() {
  function frame() {
    if (!isPlaying) return;
    if (!_seekPending) {
      // Don't advance while user is scrolling or audio is buffering
      const elapsed = (Date.now() - playStartTime) / 1000;
      _tlAnimUpdate = true;
      Timeline.setTime(playFromTs + elapsed);
      _tlAnimUpdate = false;
    }
    playAnimFrame = requestAnimationFrame(frame);
  }
  playAnimFrame = requestAnimationFrame(frame);
}

let _seekTimer = null;
function _debouncedSeek(ts) {
  // Immediately freeze animation so the user sees their scroll position
  _seekPending = true;
  clearTimeout(_seekTimer);
  _seekTimer = setTimeout(() => _seekPlayback(ts), 150);
}

function _seekPlayback(ts) {
  if (!isPlaying || !currentChannel) { _seekPending = false; return; }
  playFromTs = ts;
  _showPlayLoading();
  const { start: selS, end: selE } = Timeline.getSelection();
  const endTs = (selE !== null) ? selE : ts + 3600;
  const url = `/api/audio/stream?channel=${currentChannel.id}&start=${ts}&end=${endTs}&format=mp3&bitrate=192k`;
  audio.src = url;
  audio.play().catch(e => {
    console.warn('Seek failed:', e);
    _seekPending = false;
    _hidePlayLoading();
  });
}


const MAX_SEGMENT_SEC = 3 * 3600;  // 3 hours

function updateSelLabel(s, e) {
  const lbl  = document.getElementById('sel-label');
  const warn = document.getElementById('sel-warning');

  if (s === null && e === null) {
    lbl.innerHTML = '<span class="sel-empty">—</span>';
    warn.classList.add('hidden');
    return;
  }

  const durSec = (s !== null && e !== null) ? Math.abs(e - s) : null;

  const _block = ts => ts === null ? '' :
    `<div class="sel-block">
       <div class="sel-date">${_tsToDate(ts)}</div>
       <div class="sel-time">${_tsToHMS(ts)}</div>
     </div>`;

  const durHtml = durSec !== null
    ? `<div class="sel-dur">[${_secToDuration(durSec)}]</div>` : '';

  if (s !== null && e !== null) {
    lbl.innerHTML =
      _block(s) +
      '<div class="sel-arrow">→</div>' +
      _block(e) +
      durHtml;
  } else {
    lbl.innerHTML = _block(s ?? e) + durHtml;
  }

  if (durSec !== null && durSec > MAX_SEGMENT_SEC) {
    warn.textContent = I18n.t('sel.warning');
    warn.classList.remove('hidden');
  } else {
    warn.classList.add('hidden');
  }
}

function _tsToDate(ts) {
  const d = new Date(ts * 1000);
  return `${_fmt2(d.getDate())}.${_fmt2(d.getMonth() + 1)}.${d.getFullYear()}`;
}

function _tsToHMS(ts) {
  const d = new Date(ts * 1000);
  return `${_fmt2(d.getHours())}:${_fmt2(d.getMinutes())}:${_fmt2(d.getSeconds())}`;
}

function _secToDuration(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (h > 0) return `${h}:${_fmt2(m)}:${_fmt2(sec)}`;
  return `${m}:${_fmt2(sec)}`;
}

// ── UI state persistence (server-side, per-user) ──────────────────────────
let _uiSaveTimer = null;

function _scheduleUiStateSave() {
  clearTimeout(_uiSaveTimer);
  _uiSaveTimer = setTimeout(_doSaveUiState, 1500);
}

async function _doSaveUiState() {
  const sel = Timeline.getSelection();
  try {
    await fetch('/api/ui-state', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channel_id:    currentChannel?.id ?? null,
        timeline_time: Timeline.getCenterTime(),
        sel_start:     sel.start,
        sel_end:       sel.end,
        log_items:     logItems,
      }),
    });
  } catch (_) { /* non-critical */ }
}

async function _fetchUiState() {
  try { return await api('/api/ui-state'); } catch(_) { return {}; }
}

// Save immediately on tab hide/close so timeline position is not lost
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    clearTimeout(_uiSaveTimer);
    _doSaveUiState();
  }
});

// ── Log-list persistence ───────────────────────────────────────────────────
function _saveLogItems() {
  _scheduleUiStateSave();
}

// ── Log-list ───────────────────────────────────────────────────────────────
function initLogList() {
  document.getElementById('btn-clear-log').addEventListener('click', () => {
    logItems = [];
    _saveLogItems();
    renderLogList();
  });
  document.getElementById('btn-export-all').addEventListener('click', exportAll);
}

function addLogItem(item) {
  const id = 'log_' + Date.now() + '_' + Math.random().toString(36).slice(2);
  logItems.push({ id, ...item });
  _saveLogItems();
  renderLogList();
}

function renderLogList() {
  _updateTabBadge();
  const container = document.getElementById('loglist-items');
  container.innerHTML = '';
  if (logItems.length === 0) {
    const hint = document.createElement('div');
    hint.className = 'loglist-empty-hint';
    hint.innerHTML = I18n.t('panel.segments_empty_hint');
    container.appendChild(hint);
    return;
  }
  logItems.forEach(item => {
    const row = document.createElement('div');
    row.className = 'log-row';

    const info = document.createElement('div');
    info.className = 'log-info';

    const ch = document.createElement('div');
    ch.className = 'log-channel';
    ch.textContent = item.channel_name + (item.label ? ` — ${item.label}` : '');

    const times = document.createElement('div');
    times.className = 'log-times';
    const sd = new Date(item.start * 1000);
    const ed = new Date(item.end * 1000);
    const fmt = d => `${d.getDate().toString().padStart(2,'0')}.${(d.getMonth()+1).toString().padStart(2,'0')}.${d.getFullYear()} ${_fmt2(d.getHours())}:${_fmt2(d.getMinutes())}:${_fmt2(d.getSeconds())}`;
    times.textContent = `${fmt(sd)}  →  ${fmt(ed)}`;

    info.appendChild(ch);
    info.appendChild(times);

    const actions = document.createElement('div');
    actions.className = 'log-actions';

    // Download button
    const dlBtn = document.createElement('button');
    dlBtn.className = 'log-btn log-btn-dl';
    dlBtn.textContent = '↓';
    dlBtn.title = I18n.t('log.dl_title');
    dlBtn.addEventListener('click', () => openExportModal(item));

    // Navigate button
    const navBtn = document.createElement('button');
    navBtn.className = 'log-btn log-btn-nav';
    navBtn.textContent = '↗';
    navBtn.title = I18n.t('log.nav_title');
    navBtn.addEventListener('click', () => {
      // Switch to the channel the fragment was recorded on (if it differs from current)
      if (!currentChannel || currentChannel.id !== item.channel_id) {
        for (const st of stations) {
          const ch = st.channels.find(c => c.id === item.channel_id);
          if (ch) { selectChannel(ch, st); break; }
        }
      }
      Timeline.setTime(item.start);
      // Also restore selection
      Timeline.setSelStart(item.start);
      Timeline.setSelEnd(item.end);
    });

    // Delete button
    const delBtn = document.createElement('button');
    delBtn.className = 'log-btn log-btn-del';
    delBtn.textContent = '✕';
    delBtn.title = I18n.t('log.del_title');
    delBtn.addEventListener('click', () => {
      logItems = logItems.filter(i => i.id !== item.id);
      _saveLogItems();
      renderLogList();
      Timeline.clearSelection();
    });

    actions.appendChild(dlBtn);
    actions.appendChild(navBtn);
    actions.appendChild(delBtn);

    row.appendChild(info);
    row.appendChild(actions);
    container.appendChild(row);
  });
}

// ── Export modal ───────────────────────────────────────────────────────────
function _getChannelById(id) {
  for (const st of stations) {
    const ch = st.channels.find(c => c.id === id);
    if (ch) return ch;
  }
  return null;
}

function initExportModal() {
  document.getElementById('exp-cancel').addEventListener('click', () => {
    document.getElementById('export-modal').classList.add('hidden');
  });
  document.getElementById('exp-ok').addEventListener('click', doExport);
  document.getElementById('exp-format').addEventListener('change', _updateExportFields);
}

function _updateExportFields() {
  const fmt        = document.getElementById('exp-format').value;
  const bitrateRow = document.getElementById('exp-bitrate-row');
  const wavNote    = document.getElementById('exp-wav-note');
  const copyNote   = document.getElementById('exp-copy-note');
  const srSel      = document.getElementById('exp-samplerate');
  const brSel      = document.getElementById('exp-bitrate');

  const ch = exportTarget ? _getChannelById(exportTarget.channel_id) : null;
  const nativeExt = ch ? ch.file_extension.toLowerCase() : null;
  const isCopy    = nativeExt && fmt === nativeExt;

  // Show/hide bitrate row
  if (fmt === 'wav') {
    bitrateRow.classList.add('hidden');
    wavNote.classList.remove('hidden');
  } else {
    bitrateRow.classList.remove('hidden');
    wavNote.classList.add('hidden');
  }

  // Show copy-mode note and grey out params when native format selected
  copyNote.classList.toggle('hidden', !isCopy);
  srSel.disabled = isCopy;
  brSel.disabled = isCopy;

  // Always keep native sample_rate selected when changing format
  if (ch && ch.sample_rate) {
    const opt = [...srSel.options].find(o => parseInt(o.value) === ch.sample_rate);
    if (opt) srSel.value = opt.value;
  }
}

function openExportModal(item) {
  exportTarget = item;
  document.getElementById('export-progress').classList.add('hidden');

  // Pre-select native channel format
  const ch = _getChannelById(item.channel_id);
  if (ch) {
    const fmtSel = document.getElementById('exp-format');
    const ext = ch.file_extension.toLowerCase();
    // Map extension to select option value
    const fmtMap = { mp3: 'mp3', wav: 'wav', aac: 'aac' };
    if (fmtMap[ext]) fmtSel.value = fmtMap[ext];

    // Pre-fill samplerate with channel's native rate
    const srSel = document.getElementById('exp-samplerate');
    const srOpt = [...srSel.options].find(o => parseInt(o.value) === ch.sample_rate);
    if (srOpt) srSel.value = srOpt.value;

    // Pre-fill bitrate with channel's detected bitrate (mp3/aac)
    if (ch.bitrate) {
      const brSel = document.getElementById('exp-bitrate');
      const brOpt = [...brSel.options].find(o => o.value === ch.bitrate);
      if (brOpt) brSel.value = brOpt.value;
    }
  }

  _updateExportFields();
  document.getElementById('export-modal').classList.remove('hidden');
}

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

async function doExport() {
  if (!exportTarget) return;
  const prog = document.getElementById('export-progress');
  prog.classList.remove('hidden');
  prog.textContent = I18n.t('export.in_progress');
  try {
    const result = await api('/api/audio/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_buildExportBody(exportTarget)),
    });
    prog.textContent = I18n.t('export.done');
    _triggerDownload(result.download_url, result.filename);
    setTimeout(() => document.getElementById('export-modal').classList.add('hidden'), 1200);
  } catch (e) {
    prog.textContent = I18n.t('export.error', { msg: e.message });
  }
}

async function exportAll() {
  if (logItems.length === 0) return;
  const total = logItems.length;
  const el = document.getElementById('play-loading');
  let failed = 0;
  for (let i = 0; i < total; i++) {
    el.textContent = I18n.t('export.all_progress', { n: i + 1, total });
    el.classList.remove('hidden');
    const ok = await doExportItem(logItems[i]);
    if (!ok) failed++;
  }
  el.textContent = failed
    ? I18n.t('export.all_errors', { ok: total - failed, total, failed })
    : I18n.t('export.all_done', { total });
  setTimeout(() => el.classList.add('hidden'), 3000);
}

async function doExportItem(item) {
  try {
    const fmt        = document.getElementById('exp-format').value;
    const bitrate    = document.getElementById('exp-bitrate').value;
    const samplerate = parseInt(document.getElementById('exp-samplerate').value, 10);
    const result = await api('/api/audio/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channel_id:  item.channel_id,
        start:       item.start,
        end:         item.end,
        format:      fmt,
        bitrate:     fmt === 'wav' ? null : bitrate,
        sample_rate: samplerate,
      }),
    });
    _triggerDownload(result.download_url, result.filename);
    await new Promise(r => setTimeout(r, 500));
    return true;
  } catch (e) {
    console.error('Export failed for', item, e);
    return false;
  }
}

function _triggerDownload(url, filename) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ── Clock controls ─────────────────────────────────────────────────────────
// Left-click = increment, right-click = decrement the corresponding time unit.
function initClockControls() {
  const parts = [
    { id: 'clk-day',   unit: 'day'    },
    { id: 'clk-month', unit: 'month'  },
    { id: 'clk-year',  unit: 'year'   },
    { id: 'clk-h',     unit: 'hour'   },
    { id: 'clk-m',     unit: 'minute' },
    { id: 'clk-s',     unit: 'second' },
  ];

  parts.forEach(({ id, unit }) => {
    const el = document.getElementById(id);
    if (!el) return;

    el.addEventListener('click', e => {
      e.preventDefault();
      _adjustTime(unit, +1);
    });

    el.addEventListener('contextmenu', e => {
      e.preventDefault();
      _adjustTime(unit, -1);
    });

    el.style.cursor = 'pointer';
    el.title = I18n.t('clock.adjust_title');
  });
}

function _adjustTime(unit, dir) {
  const d = new Date(Timeline.getCenterTime() * 1000);
  switch (unit) {
    case 'second': d.setSeconds(d.getSeconds() + dir);     break;
    case 'minute': d.setMinutes(d.getMinutes() + dir);     break;
    case 'hour':   d.setHours(d.getHours()     + dir);     break;
    case 'day':    d.setDate(d.getDate()        + dir);     break;
    case 'month':  d.setMonth(d.getMonth()      + dir);     break;
    case 'year':   d.setFullYear(d.getFullYear()+ dir);     break;
  }
  Timeline.setTime(d.getTime() / 1000);
  // Refresh availability for the new viewport position
  if (currentChannel) Timeline.setChannel(currentChannel.id);
}

// ── Mobile clock (date/time inputs) ────────────────────────────────────────
function _syncMobClock(ts) {
  const mobDate = document.getElementById('mob-date');
  const mobTime = document.getElementById('mob-time');
  if (!mobDate && !mobTime) return;
  const d = new Date(ts * 1000);
  const y   = d.getFullYear();
  const mo  = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const h   = String(d.getHours()).padStart(2, '0');
  const mi  = String(d.getMinutes()).padStart(2, '0');
  const s   = String(d.getSeconds()).padStart(2, '0');
  if (mobDate) mobDate.value = `${y}-${mo}-${day}`;
  if (mobTime) mobTime.value = `${h}:${mi}:${s}`;
}

function initMobClock() {
  const mobDate  = document.getElementById('mob-date');
  const mobTime  = document.getElementById('mob-time');
  const mobNow   = document.getElementById('mob-btn-now');

  function _applyMobDateTime() {
    const dv = mobDate?.value; // "YYYY-MM-DD"
    const tv = mobTime?.value; // "HH:MM" or "HH:MM:SS"
    if (!dv || !tv) return;
    const d = new Date(`${dv}T${tv}`);
    if (isNaN(d)) return;
    Timeline.setTime(d.getTime() / 1000);
    if (currentChannel) Timeline.setChannel(currentChannel.id);
  }

  mobDate?.addEventListener('change', _applyMobDateTime);
  mobTime?.addEventListener('change', _applyMobDateTime);
  mobNow?.addEventListener('click', () => Timeline.setTime(Date.now() / 1000));
}

// ── Mobile panel tabs ───────────────────────────────────────────────────────
function initMobileTabs() {
  const tabs = document.getElementById('panel-tabs');
  if (!tabs) return;

  // Set initial state: show only the active tab's panel
  const activeTab = tabs.querySelector('.panel-tab.active');
  if (activeTab) {
    const target = activeTab.dataset.panel;
    document.querySelectorAll('#main-columns .panel').forEach(p => {
      p.classList.toggle('mob-hidden', p.id !== target);
    });
  }

  tabs.querySelectorAll('.panel-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.querySelectorAll('.panel-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const target = tab.dataset.panel;
      document.querySelectorAll('#main-columns .panel').forEach(p => {
        p.classList.toggle('mob-hidden', p.id !== target);
      });
    });
  });
}

function _updateTabBadge() {
  const badge = document.getElementById('tab-badge');
  if (!badge) return;
  const count = logItems.length;
  badge.textContent = count;
  badge.classList.toggle('hidden', count === 0);
}

// ── Hotkeys ────────────────────────────────────────────────────────────────
// 0        — Play / Pause
// [        — Mark In
// ]        — Mark Out
// S        — Focus channel search
// + / =    — Increment hovered timeline unit
// -        — Decrement hovered timeline unit
function initHotkeys() {
  const TIMELINE_UNITS = new Set(['second', 'minute', 'hour', 'day', 'month']);

  document.addEventListener('keydown', e => {
    const tag = document.activeElement && document.activeElement.tagName;
    const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

    if ((e.key === 's' || e.key === 'S') && !e.ctrlKey && !e.altKey && !e.metaKey && !isInput) {
      e.preventDefault();
      const inp = document.getElementById('channel-search');
      if (inp) { inp.focus(); inp.select(); }
      return;
    }

    if (isInput) return;

    switch (e.key) {
      case '0':
        e.preventDefault();
        togglePlay();
        break;

      case '[':
        e.preventDefault();
        Timeline.setSelStart();
        break;

      case ']':
        e.preventDefault();
        Timeline.setSelEnd();
        break;

      case '+':
      case '=': {
        e.preventDefault();
        const unit = Timeline.getHoveredUnit();
        if (unit && TIMELINE_UNITS.has(unit)) _adjustTime(unit, +1);
        break;
      }

      case '-': {
        e.preventDefault();
        const unit = Timeline.getHoveredUnit();
        if (unit && TIMELINE_UNITS.has(unit)) _adjustTime(unit, -1);
        break;
      }
    }
  });
}

// ── Braille spinner ────────────────────────────────────────────────────────
const _SPINNER_FRAMES = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏'];
const _spinnerTargets = new Map(); // element → intervalId

function _spinnerStart(el) {
  if (_spinnerTargets.has(el)) return;          // already spinning
  let i = 0;
  el.textContent = _SPINNER_FRAMES[0];
  const id = setInterval(() => {
    i = (i + 1) % _SPINNER_FRAMES.length;
    el.textContent = _SPINNER_FRAMES[i];
  }, 80);
  _spinnerTargets.set(el, id);
}

function _spinnerStop(el) {
  const id = _spinnerTargets.get(el);
  if (id !== undefined) { clearInterval(id); _spinnerTargets.delete(el); }
}

function _spinnerStopAll() {
  _spinnerTargets.forEach(id => clearInterval(id));
  _spinnerTargets.clear();
}

// ── Index status bar ───────────────────────────────────────────────────────
let _statusChannels = [];   // [{id, name, files, done, failed, rescanning}]
// [{pl_id, pl_name, priority, ok, error, checking}]
let _plSources = [];

function initStatusBar() {
  document.getElementById('status-close').addEventListener('click', () => {
    document.getElementById('status-bar').classList.add('hidden');
  });
  document.getElementById('plbar-close').addEventListener('click', () => {
    document.getElementById('playlog-bar').classList.add('hidden');
  });

  // Fetch current state (handles page reload mid-scan or after scan)
  fetch('/api/index_status')
    .then(r => r.json())
    .then(data => _applyIndexStatus(data))
    .catch(() => _setStatus('error', '⚠', I18n.t('status.init')));

  // Fetch last playlog check result (may be empty if check not done yet)
  fetch('/api/playlog_status')
    .then(r => r.json())
    .then(data => { if (data.length) _applyPlaylogStatus(data); })
    .catch(() => {});
}

function _applyIndexStatus(data) {
  _statusChannels = data.channels || [];
  _rebuildDots();

  if (data.status === 'ready' || data.status === 'idle') {
    const failedN   = _statusChannels.filter(c => c.failed).length;
    const failedStr = failedN ? I18n.t('status.unavailable_count', { n: failedN }) : '';
    _setStatus('ready', '✓',
      I18n.t('status.index_ready', { files: data.total_files, channels: data.total_channels, failed: failedStr }));
    if (failedN) _makeDiagLink();
  } else if (data.status === 'scanning') {
    const done  = data.done_channels;
    const total = data.total_channels;
    const active = _statusChannels.find(c => !c.done);
    const label  = active ? ` · ${_displayName(active.name)}` : '';
    _setStatus('scanning', '',
      I18n.t('status.index_scanning', { done, total, label }));
  }
}

function _handleCacheLoaded(msg) {
  _setStatus('scanning', '📦',
    I18n.t('status.cache_loaded', { files: msg.total_files }));
  // Index just became available from cache — re-fetch without clearing existing data
  if (currentChannel) Timeline.refreshAvailability();
}

function _handleIndexScanning(msg) {
  _statusChannels.forEach(c => { c.rescanning = (c.id === msg.channel_id); });
  _rebuildDots();
  _setStatus('scanning', '',
    I18n.t('status.index_scanning', { done: msg.done, total: msg.total, label: ` · ${_displayName(msg.channel_name)}` }));
}

function _displayName(fullName) {
  // Normalize em-dash to regular dash for display
  return fullName.replace(/\s*—\s*/g, ' - ');
}

function _handleIndexProgress(msg) {
  const ch = _statusChannels.find(c => c.id === msg.channel_id);
  if (ch) { ch.files = msg.files; ch.done = true; ch.failed = false; ch.rescanning = false; }
  _rebuildDots();
  // If this is the currently selected channel, re-fetch availability without clearing
  if (currentChannel && currentChannel.id === msg.channel_id) Timeline.refreshAvailability();

  if (msg.rescan && msg.done === 1) {
    _setStatus('ready', '✓',
      I18n.t('status.rescan_done', { name: _displayName(msg.channel_name), files: msg.files }));
    if (currentChannel && currentChannel.id === msg.channel_id) {
      Timeline.setChannel(currentChannel.id);
    }
    return;
  }

  const nextCh = _statusChannels.find(c => !c.done);
  const label  = nextCh ? ` · ${_displayName(nextCh.name)}` : '';
  _setStatus('scanning', '',
    I18n.t('status.index_scanning', { done: msg.done, total: msg.total, label }),
    I18n.t('status.detail_done', { name: _displayName(msg.channel_name), files: msg.files }));
}

function _handleIndexError(msg) {
  const ch = _statusChannels.find(c => c.id === msg.channel_id);
  if (ch) { ch.done = true; ch.failed = true; ch.files = 0; ch.rescanning = false; ch.error = msg.error || ''; }
  _rebuildDots();

  const nextCh = _statusChannels.find(c => !c.done);
  const label  = nextCh ? ` · ${_displayName(nextCh.name)}` : '';
  _setStatus('scanning', '',
    I18n.t('status.index_scanning', { done: msg.done, total: msg.total, label }),
    I18n.t('status.detail_unavailable', { name: _displayName(msg.channel_name) }));
}

function _handleIndexDone(msg) {
  _statusChannels.forEach(c => { c.rescanning = false; if (!c.done) c.done = true; });
  _rebuildDots();
  // Re-fetch availability now that all scanning is done (without clearing existing data)
  if (currentChannel) Timeline.refreshAvailability();
  const failedN   = _statusChannels.filter(c => c.failed).length;
  const failedStr = failedN ? I18n.t('status.unavailable_count', { n: failedN }) : '';
  _setStatus('ready', '✓',
    I18n.t('status.index_ready', { files: msg.total_files, channels: msg.channels, failed: failedStr }));
  if (failedN) _makeDiagLink();
}

// ── Playlog status bar ─────────────────────────────────────────────────────

function _handlePlaylogChecking() {
  const bar = document.getElementById('playlog-bar');
  bar.classList.remove('hidden');
  _plSources = _plSources.map(s => ({ ...s, checking: true }));
  if (!_plSources.length) {
    _spinnerStart(document.getElementById('plbar-icon'));
    document.getElementById('plbar-text').textContent   = I18n.t('playlog.checking');
    document.getElementById('plbar-detail').textContent = '';
  }
  _rebuildPlDots();
}

function _applyPlaylogStatus(playlogs) {
  _plSources = [];
  playlogs.forEach(pl => {
    pl.sources.forEach(src => {
      _plSources.push({
        pl_id:    pl.id,
        pl_name:  pl.name,
        priority: src.priority,
        ok:       src.ok,
        error:    src.error || '',
        checking: false,
      });
    });
  });
  _rebuildPlDots();

  const total  = _plSources.length;
  const failed = _plSources.filter(s => !s.ok).length;
  const bar    = document.getElementById('playlog-bar');
  bar.classList.remove('hidden');

  if (total === 0) {
    bar.classList.add('hidden');
    return;
  }
  const plbarIcon = document.getElementById('plbar-icon');
  _spinnerStop(plbarIcon);
  if (failed === 0) {
    plbarIcon.textContent = '✓';
    document.getElementById('plbar-text').textContent   = I18n.t('playlog.available', { n: total });
    document.getElementById('plbar-detail').textContent = '';
    // Auto-clear text after 5s (bar stays, dots remain)
    setTimeout(() => {
      plbarIcon.textContent = '';
      document.getElementById('plbar-text').textContent   = '';
      document.getElementById('plbar-detail').textContent = '';
    }, 5000);
  } else {
    plbarIcon.textContent = '⚠';
    document.getElementById('plbar-text').textContent   =
      I18n.t('playlog.unavailable', { failed, total });
    document.getElementById('plbar-detail').textContent = '';
  }
}

function _rebuildPlDots() {
  const bar = document.getElementById('playlog-bar');
  const old = document.getElementById('plbar-dots');
  if (old) old.remove();
  if (!_plSources.length) return;

  const dots = document.createElement('span');
  dots.id = 'plbar-dots';
  dots.className = 'status-dots';

  _plSources.forEach(s => {
    const dot = document.createElement('span');
    if (s.checking) dot.className = 'status-dot rescanning';
    else if (s.ok)  dot.className = 'status-dot done';
    else            dot.className = 'status-dot failed';

    const label = `${_displayName(s.pl_name)} (${I18n.t('dot.pl_priority', { n: s.priority })})`;
    dot.title = s.ok       ? `${label}: ${I18n.t('dot.available')}`
              : s.checking ? `${label}: ${I18n.t('dot.checking')}`
              : `${label}: ${I18n.t('dot.unavailable_click')}`;

    if (!s.ok && !s.checking) {
      dot.style.cursor = 'pointer';
      dot.addEventListener('click', e => {
        e.stopPropagation();
        _showDiagPopover(dot, [{ name: label, error: s.error }]);
      });
    }
    dots.appendChild(dot);
  });

  bar.insertBefore(dots, document.getElementById('plbar-close'));
}

// ── Audio index status helpers ─────────────────────────────────────────────

let _statusHideTimer = null;

function _setStatus(cls, icon, text, detail = '') {
  clearTimeout(_statusHideTimer);
  const bar     = document.getElementById('status-bar');
  const iconEl  = document.getElementById('status-icon');
  bar.classList.remove('status-scanning', 'status-ready', 'status-error', 'hidden');
  bar.classList.add(`status-${cls}`);
  _spinnerStop(iconEl);
  if (cls === 'scanning') {
    _spinnerStart(iconEl);
  } else {
    iconEl.textContent = icon;
  }
  document.getElementById('status-text').textContent   = text;
  document.getElementById('status-detail').textContent = detail;
  // Auto-clear informational 'ready' messages after 5 seconds (bar stays visible)
  if (cls === 'ready') {
    _statusHideTimer = setTimeout(() => {
      iconEl.textContent = '';
      document.getElementById('status-text').textContent   = '';
      document.getElementById('status-detail').textContent = '';
    }, 5000);
  }
}

function _rebuildDots() {
  const old = document.getElementById('status-dots');
  if (old) old.remove();
  if (_statusChannels.length === 0) return;

  const bar  = document.getElementById('status-bar');
  const dots = document.createElement('span');
  dots.id = 'status-dots';
  dots.className = 'status-dots';

  _statusChannels.forEach(c => {
    const dot = document.createElement('span');
    const empty = c.done && !c.failed && !c.rescanning && c.files === 0;
    let cls = 'status-dot';
    if (c.rescanning)  cls += ' rescanning';
    else if (c.failed) cls += ' failed';
    else if (empty)    cls += ' empty';
    else if (c.done)   cls += ' done';
    dot.className = cls;
    dot.title = c.failed     ? `${c.name}: ${I18n.t('dot.unavailable_click')}`
              : c.rescanning ? `${c.name}: ${I18n.t('dot.updating')}`
              : empty        ? `${c.name}: ${I18n.t('dot.files_empty')}`
              : `${c.name}: ${I18n.t('dot.files', { n: c.files })}`;
    if (c.failed || empty) {
      dot.style.cursor = 'pointer';
      dot.addEventListener('click', e => {
        e.stopPropagation();
        _showDiagPopover(dot, [{ name: c.name, error: c.error || I18n.t('dot.files_empty') }]);
      });
    }
    dots.appendChild(dot);
  });

  bar.insertBefore(dots, document.getElementById('status-close'));
}

function _makeDiagLink() {
  const textEl = document.getElementById('status-text');
  if (!textEl) return;
  const html = textEl.textContent;
  // Match "⚠ N unavailable/недоступн./indisponible(s)" pattern
  const match = html.match(/(⚠\s*\d+\s*\S+\.?)/);
  if (!match) return;
  textEl.innerHTML = html.replace(match[0],
    `<span class="diag-link" style="cursor:pointer;text-decoration:underline dotted">${match[0]}</span>`);
  textEl.querySelector('.diag-link').addEventListener('click', e => {
    e.stopPropagation();
    const failed = _statusChannels.filter(c => c.failed);
    _showDiagPopover(textEl, failed);
  });
}

function _showDiagPopover(anchor, channels) {
  const pop = document.getElementById('diag-popover');
  const list = document.getElementById('diag-list');
  list.innerHTML = '';
  channels.forEach(c => {
    const item = document.createElement('div');
    item.className = 'diag-item';
    const name = document.createElement('div');
    name.className = 'diag-channel-name';
    name.textContent = c.name;
    item.appendChild(name);
    const err = document.createElement('div');
    if (c.error) {
      err.className = 'diag-error';
      err.textContent = c.error;
    } else {
      err.className = 'diag-no-error';
      err.textContent = I18n.t('diag.unknown_reason');
    }
    item.appendChild(err);
    if (currentUser?.is_admin && c.id) {
      const btn = document.createElement('button');
      btn.className = 'diag-rescan-btn';
      btn.textContent = I18n.t('diag.rescan_btn');
      btn.addEventListener('click', async e => {
        e.stopPropagation();
        btn.disabled = true;
        btn.textContent = I18n.t('diag.rescan_btn_running');
        try {
          await api(`/api/rescan/${c.id}`, { method: 'POST' });
        } catch (ex) {
          btn.disabled = false;
          btn.textContent = I18n.t('diag.rescan_btn');
        }
      });
      item.appendChild(btn);
    }
    list.appendChild(item);
  });

  // Position below the anchor
  pop.classList.remove('hidden');
  const rect = anchor.getBoundingClientRect();
  const pw = pop.offsetWidth || 340;
  let left = rect.left;
  if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
  pop.style.left = left + 'px';
  pop.style.top  = (rect.bottom + 6) + 'px';
}

// Close popover on outside click
document.addEventListener('click', () => {
  document.getElementById('diag-popover')?.classList.add('hidden');
});

// ── WebSocket (real-time availability updates) ─────────────────────────────
function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.addEventListener('message', ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'availability_update' && currentChannel && msg.channel_id === currentChannel.id) {
      Timeline.addAvailability(msg.added, msg.removed);
    } else if (msg.type === 'config_reloaded') {
      loadStations().then(buildChannelDropdown);
    } else if (msg.type === 'cache_loaded') {
      _handleCacheLoaded(msg);
    } else if (msg.type === 'index_scanning') {
      _handleIndexScanning(msg);
    } else if (msg.type === 'index_progress') {
      _handleIndexProgress(msg);
    } else if (msg.type === 'index_error') {
      _handleIndexError(msg);
    } else if (msg.type === 'index_done') {
      _handleIndexDone(msg);
    } else if (msg.type === 'playlog_checking') {
      _handlePlaylogChecking();
    } else if (msg.type === 'playlog_status') {
      _applyPlaylogStatus(msg.playlogs);
    }
  });

  ws.addEventListener('close', () => {
    setTimeout(connectWebSocket, 3000);
  });
}

// ── Hamburger menu ─────────────────────────────────────────────────────────
function initHamburgerMenu() {
  const btn  = document.getElementById('btn-hamburger');
  const menu = document.getElementById('hamburger-menu');

  btn.addEventListener('click', e => {
    e.stopPropagation();
    menu.classList.toggle('hidden');
  });

  // Close on outside click
  document.addEventListener('click', () => menu.classList.add('hidden'));
  menu.addEventListener('click', e => e.stopPropagation());

  // ── Auto theme ─────────────────────────────────────────────────────────
  // Light: 09:00–18:00  |  Dark: 18:00–09:00
  // Manual override persists until the next scheduled transition,
  // then auto theme resumes automatically.

  function _autoTheme() {
    const h = new Date().getHours();
    return (h >= 9 && h < 18) ? 'light' : 'dark';
  }

  function _nextTransition() {
    const now  = new Date();
    const next = new Date(now);
    next.setSeconds(0);
    next.setMilliseconds(0);
    const h = now.getHours();
    if (h >= 9 && h < 18) {
      next.setHours(18, 0);               // light → dark at 18:00
    } else if (h < 9) {
      next.setHours(9, 0);                // dark  → light at 09:00 today
    } else {
      next.setDate(next.getDate() + 1);
      next.setHours(9, 0);               // dark  → light at 09:00 tomorrow
    }
    return next.getTime();
  }

  function _getEffectiveTheme() {
    try {
      const obj = JSON.parse(localStorage.getItem('apricot-theme') || 'null');
      if (obj && obj.until && Date.now() < obj.until) return obj.value;
    } catch (e) {}
    return _autoTheme();
  }

  function _applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const btn = document.getElementById('menu-theme');
    if (btn) btn.textContent = theme === 'light' ? I18n.t('menu.theme_dark') : I18n.t('menu.theme_light');
    Timeline.drawAll();
  }

  function _scheduleThemeTransition() {
    const delay = Math.max(_nextTransition() - Date.now(), 1000);
    setTimeout(() => {
      // If manual override is still valid, wait for the next boundary
      try {
        const obj = JSON.parse(localStorage.getItem('apricot-theme') || 'null');
        if (obj && obj.until && Date.now() < obj.until) {
          _scheduleThemeTransition();
          return;
        }
      } catch (e) {}
      _applyTheme(_autoTheme());
      _scheduleThemeTransition();
    }, delay);
  }

  // Theme toggle (manual)
  const themeBtn = document.getElementById('menu-theme');
  themeBtn.addEventListener('click', () => {
    const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
    const newTheme = isDark ? 'light' : 'dark';
    localStorage.setItem('apricot-theme', JSON.stringify({ value: newTheme, until: _nextTransition() }));
    _applyTheme(newTheme);
    menu.classList.add('hidden');
  });

  // Apply theme on load and schedule auto-transitions
  _applyTheme(_getEffectiveTheme());
  _scheduleThemeTransition();

  // Language buttons
  document.querySelectorAll('.menu-lang-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.lang === I18n.getLang());
    btn.addEventListener('click', async () => {
      await I18n.setLang(btn.dataset.lang);
      // Re-apply dynamic texts that aren't data-i18n attributes
      _reapplyDynamicTexts();
    });
  });

  // Rescan current channel
  document.getElementById('menu-rescan').addEventListener('click', async () => {
    menu.classList.add('hidden');
    if (!currentChannel) {
      _setStatus('error', '⚠', I18n.t('status.no_channel'));
      return;
    }
    _setStatus('scanning', '', I18n.t('status.rescanning', { name: _displayName(currentChannel.name) }));
    try {
      await api(`/api/rescan/${currentChannel.id}`, { method: 'POST' });
      // Result arrives via WebSocket (index_progress / index_error)
    } catch (e) {
      _setStatus('error', '⚠', I18n.t('status.rescan_error', { msg: e.message }));
    }
  });

  // Rescan playlogs for current station
  document.getElementById('menu-rescan-playlogs').addEventListener('click', async () => {
    menu.classList.add('hidden');
    if (!currentChannel) {
      document.getElementById('plbar-icon').textContent = '⚠';
      document.getElementById('plbar-text').textContent = I18n.t('playlog.no_channel');
      document.getElementById('playlog-bar').classList.remove('hidden');
      return;
    }
    _spinnerStart(document.getElementById('plbar-icon'));
    document.getElementById('plbar-text').textContent   = I18n.t('playlog.rescanning');
    document.getElementById('plbar-detail').textContent = '';
    document.getElementById('playlog-bar').classList.remove('hidden');
    try {
      await api(`/api/rescan_playlogs/${currentChannel.id}`, { method: 'POST' });
      // Result arrives via WS (playlog_checking → playlog_status)
    } catch (e) {
      document.getElementById('plbar-icon').textContent = '⚠';
      document.getElementById('plbar-text').textContent = I18n.t('playlog.rescan_error', { msg: e.message });
    }
  });

  // Reload config
  document.getElementById('menu-reload').addEventListener('click', async () => {
    menu.classList.add('hidden');
    _setStatus('scanning', '', I18n.t('status.reload_config'));
    try {
      const res = await api('/api/reload', { method: 'POST' });
      await loadStations();
      buildChannelDropdown();
      _setStatus('ready', '✓',
        I18n.t('status.config_reloaded', { stations: res.stations, channels: res.channels, playlogs: res.playlogs }));
    } catch (e) {
      _setStatus('error', '⚠', I18n.t('status.config_error', { msg: e.message }));
    }
  });

  document.getElementById('menu-restart').addEventListener('click', async () => {
    menu.classList.add('hidden');
    if (!confirm(I18n.t('status.restart_confirm'))) return;
    try {
      await api('/api/restart', { method: 'POST' });
    } catch { /* server going down — expected */ }
    _setStatus('scanning', '', I18n.t('status.restarting'));
    // Poll until server is back, then reload
    const _tryReload = () => {
      fetch('/api/version').then(r => {
        if (r.ok) window.location.reload();
        else setTimeout(_tryReload, 1000);
      }).catch(() => setTimeout(_tryReload, 1000));
    };
    setTimeout(_tryReload, 1500);
  });

  // Check updates
  document.getElementById('menu-check-updates').addEventListener('click', async () => {
    menu.classList.add('hidden');
    _setStatus('scanning', '', I18n.t('update.checking'));
    let data;
    try {
      data = await api('/api/check_updates');
    } catch (e) {
      _setStatus('error', '', I18n.t('update.error', { msg: e.message ?? String(e) }));
      return;
    }
    _setStatus('ok', '');
    _showUpdateModal(data);
  });

  // Sessions
  document.getElementById('menu-sessions').addEventListener('click', () => {
    menu.classList.add('hidden');
    _openSessionsModal();
  });

  // Sessions modal buttons
  document.getElementById('sessions-refresh').addEventListener('click', _loadSessions);
  document.getElementById('sessions-close').addEventListener('click', () => {
    document.getElementById('sessions-modal').classList.add('hidden');
  });
  document.getElementById('sessions-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('sessions-modal'))
      document.getElementById('sessions-modal').classList.add('hidden');
  });

  // About
  document.getElementById('menu-about').addEventListener('click', () => {
    menu.classList.add('hidden');
    _showAboutModal();
  });

  // About modal — OK button and backdrop click
  document.getElementById('about-ok').addEventListener('click', () => {
    document.getElementById('about-modal').classList.add('hidden');
  });
  document.getElementById('about-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('about-modal'))
      document.getElementById('about-modal').classList.add('hidden');
  });

  // Update modal — OK button and backdrop click
  document.getElementById('update-ok-btn').addEventListener('click', () => {
    document.getElementById('update-modal').classList.add('hidden');
  });
  document.getElementById('update-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('update-modal'))
      document.getElementById('update-modal').classList.add('hidden');
  });

  // Logout
  document.getElementById('menu-logout').addEventListener('click', async () => {
    menu.classList.add('hidden');
    try { await fetch('/api/auth/logout', { method: 'POST' }); } catch { /* ok */ }
    location.href = '/login';
  });
}

function _showUpdateModal(data) {
  const modal   = document.getElementById('update-modal');
  const content = document.getElementById('update-content');
  const doBtn   = document.getElementById('update-do-btn');
  const okBtn   = document.getElementById('update-ok-btn');

  let html = '<h2>' + I18n.t('update.title') + '</h2>';
  if (data.up_to_date) {
    html += '<p>' + I18n.t('update.up_to_date') + '</p>';
    html += '<p class="about-version">' + I18n.t('update.local_commit',
      { sha: data.local.sha, date: data.local.date, msg: data.local.message }) + '</p>';
    doBtn.classList.add('hidden');
  } else {
    html += '<p>' + I18n.t('update.available') + '</p>';
    html += '<table class="update-table">';
    html += '<tr><th>' + I18n.t('update.col_version') + '</th><th>' +
            I18n.t('update.col_commit') + '</th><th>' + I18n.t('update.col_date') + '</th><th>' +
            I18n.t('update.col_message') + '</th></tr>';
    html += '<tr><td>' + I18n.t('update.row_local') + '</td><td><code>' +
            data.local.sha + '</code></td><td>' + data.local.date + '</td><td>' +
            _escHtml(data.local.message) + '</td></tr>';
    html += '<tr><td>' + I18n.t('update.row_remote') + '</td><td><code>' +
            data.remote.sha + '</code></td><td>' + data.remote.date + '</td><td>' +
            _escHtml(data.remote.message) + '</td></tr>';
    html += '</table>';
    doBtn.textContent = I18n.t('update.do_update');
    doBtn.classList.remove('hidden');
    doBtn.onclick = async () => {
      doBtn.disabled = true;
      doBtn.textContent = I18n.t('update.updating');
      try {
        await api('/api/update', { method: 'POST' });
      } catch { /* server goes down */ }
      modal.classList.add('hidden');
      _setStatus('scanning', '', I18n.t('update.restarting'));
      const _tryReload = () => {
        fetch('/api/version').then(r => {
          if (r.ok) window.location.reload();
          else setTimeout(_tryReload, 1000);
        }).catch(() => setTimeout(_tryReload, 1000));
      };
      setTimeout(_tryReload, 2000);
    };
  }
  content.innerHTML = html;
  okBtn.textContent = I18n.t('btn.ok');
  modal.classList.remove('hidden');
}

function _escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getDate())}.${pad(d.getMonth()+1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function _loadSessions() {
  const tbody = document.getElementById('sessions-tbody');
  tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text2)">…</td></tr>';
  let data;
  try {
    const r = await fetch('/api/admin/sessions');
    if (!r.ok) throw new Error(r.status);
    data = await r.json();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" style="color:var(--danger,#c0392b)">${_escHtml(String(e))}</td></tr>`;
    return;
  }
  const sessions = data.sessions || [];
  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text2)">—</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  sessions.forEach(s => {
    const tr = document.createElement('tr');
    const userCell = s.is_current
      ? `<td class="sess-current">★ ${_escHtml(s.username)}</td>`
      : `<td>${_escHtml(s.username)}</td>`;
    const domainCell = `<td>${_escHtml(s.domain || '—')}</td>`;
    const adminCell  = s.is_admin
      ? `<td class="sess-admin-yes">✓</td>`
      : `<td class="sess-admin-no">—</td>`;
    const createdCell = `<td>${_fmtTs(s.created_at)}</td>`;
    const expiresCell = `<td>${_fmtTs(s.expires)}</td>`;
    const ipCell      = `<td>${_escHtml(s.ip || '—')}</td>`;
    const btnLabel    = I18n.t('sessions.btn_terminate');
    const actionCell  = s.is_current
      ? '<td></td>'
      : `<td><button class="btn-terminate" data-sid="${_escHtml(s.id)}">${_escHtml(btnLabel)}</button></td>`;
    tr.innerHTML = userCell + domainCell + adminCell + createdCell + expiresCell + ipCell + actionCell;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll('.btn-terminate').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const r = await fetch(`/api/admin/sessions/${btn.dataset.sid}`, { method: 'DELETE' });
        if (!r.ok) throw new Error(r.status);
        await _loadSessions();
      } catch (e) {
        btn.disabled = false;
        alert('Error: ' + e);
      }
    });
  });
}

function _openSessionsModal() {
  document.getElementById('sessions-modal').classList.remove('hidden');
  _loadSessions();
}

function _showAboutModal() {
  const version = (window.__APP_VERSION__ || '') +
    (window.__BUILD_DATE__ ? '  ·  ' + window.__BUILD_DATE__ : '');
  const html = I18n.t('about.body', { version });
  const content = document.getElementById('about-content');
  content.innerHTML = '<h2>' + I18n.t('about.title') + '</h2>' + html;
  // Re-apply i18n to the OK button in case language changed
  document.getElementById('about-ok').textContent = I18n.t('btn.ok');
  document.getElementById('about-modal').classList.remove('hidden');
}

function _reapplyDynamicTexts() {
  // Re-apply texts that are set dynamically and may not have data-i18n attrs

  // Theme button
  const themeBtn = document.getElementById('menu-theme');
  if (themeBtn) {
    const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
    themeBtn.textContent = isDark ? I18n.t('menu.theme_light') : I18n.t('menu.theme_dark');
  }

  // Channel label — only reset to prompt if no channel selected
  if (!currentChannel) {
    const lbl = document.getElementById('channel-label');
    if (lbl) {
      lbl.setAttribute('data-i18n', 'channel.select_prompt');
      lbl.textContent = I18n.t('channel.select_prompt');
    }
    document.getElementById('channel-list-btn')?.classList.add('no-channel');
    document.getElementById('channel-label')?.classList.add('no-channel');
  }

  // Copy button title
  const copy = document.getElementById('channel-copy-btn');
  if (copy && currentChannel) {
    const folderPath = _channelFolderPath(currentChannel);
    copy.title = folderPath
      ? I18n.t('channel.copy_path_title_fmt', { path: folderPath })
      : I18n.t('channel.copy_path_unavailable');
  }

  // Clock part titles
  document.querySelectorAll('#clk-day, #clk-month, #clk-year, #clk-h, #clk-m, #clk-s').forEach(el => {
    el.title = I18n.t('clock.adjust_title');
  });

  // Re-render log list to update button titles
  renderLogList();

  // Redraw timeline so day/month names update immediately
  Timeline.drawAll();

  // Update page title with translated app name
  const el = document.getElementById('menu-ver');
  if (el) {
    const verText = el.textContent;
    const vMatch = verText.match(/v[\d.]+/);
    if (vMatch) {
      const appName = I18n.t('app.title');
      document.title = `${appName} ${vMatch[0]}`;
      el.textContent = verText.replace(/^[^\s]+/, appName);
    }
  }
}

async function loadVersion() {
  try {
    const { version, build_date } = await api('/api/version');
    const el = document.getElementById('menu-ver');
    const appName = I18n.t('app.title');
    const verStr = build_date ? `${appName} v${version} · ${build_date}` : `${appName} v${version}`;
    if (el) el.textContent = verStr;
    document.title = `${appName} v${version}`;
  } catch (e) { /* ignore */ }
}

// ── Brand link ─────────────────────────────────────────────────────────────
function _updateBrandLink() {
  const link = document.getElementById('app-brand-link');
  if (!link) return;
  const ts = Math.round(Timeline.getCenterTime());
  const params = new URLSearchParams({ t: ts });
  if (currentChannel) params.set('ch', currentChannel.id);
  link.href = `${location.pathname}?${params}`;
}

// ── Helpers ────────────────────────────────────────────────────────────────
function _fmt2(n) { return String(n).padStart(2, '0'); }

function _hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1,3), 16);
  const g = parseInt(hex.slice(3,5), 16);
  const b = parseInt(hex.slice(5,7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function _contrastColor(hex) {
  const r = parseInt(hex.slice(1,3), 16);
  const g = parseInt(hex.slice(3,5), 16);
  const b = parseInt(hex.slice(5,7), 16);
  const luminance = (0.299*r + 0.587*g + 0.114*b) / 255;
  return luminance > 0.5 ? '#222' : '#fff';
}
