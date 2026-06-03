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

const _LOG_KEY = 'avocado-logitems';

const audio = document.getElementById('audio-player');

// ── Bootstrap ──────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  Timeline.init({
    onTimeChange: ts => {
      refreshPlaylistForVisible();
      if (isPlaying && !_tlAnimUpdate) _debouncedSeek(ts);
    },
    onSelChange: (s, e) => {
      updateSelLabel(s, e);
    },
  });

  await loadStations();
  buildChannelDropdown();
  initChannelSearch();
  initTransport();
  _loadLogItems();
  initLogList();
  initExportModal();
  initClockControls();
  initHamburgerMenu();
  initHotkeys();
  initStatusBar();
  connectWebSocket();
  loadVersion();

  // Default to current time
  Timeline.setTime(Date.now() / 1000);
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
  copy.addEventListener('click', async () => {
    const path = _channelFolderPath(currentChannel || {});
    if (!path) {
      alert('Путь к папке недоступен для этого канала.');
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
      prompt('Скопируйте путь вручную:', path);
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
  if (ch.local_path) return ch.local_path;
  if (ch.smb) {
    const parts = [ch.smb.host, ch.smb.share];
    if (ch.smb.path) parts.push(ch.smb.path);
    return '\\\\' + parts.join('\\');
  }
  return '';
}

function selectChannel(ch, st) {
  currentChannel = ch;
  const inp  = document.getElementById('channel-search');
  const lbl  = document.getElementById('channel-label');
  const copy = document.getElementById('channel-copy-btn');
  inp.value = '';
  inp.placeholder = 'Фильтр…';
  lbl.textContent = ch.name;
  const folderPath = _channelFolderPath(ch);
  copy.title = folderPath ? `Скопировать путь: ${folderPath}` : 'Путь недоступен';
  copy.disabled = !folderPath;

  // Mark active
  document.querySelectorAll('.dropdown-channel').forEach(el => {
    el.classList.toggle('active', el.dataset.id === ch.id);
  });
  document.getElementById('channel-dropdown').classList.add('hidden');

  Timeline.setChannel(ch.id);
  loadPlaylist();
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
  entries.forEach(e => {
    const d = new Date(e.timestamp * 1000);
    const timeStr = _fmt2(d.getHours()) + ':' + _fmt2(d.getMinutes()) + ':' + _fmt2(d.getSeconds());

    const row = document.createElement('div');
    row.className = 'playlist-row';

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

    const addBtn = document.createElement('button');
    addBtn.className = 'pl-add-btn';
    addBtn.textContent = '↑';
    addBtn.title = 'Добавить в лог-лист';
    addBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      addToLogFromPlaylist(e);
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
  });
}

function addToLogFromPlaylist(entry) {
  const dur = entry.duration || 180;
  addLogItem({
    channel_id:   currentChannel.id,
    channel_name: currentChannel.name,
    start: entry.timestamp,
    end:   entry.timestamp + dur,
    label: entry.title,
  });
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
      alert('Сначала выделите фрагмент кнопками In и Out');
      return;
    }
    if (!currentChannel) {
      alert('Выберите канал записи');
      return;
    }
    const durSec = Math.abs(end - start);
    if (durSec > MAX_SEGMENT_SEC) {
      alert('Слишком большой сегмент — экспорт не будет корректным.\nВыделите фрагмент не длиннее 3 часов.');
      return;
    }
    addLogItem({
      channel_id:   currentChannel.id,
      channel_name: currentChannel.name,
      start, end,
      label: '',
    });
    Timeline.clearSelection();
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
  const startTs = (selS !== null) ? selS : ts;
  const endTs   = (selE !== null) ? selE : startTs + 3600;

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
  _seekTimer = setTimeout(() => _seekPlayback(ts), 250);
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
    lbl.textContent = '—';
    warn.classList.add('hidden');
    return;
  }

  const durSec = (s !== null && e !== null) ? Math.abs(e - s) : null;
  const parts = [];
  if (s !== null) parts.push(_tsToHMS(s));
  if (e !== null) parts.push(_tsToHMS(e));
  let txt = parts.join(' → ');
  if (durSec !== null) txt += `  [${_secToDuration(durSec)}]`;
  lbl.textContent = txt;

  if (durSec !== null && durSec > MAX_SEGMENT_SEC) {
    warn.textContent = '⚠ Слишком большой сегмент, экспорт не будет корректным';
    warn.classList.remove('hidden');
  } else {
    warn.classList.add('hidden');
  }
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

// ── Log-list persistence ───────────────────────────────────────────────────
function _saveLogItems() {
  try { localStorage.setItem(_LOG_KEY, JSON.stringify(logItems)); } catch(e) {}
}

function _loadLogItems() {
  try {
    const raw = localStorage.getItem(_LOG_KEY);
    if (raw) { logItems = JSON.parse(raw); renderLogList(); }
  } catch(e) {}
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
  const container = document.getElementById('loglist-items');
  container.innerHTML = '';
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
    dlBtn.title = 'Экспортировать';
    dlBtn.addEventListener('click', () => openExportModal(item));

    // Navigate button
    const navBtn = document.createElement('button');
    navBtn.className = 'log-btn log-btn-nav';
    navBtn.textContent = '↗';
    navBtn.title = 'Перейти к фрагменту';
    navBtn.addEventListener('click', () => {
      Timeline.setTime(item.start);
      // Also restore selection
      Timeline.setSelStart(item.start);
      Timeline.setSelEnd(item.end);
    });

    // Delete button
    const delBtn = document.createElement('button');
    delBtn.className = 'log-btn log-btn-del';
    delBtn.textContent = '✕';
    delBtn.title = 'Удалить из лог-листа';
    delBtn.addEventListener('click', () => {
      logItems = logItems.filter(i => i.id !== item.id);
      _saveLogItems();
      renderLogList();
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
  prog.textContent = 'Экспорт…';
  try {
    const result = await api('/api/audio/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_buildExportBody(exportTarget)),
    });
    prog.textContent = 'Готово! Скачивание…';
    _triggerDownload(result.download_url, result.filename);
    setTimeout(() => document.getElementById('export-modal').classList.add('hidden'), 1200);
  } catch (e) {
    prog.textContent = 'Ошибка экспорта: ' + e.message;
  }
}

async function exportAll() {
  for (const item of logItems) {
    await doExportItem(item);
  }
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
  } catch (e) {
    console.error('Export failed for', item, e);
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

    // Visual feedback: cursor pointer + title
    el.style.cursor = 'pointer';
    el.title = 'ЛКМ — увеличить, ПКМ — уменьшить';
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

// ── Hotkeys ────────────────────────────────────────────────────────────────
// 0        — Play / Pause
// [        — Mark In (начало выделения)
// ]        — Mark Out (конец выделения)
// S        — Фокус на поле выбора канала
// + / =    — Увеличить единицу времени под курсором на таймлайне
// -        — Уменьшить единицу времени под курсором на таймлайне
function initHotkeys() {
  // Единицы таймлайна → единицы _adjustTime (совпадают)
  const TIMELINE_UNITS = new Set(['second', 'minute', 'hour', 'day', 'month']);

  document.addEventListener('keydown', e => {
    // Не перехватывать клавиши, когда фокус в поле ввода или textarea
    const tag = document.activeElement && document.activeElement.tagName;
    const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

    // S без модификаторов — фокус на поиске канала (работает даже из input)
    if ((e.key === 's' || e.key === 'S') && !e.ctrlKey && !e.altKey && !e.metaKey && !isInput) {
      e.preventDefault();
      const inp = document.getElementById('channel-search');
      if (inp) { inp.focus(); inp.select(); }
      return;
    }

    if (isInput) return;   // остальные горячие клавиши — только вне полей ввода

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
      case '=': {   // '=' — это '+' без Shift на EN-раскладке
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

// ── Index status bar ───────────────────────────────────────────────────────
let _statusChannels = [];   // [{id, name, files, done, failed, rescanning}]

function initStatusBar() {
  document.getElementById('status-close').addEventListener('click', () => {
    document.getElementById('status-bar').classList.add('hidden');
  });

  // Fetch current state (handles page reload mid-scan or after scan)
  fetch('/api/index_status')
    .then(r => r.json())
    .then(data => _applyIndexStatus(data))
    .catch(() => _setStatus('error', '⚠', 'Не удалось получить статус индекса'));
}

function _applyIndexStatus(data) {
  _statusChannels = data.channels || [];
  _rebuildDots();

  if (data.status === 'ready' || data.status === 'idle') {
    const failedN   = _statusChannels.filter(c => c.failed).length;
    const failedStr = failedN ? ` · ⚠ ${failedN} недоступн.` : '';
    _setStatus('ready', '✓',
      `Индекс готов · ${data.total_files} файлов · ${data.total_channels} каналов${failedStr}`);
    if (failedN) _makeDiagLink();
  } else if (data.status === 'scanning') {
    const done  = data.done_channels;
    const total = data.total_channels;
    const active = _statusChannels.find(c => !c.done);
    const label  = active ? ` · ${_displayName(active.name)}` : '';
    _setStatus('scanning', '⟳', `Обновление (${done}/${total})${label}…`);
  }
}

function _handleCacheLoaded(msg) {
  _setStatus('scanning', '📦',
    `Кэш загружен · ${msg.total_files} файлов · обновление в фоне…`);
  // Index just became available from cache — re-fetch without clearing existing data
  if (currentChannel) Timeline.refreshAvailability();
}

function _handleIndexScanning(msg) {
  _statusChannels.forEach(c => { c.rescanning = (c.id === msg.channel_id); });
  _rebuildDots();
  _setStatus('scanning', '⟳',
    `Обновление (${msg.done}/${msg.total}) · ${_displayName(msg.channel_name)}…`);
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
      `Пересканирование завершено · ${_displayName(msg.channel_name)} · ${msg.files} файлов`);
    if (currentChannel && currentChannel.id === msg.channel_id) {
      Timeline.setChannel(currentChannel.id);
    }
    return;
  }

  const nextCh = _statusChannels.find(c => !c.done);
  const label  = nextCh ? ` · ${_displayName(nextCh.name)}` : '';
  _setStatus('scanning', '⟳',
    `Обновление (${msg.done}/${msg.total})${label}…`,
    `готово: ${_displayName(msg.channel_name)} · ${msg.files} файлов`);
}

function _handleIndexError(msg) {
  const ch = _statusChannels.find(c => c.id === msg.channel_id);
  if (ch) { ch.done = true; ch.failed = true; ch.files = 0; ch.rescanning = false; ch.error = msg.error || ''; }
  _rebuildDots();

  const nextCh = _statusChannels.find(c => !c.done);
  const label  = nextCh ? ` · ${_displayName(nextCh.name)}` : '';
  _setStatus('scanning', '⟳',
    `Обновление (${msg.done}/${msg.total})${label}…`,
    `⚠ недоступен: ${_displayName(msg.channel_name)}`);
}

function _handleIndexDone(msg) {
  _statusChannels.forEach(c => { c.rescanning = false; if (!c.done) c.done = true; });
  _rebuildDots();
  // Re-fetch availability now that all scanning is done (without clearing existing data)
  if (currentChannel) Timeline.refreshAvailability();
  const failedN   = _statusChannels.filter(c => c.failed).length;
  const failedStr = failedN ? ` · ⚠ ${failedN} недоступн.` : '';
  _setStatus('ready', '✓',
    `Индекс готов · ${msg.total_files} файлов · ${msg.channels} каналов${failedStr}`);
  if (failedN) _makeDiagLink();
}

let _statusHideTimer = null;

function _setStatus(cls, icon, text, detail = '') {
  clearTimeout(_statusHideTimer);
  const bar = document.getElementById('status-bar');
  bar.classList.remove('status-scanning', 'status-ready', 'status-error', 'hidden');
  bar.classList.add(`status-${cls}`);
  document.getElementById('status-icon').textContent   = icon;
  document.getElementById('status-text').textContent   = text;
  document.getElementById('status-detail').textContent = detail;
  // Auto-clear informational 'ready' messages after 5 seconds (bar stays visible)
  if (cls === 'ready') {
    _statusHideTimer = setTimeout(() => {
      document.getElementById('status-icon').textContent   = '';
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
    let cls = 'status-dot';
    if (c.rescanning)  cls += ' rescanning';
    else if (c.failed) cls += ' failed';
    else if (c.done)   cls += ' done';
    dot.className = cls;
    dot.title = c.failed     ? `${c.name}: недоступен — нажмите для диагностики`
              : c.rescanning ? `${c.name}: обновляется…`
              : `${c.name}: ${c.files} файлов`;
    if (c.failed) {
      dot.style.cursor = 'pointer';
      dot.addEventListener('click', e => { e.stopPropagation(); _showDiagPopover(dot, [c]); });
    }
    dots.appendChild(dot);
  });

  bar.insertBefore(dots, document.getElementById('status-close'));
}

function _makeDiagLink() {
  // Replace "N недоступн." text in status with a clickable span
  const textEl = document.getElementById('status-text');
  if (!textEl) return;
  const html = textEl.textContent;
  const match = html.match(/(⚠\s*\d+\s*недоступн\.)/);
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
      err.textContent = 'причина неизвестна';
    }
    item.appendChild(err);
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
    const msg = JSON.parse(ev.data);
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

  // Theme toggle
  const themeBtn = document.getElementById('menu-theme');
  themeBtn.addEventListener('click', () => {
    const html = document.documentElement;
    const isDark = html.getAttribute('data-theme') !== 'light';
    html.setAttribute('data-theme', isDark ? 'light' : 'dark');
    themeBtn.textContent = isDark ? '🌙 Тёмный режим' : '☀️ Светлый режим';
    localStorage.setItem('avocado-theme', isDark ? 'light' : 'dark');
    Timeline.drawAll();   // repaint canvases with new colors
    menu.classList.add('hidden');
  });

  // Restore saved theme
  const saved = localStorage.getItem('avocado-theme');
  if (saved) {
    document.documentElement.setAttribute('data-theme', saved);
    themeBtn.textContent = saved === 'light' ? '🌙 Тёмный режим' : '☀️ Светлый режим';
  }

  // Rescan current channel
  document.getElementById('menu-rescan').addEventListener('click', async () => {
    menu.classList.add('hidden');
    if (!currentChannel) {
      _setStatus('error', '⚠', 'Сначала выберите канал');
      /* reload done */
      return;
    }
    _setStatus('scanning', '⏳', `Пересканирование · ${_displayName(currentChannel.name)}…`);
    try {
      await api(`/api/rescan/${currentChannel.id}`, { method: 'POST' });
      // Result arrives via WebSocket (index_progress / index_error)
    } catch (e) {
      _setStatus('error', '⚠', `Ошибка пересканирования: ${e.message}`);
      /* error shown */
    }
  });

  // Reload config
  document.getElementById('menu-reload').addEventListener('click', async () => {
    menu.classList.add('hidden');
    _setStatus('scanning', '⏳', 'Обновление конфигурации…');
    try {
      const res = await api('/api/reload', { method: 'POST' });
      await loadStations();
      buildChannelDropdown();
      _setStatus('ready', '✓',
        `Конфигурация обновлена · ${res.stations} ст. · ${res.channels} кан. · ${res.playlists} плейл.`);
    } catch (e) {
      _setStatus('error', '⚠', `Ошибка обновления конфигурации: ${e.message}`);
    }
  });

  document.getElementById('menu-restart').addEventListener('click', async () => {
    menu.classList.add('hidden');
    if (!confirm('Перезапустить сервер?\nСтраница обновится автоматически через несколько секунд.')) return;
    try {
      await api('/api/restart', { method: 'POST' });
    } catch { /* server going down — expected */ }
    _setStatus('scanning', '⏳', 'Перезапуск сервера…');
    // Poll until server is back, then reload
    const _tryReload = () => {
      fetch('/api/version').then(r => {
        if (r.ok) window.location.reload();
        else setTimeout(_tryReload, 1000);
      }).catch(() => setTimeout(_tryReload, 1000));
    };
    setTimeout(_tryReload, 1500);
  });
}

async function loadVersion() {
  try {
    const { version, name, build_date } = await api('/api/version');
    const el = document.getElementById('menu-ver');
    const verStr = build_date ? `${name} v${version} · ${build_date}` : `${name} v${version}`;
    if (el) el.textContent = verStr;
    document.title = `${name} v${version}`;
  } catch (e) { /* ignore */ }
}

// ── Helpers ────────────────────────────────────────────────────────────────
function _fmt2(n) { return String(n).padStart(2, '0'); }

function _contrastColor(hex) {
  const r = parseInt(hex.slice(1,3), 16);
  const g = parseInt(hex.slice(3,5), 16);
  const b = parseInt(hex.slice(5,7), 16);
  const luminance = (0.299*r + 0.587*g + 0.114*b) / 255;
  return luminance > 0.5 ? '#222' : '#fff';
}
