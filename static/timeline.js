/**
 * Multi-scale timeline renderer.
 *
 * Five canvas rows (seconds / minutes / hours / days / months) are all
 * linked to a single `centerTime` (Unix timestamp, seconds).
 * Dragging or wheel-scrolling any row updates centerTime and redraws all rows.
 * Color bands show audio-file availability for the selected channel.
 */

'use strict';

const Timeline = (() => {

  // ── Row definitions ─────────────────────────────────────────────────────
  // timePerCell: how many real seconds one labeled cell represents
  // cellWidth:   pixels per cell
  const ROW_DEFS = [
    { id: 'tl-seconds', timePerCell: 1,          cellWidth: 28,  unit: 'second' },
    { id: 'tl-minutes', timePerCell: 60,          cellWidth: 28,  unit: 'minute' },
    { id: 'tl-hours',   timePerCell: 3600,        cellWidth: 44,  unit: 'hour',   tall: true },
    { id: 'tl-days',    timePerCell: 86400,       cellWidth: 110, unit: 'day'   },
    { id: 'tl-months',  timePerCell: 30 * 86400,  cellWidth: 110, unit: 'month' },
  ];

  // Color scheme — reads from CSS custom properties so light/dark theme works
  function _cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function _getC() {
    return {
      bg:         _cssVar('--tl-bg')      || '#1a1a2e',
      bgAbsent:   _cssVar('--tl-absent')  || '#1e1e3a',
      bgPresent:  _cssVar('--tl-present') || '#1a6bb5',
      border:     _cssVar('--cell-border')|| '#0a3070',
      centerLine: _cssVar('--center-line')|| '#f0d050',
      selFill:    'rgba(240,208,80,0.18)',
      selBorder:  _cssVar('--center-line')|| '#f0d050',
      text:       _cssVar('--tl-text')    || '#c0c8e0',
      textDim:    _cssVar('--tl-text-dim')|| '#5060a0',
    };
  }
  // kept for backward compat — recalculated each draw
  const C = {
    bg:           '#1a1a2e',
    bgAbsent:     '#1e1e3a',
    bgPresent:    '#1a6bb5',
    bgPresentAlt: '#1558a0',
    border:       '#0a3070',
    centerLine:   '#f0d050',
    selFill:      'rgba(255,220,50,0.18)',
    selBorder:    '#f0d050',
    text:         '#c0c8e0',
    textDim:      '#5060a0',
    waveform:     'rgba(120,200,255,0.35)',
    today:        '#0f9af0',
  };

  // State
  let centerTime = Date.now() / 1000;   // Unix timestamp (seconds)
  let selStart = null;                  // selection start (Unix ts)
  let selEnd   = null;                  // selection end   (Unix ts)
  let channelId = null;
  let availability = [];                // [{start, end}] from API
  let rows = [];
  let onTimeChange = null;              // callback(ts)
  let onSelChange  = null;              // callback(selStart, selEnd)
  let isPlaying = false;
  let playbackStart = null;
  let playbackTimer = null;

  // ── Initialise ───────────────────────────────────────────────────────────
  function init(callbacks = {}) {
    onTimeChange = callbacks.onTimeChange || null;
    onSelChange  = callbacks.onSelChange  || null;

    rows = ROW_DEFS.map(def => {
      const canvas = document.getElementById(def.id);
      const ctx = canvas.getContext('2d');
      return { ...def, canvas, ctx };
    });

    rows.forEach(row => _attachEvents(row));
    window.addEventListener('resize', () => { _resizeAll(); drawAll(); });
    _resizeAll();
    drawAll();
  }

  function _resizeAll() {
    rows.forEach(row => {
      row.canvas.width = row.canvas.offsetWidth;
    });
  }

  // ── Public API ───────────────────────────────────────────────────────────
  function setTime(ts) {
    centerTime = ts;
    drawAll();
    if (onTimeChange) onTimeChange(ts);
  }

  function setChannel(id) {
    channelId = id;
    availability = [];
    drawAll();
    _fetchAvailability();
  }

  function setAvailability(intervals) {
    availability = intervals;
    drawAll();
  }

  function addAvailability(added, removed) {
    removed.forEach(r => {
      availability = availability.filter(a => !(a.start === r.start && a.end === r.end));
    });
    availability.push(...added);
    availability.sort((a, b) => a.start - b.start);
    drawAll();
  }

  function getSelection() { return { start: selStart, end: selEnd }; }

  function setSelStart(ts) {
    selStart = ts !== undefined ? ts : centerTime;
    if (selEnd !== null && selEnd < selStart) selEnd = null;
    _notifySel();
    drawAll();
  }

  function setSelEnd(ts) {
    selEnd = ts !== undefined ? ts : centerTime;
    if (selStart !== null && selEnd < selStart) [selStart, selEnd] = [selEnd, selStart];
    _notifySel();
    drawAll();
  }

  function clearSelection() {
    selStart = null;
    selEnd   = null;
    _notifySel();
    drawAll();
  }

  function _notifySel() {
    if (onSelChange) onSelChange(selStart, selEnd);
  }

  // ── Fetch availability from API ──────────────────────────────────────────
  async function _fetchAvailability() {
    if (!channelId) return;
    // Fetch 90 days back + 2 days ahead
    const end   = centerTime + 2 * 86400;
    const start = centerTime - 90 * 86400;
    try {
      const resp = await fetch(`/api/availability/${channelId}?start=${start}&end=${end}`);
      if (!resp.ok) return;
      availability = await resp.json();
      drawAll();
    } catch (e) { /* offline */ }
  }

  // ── Draw ─────────────────────────────────────────────────────────────────
  function drawAll() {
    rows.forEach(row => _drawRow(row));
    _updateClock();
  }

  function _drawRow(row) {
    const { canvas, ctx, cellWidth, timePerCell, unit, tall } = row;
    const W = canvas.width;
    const H = canvas.height;
    if (W === 0) return;

    const TC = _getC();   // theme-aware colors

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = TC.bg;
    ctx.fillRect(0, 0, W, H);

    const pxPerSec = cellWidth / timePerCell;

    const leftTime   = centerTime - W / 2 / pxPerSec;
    const rightTime  = centerTime + W / 2 / pxPerSec;
    const firstCell  = Math.floor(leftTime / timePerCell);
    const lastCell   = Math.ceil(rightTime / timePerCell);

    // Draw availability bands first (behind grid)
    if (availability.length) {
      availability.forEach(({ start, end }) => {
        const xStart = Math.round(W / 2 + (start - centerTime) * pxPerSec);
        const xEnd   = Math.round(W / 2 + (end   - centerTime) * pxPerSec);
        if (xEnd < 0 || xStart > W) return;
        const x  = Math.max(0, xStart);
        const x2 = Math.min(W, xEnd);
        ctx.fillStyle = TC.bgPresent;
        ctx.fillRect(x, 0, x2 - x, H);
      });
    }

    // Draw selection highlight
    if (selStart !== null || selEnd !== null) {
      const sA = selStart ?? selEnd;
      const sB = selEnd   ?? selStart;
      const xs = Math.round(W / 2 + (Math.min(sA,sB) - centerTime) * pxPerSec);
      const xe = Math.round(W / 2 + (Math.max(sA,sB) - centerTime) * pxPerSec);
      if (xe > 0 && xs < W) {
        ctx.fillStyle = TC.selFill;
        ctx.fillRect(Math.max(0, xs), 0, Math.min(W, xe) - Math.max(0, xs), H);
        ctx.strokeStyle = TC.selBorder;
        ctx.lineWidth = 1;
        if (xs >= 0 && xs < W) { ctx.beginPath(); ctx.moveTo(xs+0.5,0); ctx.lineTo(xs+0.5,H); ctx.stroke(); }
        if (xe >= 0 && xe < W) { ctx.beginPath(); ctx.moveTo(xe+0.5,0); ctx.lineTo(xe+0.5,H); ctx.stroke(); }
      }
    }

    // Draw cells (borders + labels)
    ctx.strokeStyle = TC.border;
    ctx.lineWidth = 1;

    for (let ci = firstCell; ci <= lastCell; ci++) {
      const cellStart = ci * timePerCell;
      const x = Math.round(W / 2 + (cellStart - centerTime) * pxPerSec);

      ctx.beginPath();
      ctx.moveTo(x + 0.5, 0);
      ctx.lineTo(x + 0.5, H);
      ctx.stroke();

      const label = _cellLabel(ci, cellStart, unit);
      const cx = x + cellWidth / 2;
      if (cx < -cellWidth || cx > W + cellWidth) continue;

      ctx.fillStyle = TC.text;
      ctx.font = tall ? 'bold 12px monospace' : '11px monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, cx, H / 2);
    }

    // Center vertical line
    ctx.strokeStyle = C.centerLine;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(W / 2, 0);
    ctx.lineTo(W / 2, H);
    ctx.strokeStyle = TC.centerLine;
    ctx.stroke();
  }

  function _cellLabel(cellIndex, cellStartSec, unit) {
    const d = new Date(cellStartSec * 1000);
    switch (unit) {
      case 'second': return _z2(d.getSeconds());
      case 'minute': return _z2(d.getMinutes());
      case 'hour':   return _z2(d.getHours());
      case 'day': {
        const dayNames = ['Вс','Пн','Вт','Ср','Чт','Пт','Сб'];
        return `${d.getDate()} (${dayNames[d.getDay()]})`;
      }
      case 'month': {
        const monthNames = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];
        return `${monthNames[d.getMonth()]} ${d.getFullYear()}`;
      }
    }
    return '';
  }

  function _z2(n) { return String(n).padStart(2, '0'); }

  // ── Clock display ────────────────────────────────────────────────────────
  function _updateClock() {
    const d = new Date(centerTime * 1000);
    _setText('clk-day',   _z2(d.getDate()));
    _setText('clk-month', _z2(d.getMonth() + 1));
    _setText('clk-year',  d.getFullYear());
    _setText('clk-h',     _z2(d.getHours()));
    _setText('clk-m',     _z2(d.getMinutes()));
    _setText('clk-s',     _z2(d.getSeconds()));
  }

  function _setText(id, v) {
    const el = document.getElementById(id);
    if (el) el.textContent = v;
  }

  // ── Events ───────────────────────────────────────────────────────────────
  function _attachEvents(row) {
    const { canvas, cellWidth, timePerCell } = row;
    const secPerPx = timePerCell / cellWidth;   // seconds per pixel for this row

    let drag = false;
    let dragX = 0;

    canvas.addEventListener('mousedown', e => {
      drag  = true;
      dragX = e.clientX;
      canvas.style.cursor = 'grabbing';
    });

    window.addEventListener('mousemove', e => {
      if (!drag) return;
      const dx = e.clientX - dragX;
      dragX = e.clientX;
      centerTime -= dx * secPerPx;
      drawAll();
      if (onTimeChange) onTimeChange(centerTime);
      if (channelId) _debounceFetch();
    });

    window.addEventListener('mouseup', () => {
      if (drag) { drag = false; canvas.style.cursor = 'grab'; }
    });

    canvas.addEventListener('wheel', e => {
      e.preventDefault();
      centerTime += e.deltaY * secPerPx;
      drawAll();
      if (onTimeChange) onTimeChange(centerTime);
      if (channelId) _debounceFetch();
    }, { passive: false });

    // Touch support
    let lastTouchX = 0;
    canvas.addEventListener('touchstart', e => {
      lastTouchX = e.touches[0].clientX;
    }, { passive: true });
    canvas.addEventListener('touchmove', e => {
      e.preventDefault();
      const dx = e.touches[0].clientX - lastTouchX;
      lastTouchX = e.touches[0].clientX;
      centerTime -= dx * secPerPx;
      drawAll();
      if (onTimeChange) onTimeChange(centerTime);
    }, { passive: false });
  }

  let _fetchTimer = null;
  function _debounceFetch() {
    clearTimeout(_fetchTimer);
    _fetchTimer = setTimeout(_fetchAvailability, 300);
  }

  // ── Exports ──────────────────────────────────────────────────────────────
  return {
    init, drawAll, setTime, setChannel, setAvailability, addAvailability,
    getSelection, setSelStart, setSelEnd, clearSelection,
    getCenterTime: () => centerTime,
  };

})();
