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

const audio = document.getElementById('audio-player');

// ── Bootstrap ──────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  Timeline.init({
    onTimeChange: ts => {
      refreshPlaylistForVisible();
    },
    onSelChange: (s, e) => {
      updateSelLabel(s, e);
    },
  });

  await loadStations();
  buildChannelDropdown();
  initChannelSearch();
  initTransport();
  initLogList();
  initExportModal();
  initClockControls();
  connectWebSocket();

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
  const inp = document.getElementById('channel-search');
  const dd  = document.getElementById('channel-dropdown');
  const lbl = document.getElementById('channel-label');

  inp.addEventListener('focus', () => {
    dd.classList.remove('hidden');
    inp.select();
  });
  inp.addEventListener('blur', () => {
    setTimeout(() => dd.classList.add('hidden'), 150);
  });
  inp.addEventListener('input', () => {
    filterChannelDropdown(inp.value);
  });
}

function selectChannel(ch, st) {
  currentChannel = ch;
  const inp = document.getElementById('channel-search');
  const lbl = document.getElementById('channel-label');
  inp.value = ch.name;
  lbl.textContent = '';

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
      alert('Сначала выделите фрагмент (◀| и |▶)');
      return;
    }
    if (!currentChannel) {
      alert('Выберите канал записи');
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

  // Navigate to prev/next file
  document.getElementById('btn-prev-file').addEventListener('click', () => navigateFile(-1));
  document.getElementById('btn-next-file').addEventListener('click', () => navigateFile(1));

  audio.addEventListener('ended', () => stopPlay());
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

  const url = `/api/audio/stream?channel=${currentChannel.id}&start=${startTs}&end=${endTs}&format=mp3&bitrate=192k`;
  audio.src = url;
  audio.play().then(() => {
    isPlaying = true;
    document.getElementById('btn-play').textContent = '⏸';
    playStartTime = Date.now();
    playFromTs    = startTs;
    _startPlayheadAnimation();
  }).catch(e => console.warn('Playback failed:', e));
}

function stopPlay() {
  audio.pause();
  audio.src = '';
  isPlaying = false;
  document.getElementById('btn-play').textContent = '▶';
  cancelAnimationFrame(playAnimFrame);
}

function _startPlayheadAnimation() {
  function frame() {
    if (!isPlaying) return;
    const elapsed = (Date.now() - playStartTime) / 1000;
    Timeline.setTime(playFromTs + elapsed);
    playAnimFrame = requestAnimationFrame(frame);
  }
  playAnimFrame = requestAnimationFrame(frame);
}

async function navigateFile(dir) {
  // Not implemented in minimal version — would require querying sorted file list
}

function updateSelLabel(s, e) {
  const lbl = document.getElementById('sel-label');
  if (s === null && e === null) { lbl.textContent = '—'; return; }
  const durSec = (s !== null && e !== null) ? Math.abs(e - s) : null;
  const parts = [];
  if (s !== null) parts.push(_tsToHMS(s));
  if (e !== null) parts.push(_tsToHMS(e));
  let txt = parts.join(' → ');
  if (durSec !== null) txt += `  [${_secToDuration(durSec)}]`;
  lbl.textContent = txt;
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

// ── Log-list ───────────────────────────────────────────────────────────────
function initLogList() {
  document.getElementById('btn-clear-log').addEventListener('click', () => {
    logItems = [];
    renderLogList();
  });
  document.getElementById('btn-export-all').addEventListener('click', exportAll);
  document.getElementById('btn-uploads').addEventListener('click', () => {
    // Open the temp download folder or show a list — simplified to alert
    alert('Экспортированные файлы сохраняются во временную папку системы.\nИспользуйте кнопку ↓ рядом с каждой записью для скачивания.');
  });
}

function addLogItem(item) {
  const id = 'log_' + Date.now() + '_' + Math.random().toString(36).slice(2);
  logItems.push({ id, ...item });
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
function initExportModal() {
  document.getElementById('exp-cancel').addEventListener('click', () => {
    document.getElementById('export-modal').classList.add('hidden');
  });
  document.getElementById('exp-ok').addEventListener('click', doExport);

  // Toggle bitrate row visibility: hidden for WAV (PCM has no bitrate)
  document.getElementById('exp-format').addEventListener('change', _updateExportFields);
}

function _updateExportFields() {
  const fmt = document.getElementById('exp-format').value;
  const bitrateRow = document.getElementById('exp-bitrate-row');
  const wavNote    = document.getElementById('exp-wav-note');
  if (fmt === 'wav') {
    bitrateRow.classList.add('hidden');
    wavNote.classList.remove('hidden');
  } else {
    bitrateRow.classList.remove('hidden');
    wavNote.classList.add('hidden');
  }
}

function openExportModal(item) {
  exportTarget = item;
  document.getElementById('export-progress').classList.add('hidden');
  _updateExportFields();
  document.getElementById('export-modal').classList.remove('hidden');
}

function _buildExportBody(item) {
  const fmt        = document.getElementById('exp-format').value;
  const bitrate    = document.getElementById('exp-bitrate').value;
  const samplerate = parseInt(document.getElementById('exp-samplerate').value, 10);
  return {
    channel_id:  item.channel_id,
    start:       item.start,
    end:         item.end,
    format:      fmt,
    bitrate:     fmt === 'wav' ? null : bitrate,
    sample_rate: samplerate,
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

// ── WebSocket (real-time availability updates) ─────────────────────────────
function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.addEventListener('message', ev => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'availability_update' && currentChannel && msg.channel_id === currentChannel.id) {
      Timeline.addAvailability(msg.added, msg.removed);
    }
  });

  ws.addEventListener('close', () => {
    setTimeout(connectWebSocket, 3000);
  });
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
