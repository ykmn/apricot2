/* Settings GUI — Абрикос 2. Talks to /api/config/* (admin only). */
'use strict';

// ── DOM helpers ──────────────────────────────────────────────────────────────

function $(sel, root) { return (root || document).querySelector(sel); }

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

function esc(s) { return String(s == null ? '' : s); }

// ── API ──────────────────────────────────────────────────────────────────────

async function api(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  if (resp.status === 401) {
    location.href = '/login?next=' + encodeURIComponent('/settings');
    throw new Error('Unauthorized');
  }
  if (!resp.ok) {
    let msg = resp.status + ' ' + resp.statusText;
    try { msg = (await resp.json()).detail || msg; } catch (_) { /* keep */ }
    throw new Error(msg);
  }
  return resp.json();
}

const getCfg = (url) => api('GET', url);
const postCfg = (url, b) => api('POST', url, b);
const putCfg = (url, b) => api('PUT', url, b);
const delCfg = (url) => api('DELETE', url);

let statusTimer = null;
function setStatus(text, isErr) {
  const s = $('#settings-status');
  s.textContent = text;
  s.style.color = isErr ? '#ff8a8a' : '';
  clearTimeout(statusTimer);
  if (text) statusTimer = setTimeout(() => { s.textContent = ''; }, 4000);
}

// ── Modal stack ──────────────────────────────────────────────────────────────

const modalStack = [];

function renderModal() {
  const modal = $('#cfg-modal');
  const top = modalStack[modalStack.length - 1];
  if (!top) { modal.classList.add('hidden'); return; }
  modal.classList.remove('hidden');
  $('.cfg-modal-box', modal).classList.toggle('cfg-modal-wide', !!top.wide);
  $('#cfg-modal-title').textContent = top.title;
  $('#cfg-modal-error').classList.add('hidden');
  const body = $('#cfg-modal-body');
  body.innerHTML = '';
  body.appendChild(top.render());
  $('#cfg-modal-save').textContent = top.saveLabel || 'Сохранить';
}

function pushModal(cfg) { modalStack.push(cfg); renderModal(); }
function popModal() { modalStack.pop(); renderModal(); }

$('#cfg-modal-cancel').addEventListener('click', popModal);
$('#cfg-modal-save').addEventListener('click', async () => {
  const top = modalStack[modalStack.length - 1];
  if (!top) return;
  const errBox = $('#cfg-modal-error');
  errBox.classList.add('hidden');
  try {
    await top.onSave($('#cfg-modal-body'));
    popModal();
  } catch (exc) {
    errBox.textContent = String(exc.message || exc);
    errBox.classList.remove('hidden');
  }
});

// Esc закрывает верхний диалог: сначала подтверждение, затем редактор
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('#cfg-confirm').classList.contains('hidden')) {
    $('#cfg-confirm-cancel').click();
  } else if (modalStack.length) {
    popModal();
  }
});

// ── Confirm dialog ───────────────────────────────────────────────────────────

function confirmDlg(text, onOk) {
  $('#cfg-confirm-text').textContent = text;
  $('#cfg-confirm').classList.remove('hidden');
  const ok = $('#cfg-confirm-ok'), cancel = $('#cfg-confirm-cancel');
  const close = () => {
    $('#cfg-confirm').classList.add('hidden');
    ok.onclick = cancel.onclick = null;
  };
  ok.onclick = async () => { close(); await onOk(); };
  cancel.onclick = close;
}

// ── Shared state ─────────────────────────────────────────────────────────────

const state = {
  secrets: [],
  stations: [],
  playlogs: [],
  users: [],
  ldap: null,
  settings: null,
};

// ── SMB path string helpers ──────────────────────────────────────────────────

function smbToString(smb) {
  if (!smb) return '';
  let s = '\\\\' + smb.host + '\\' + smb.share;
  if (smb.path) {
    // Нормализуем смешанные слэши (в YAML путь может быть записан через "/")
    const p = String(smb.path).replace(/[\\/]+/g, '\\').replace(/^\\+|\\+$/g, '');
    if (p) s += '\\' + p;
  }
  return s;
}

function parseSmbString(str) {
  const m = String(str).trim().match(/^[\\/]{2}([^\\/]+)[\\/]+([^\\/]+)(?:[\\/]+(.*))?$/);
  if (!m) throw new Error('SMB-путь должен быть вида \\\\HOST\\SHARE\\папка\\…');
  const smb = {
    host: m[1],
    share: m[2],
    path: (m[3] || '').replace(/[\\/]+/g, '\\').replace(/\\+$/, ''),
  };
  return smb;
}

function secretSelect(selected) {
  const sel = el('select', { name: 'secret' });
  sel.appendChild(el('option', { value: '', text: '— без secret —' }));
  for (const s of state.secrets) {
    const label = s.domain ? `${s.id}: ${s.domain}\\${s.username}` : `${s.id}: ${s.username}`;
    sel.appendChild(el('option', { value: String(s.id), text: label }));
  }
  if (selected != null && selected !== '') sel.value = String(selected);
  return sel;
}

// ── Transliteration (channel id suffix from name) ────────────────────────────

const TRANSLIT = {
  'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
  'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
  'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
  'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
  'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
};

function translit(name) {
  return esc(name).toLowerCase().split('').map(ch => {
    if (ch in TRANSLIT) return TRANSLIT[ch];
    if (/[a-z0-9]/.test(ch)) return ch;
    return '_';
  }).join('').replace(/_+/g, '_').replace(/^_+|_+$/g, '');
}

// ── Tabs ─────────────────────────────────────────────────────────────────────

document.querySelectorAll('.stab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.stab').forEach(b => b.classList.toggle('active', b === btn));
    document.querySelectorAll('.spanel').forEach(p =>
      p.classList.toggle('active', p.id === 'panel-' + btn.dataset.tab));
  });
});

// ════════════════════════════════════════════════════════════════════════════
// STATIONS
// ════════════════════════════════════════════════════════════════════════════

const FILE_EXTENSIONS = ['wav', 'mp3', 'aac', 'flac'];
const SAMPLE_RATES = [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 96000];

function renderStations() {
  const panel = $('#panel-stations');
  panel.innerHTML = '';
  panel.appendChild(el('div', { class: 'cfg-toolbar' },
    el('button', {
      class: 'btn-green btn-sm', text: 'Создать…',
      onclick: () => openStationEditor(null),
    }),
  ));
  const table = el('table', { class: 'cfg-table' });
  table.appendChild(el('thead', null, el('tr', null,
    el('th', { text: 'ID' }), el('th', { text: 'Название' }),
    el('th', { text: 'Каналов' }), el('th', { text: 'Действия' }),
  )));
  const tbody = el('tbody');
  for (const st of state.stations) {
    tbody.appendChild(el('tr', null,
      el('td', { text: st.id }),
      el('td', { text: st.name }),
      el('td', { text: String((st.channels || []).length) }),
      el('td', { class: 'cfg-actions' },
        el('button', { text: 'Редактировать…', onclick: () => openStationEditor(st) }),
        el('button', { text: 'Клонировать…', onclick: () => cloneStation(st) }),
        el('button', {
          class: 'cfg-del', text: 'Удалить…',
          onclick: () => confirmDlg(
            `Удалить станцию «${st.name}» (${st.id}) со всеми каналами? Файл будет удалён.`,
            async () => {
              await delCfg('/api/config/stations/' + encodeURIComponent(st.id));
              setStatus('Станция удалена');
              await loadStations();
            }),
        }),
      ),
    ));
  }
  table.appendChild(tbody);
  panel.appendChild(table);
}

async function loadStations() {
  state.stations = await getCfg('/api/config/stations');
  renderStations();
}

function cloneStation(st) {
  const copy = JSON.parse(JSON.stringify(st));
  copy.id = st.id + '_copy';
  copy.name = st.name + ' (копия)';
  for (const ch of copy.channels || []) ch.id = ch.id + '_copy';
  openStationEditor(copy, true);
}

// ── Station editor ───────────────────────────────────────────────────────────

function openStationEditor(station, isNew) {
  const isCreate = !station || isNew;
  const st = station
    ? JSON.parse(JSON.stringify(station))
    : { id: '', name: '', channels: [] };
  st.channels = st.channels || [];
  const origId = station && !isNew ? station.id : null;

  pushModal({
    title: (origId ? 'Станция: ' : 'Новая станция') + (st.name || ''),
    saveLabel: 'Сохранить',
    wide: true,
    render() {
      const form = el('div', { class: 'cfg-form' });

      const idInput = el('input', { type: 'text', name: 'id', value: st.id, placeholder: 'europaplus' });
      const nameInput = el('input', { type: 'text', name: 'name', value: st.name, placeholder: 'Европа Плюс' });
      // Auto-suggest station id from name while id untouched
      let idDirty = !!st.id;
      idInput.addEventListener('input', () => { idDirty = true; });
      nameInput.addEventListener('input', () => {
        if (!idDirty) idInput.value = translit(nameInput.value);
      });
      form.appendChild(el('div', { class: 'cfg-row' },
        el('label', null, el('span', { text: 'ID (латиница, цифры, - _)' }), idInput),
        el('label', null, el('span', { text: 'Название' }), nameInput),
      ));

      // Channels sub-table
      const sub = el('div', { class: 'cfg-subtable' });
      const redraw = () => {
        sub.innerHTML = '';
        sub.appendChild(el('div', { class: 'cfg-subtable-header' },
          el('span', { text: `Каналы (${st.channels.length})` }),
          el('button', {
            class: 'btn-green btn-sm', text: 'Добавить канал…',
            onclick: (ev) => { ev.preventDefault(); openChannelEditor(st, null, redraw); },
          }),
        ));
        const tbl = el('table', { class: 'cfg-table' });
        tbl.appendChild(el('thead', null, el('tr', null,
          el('th', { text: 'ID' }), el('th', { text: 'Название' }),
          el('th', { text: 'Путь' }), el('th', { text: 'Формат' }),
          el('th', { text: 'Действия' }),
        )));
        const tb = el('tbody');
        st.channels.forEach((ch, i) => {
          tb.appendChild(el('tr', null,
            el('td', { text: ch.id }),
            el('td', { text: ch.name }),
            el('td', { text: smbToString(ch.smb) || ch.local_path || '—' }),
            el('td', { text: `${ch.file_extension || 'wav'} / ${ch.sample_rate || 44100}` }),
            el('td', { class: 'cfg-actions' },
              el('button', {
                text: 'Изменить…',
                onclick: (ev) => { ev.preventDefault(); openChannelEditor(st, i, redraw); },
              }),
              el('button', {
                text: 'Клонировать',
                onclick: (ev) => {
                  ev.preventDefault();
                  const c = JSON.parse(JSON.stringify(ch));
                  c.id = ch.id + '_copy';
                  c.name = ch.name + ' (копия)';
                  st.channels.splice(i + 1, 0, c);
                  redraw();
                },
              }),
              el('button', {
                class: 'cfg-del', text: '✕',
                onclick: (ev) => {
                  ev.preventDefault();
                  confirmDlg(`Удалить канал «${ch.name}» (${ch.id}) из станции?`, async () => {
                    st.channels.splice(i, 1);
                    redraw();
                  });
                },
              }),
            ),
          ));
        });
        tbl.appendChild(tb);
        sub.appendChild(tbl);
      };
      redraw();
      form.appendChild(sub);
      return form;
    },
    async onSave(body) {
      st.id = $('input[name=id]', body).value.trim();
      st.name = $('input[name=name]', body).value.trim();
      if (!st.id) throw new Error('Введите ID станции');
      if (!st.name) throw new Error('Введите название станции');
      if (origId) await putCfg('/api/config/stations/' + encodeURIComponent(origId), st);
      else await postCfg('/api/config/stations', st);
      setStatus('Станция сохранена');
      await loadStations();
    },
  });
}

// ── Channel editor ───────────────────────────────────────────────────────────

function openChannelEditor(st, chIndex, onDone) {
  const isNew = chIndex == null;
  const ch = isNew
    ? {
        id: '', name: '', smb: { host: '', share: '', path: '', secret: '' },
        folder_format: '%Y-%m-%d', file_format: '%H-%M-%S',
        file_extension: 'wav', sample_rate: 44100, playlogs: [], playlogs_offset: 0,
      }
    : JSON.parse(JSON.stringify(st.channels[chIndex]));

  pushModal({
    title: isNew ? 'Новый канал' : 'Канал: ' + ch.name,
    saveLabel: isNew ? 'Добавить' : 'Применить',
    render() {
      const form = el('div', { class: 'cfg-form' });

      const nameInput = el('input', { type: 'text', name: 'name', value: ch.name, placeholder: 'Европа Плюс FM' });
      const idInput = el('input', { type: 'text', name: 'id', value: ch.id, placeholder: 'europaplus_fm' });
      // id = <stationId>_<translit(name)> until user edits id manually
      let idDirty = !isNew;
      idInput.addEventListener('input', () => { idDirty = true; });
      const autoId = () => {
        if (idDirty) return;
        const prefix = st.id || 'station';
        const suffix = translit(nameInput.value);
        idInput.value = suffix ? prefix + '_' + suffix : prefix + '_';
      };
      nameInput.addEventListener('input', autoId);

      form.appendChild(el('div', { class: 'cfg-row' },
        el('label', null, el('span', { text: 'Название канала' }), nameInput),
        el('label', null,
          el('span', { text: 'ID канала (префикс = ID станции)' }), idInput,
          el('div', { class: 'cfg-hint', text: 'Формируется автоматически, можно изменить' })),
      ));

      // SMB
      const smbInput = el('input', {
        type: 'text', name: 'smb_path', value: smbToString(ch.smb),
        placeholder: '\\\\EMG-LOG3\\LOGGER\\Europa Plus FM',
      });
      const secretSel = secretSelect(ch.smb ? ch.smb.secret : null);
      form.appendChild(el('label', null,
        el('span', { text: 'SMB-путь (одной строкой)' }), smbInput,
        el('div', { class: 'cfg-hint', text: 'Разбивается на host / share / path автоматически' }),
      ));
      form.appendChild(el('label', null,
        el('span', { text: 'Учётные данные (secret из secret.yaml)' }), secretSel));

      // Playlogs checkboxes
      const checks = el('div', { class: 'cfg-checks', name: 'playlogs' });
      const selected = new Set(ch.playlogs || []);
      for (const pl of state.playlogs) {
        const cb = el('input', { type: 'checkbox', value: pl.id });
        cb.checked = selected.has(pl.id);
        checks.appendChild(el('label', null, cb, `${pl.id} — ${pl.name}`));
      }
      form.appendChild(el('label', null, el('span', { text: 'Плейлоги' }), checks));

      // Formats
      const extSel = el('select', { name: 'file_extension' });
      for (const e of FILE_EXTENSIONS) extSel.appendChild(el('option', { value: e, text: e }));
      extSel.value = (ch.file_extension || 'wav').toLowerCase();

      const srSel = el('select', { name: 'sample_rate' });
      const rates = SAMPLE_RATES.includes(+ch.sample_rate) ? SAMPLE_RATES : [...SAMPLE_RATES, +ch.sample_rate];
      for (const r of rates) srSel.appendChild(el('option', { value: String(r), text: String(r) }));
      srSel.value = String(ch.sample_rate || 44100);

      const ffInput = el('input', { type: 'text', name: 'folder_format', value: ch.folder_format || '', placeholder: '%Y-%m-%d' });
      const tfInput = el('input', { type: 'text', name: 'file_format', value: ch.file_format || '', placeholder: '%H-%M-%S' });

      form.appendChild(el('div', { class: 'cfg-row' },
        el('label', null, el('span', { text: 'Расширение файлов' }), extSel),
        el('label', null, el('span', { text: 'Частота дискретизации' }), srSel),
      ));
      form.appendChild(el('div', { class: 'cfg-row' },
        el('label', null, el('span', { text: 'Формат папок (strftime)' }), ffInput),
        el('label', null, el('span', { text: 'Формат файлов (strftime)' }), tfInput),
      ));

      return form;
    },
    async onSave(body) {
      const name = $('input[name=name]', body).value.trim();
      const id = $('input[name=id]', body).value.trim();
      if (!name) throw new Error('Введите название канала');
      if (!id) throw new Error('Введите ID канала');
      if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error('ID: только латиница, цифры, - _');
      const dup = st.channels.some((c, i) => c.id === id && i !== chIndex);
      if (dup) throw new Error(`ID «${id}» уже используется в этой станции`);

      const smbStr = $('input[name=smb_path]', body).value.trim();
      const smb = smbStr ? parseSmbString(smbStr) : null;
      const secretVal = $('select[name=secret]', body).value;
      if (smb && secretVal) smb.secret = Number(secretVal);

      const playlogs = [];
      body.querySelectorAll('.cfg-checks input[type=checkbox]').forEach(cb => {
        if (cb.checked) playlogs.push(cb.value);
      });

      const updated = {
        ...ch,
        id, name, smb,
        playlogs,
        file_extension: $('select[name=file_extension]', body).value,
        sample_rate: Number($('select[name=sample_rate]', body).value),
        folder_format: $('input[name=folder_format]', body).value.trim() || '%Y-%m-%d',
        file_format: $('input[name=file_format]', body).value.trim() || '%H-%M-%S',
      };
      if (isNew) st.channels.push(updated);
      else st.channels[chIndex] = updated;
      if (onDone) onDone();
    },
  });
}

// ════════════════════════════════════════════════════════════════════════════
// PLAYLOGS
// ════════════════════════════════════════════════════════════════════════════

const DEFAULT_FIELDS = {
  datetime: 'EventTime', title: 'ElemName', artist: 'ElemArtist',
  cls: 'ElemClass', db_id: 'ElemDbId', id_number: 'ElemIdNumber',
  duration: 'ElemLength',
};
const DEFAULT_CLASS_COLORS = {
  M: '#558b2f', J: '#1565c0', C: '#e65100', P: '#6a1b9a', N: '#37474f', default: '#424242',
};
const DEFAULT_CLASS_NAMES = {
  M: 'Музыка', J: 'Джингл', C: 'Реклама', P: 'Передача', N: 'Новости',
};

function renderPlaylogs() {
  const panel = $('#panel-playlogs');
  panel.innerHTML = '';
  panel.appendChild(el('div', { class: 'cfg-toolbar' },
    el('button', {
      class: 'btn-green btn-sm', text: 'Создать…',
      onclick: () => openPlaylogEditor(null),
    }),
  ));
  const table = el('table', { class: 'cfg-table' });
  table.appendChild(el('thead', null, el('tr', null,
    el('th', { text: 'ID' }), el('th', { text: 'Название' }),
    el('th', { text: 'Источников' }), el('th', { text: 'Действия' }),
  )));
  const tbody = el('tbody');
  for (const pl of state.playlogs) {
    tbody.appendChild(el('tr', null,
      el('td', { text: pl.id }),
      el('td', { text: pl.name }),
      el('td', { text: String((pl.sources || []).length) }),
      el('td', { class: 'cfg-actions' },
        el('button', { text: 'Редактировать…', onclick: () => openPlaylogEditor(pl) }),
        el('button', { text: 'Клонировать…', onclick: () => clonePlaylog(pl) }),
        el('button', {
          class: 'cfg-del', text: 'Удалить…',
          onclick: () => confirmDlg(
            `Удалить плейлог «${pl.name}» (${pl.id})? Файл будет удалён, ссылки в каналах очищены.`,
            async () => {
              await delCfg('/api/config/playlogs/' + encodeURIComponent(pl.id));
              // Каскад: убрать ссылки на удалённый плейлог из всех каналов
              await loadStations();
              const affected = [];
              for (const st of state.stations) {
                let dirty = false;
                for (const ch of st.channels || []) {
                  const before = (ch.playlogs || []).length;
                  ch.playlogs = (ch.playlogs || []).filter(id => id !== pl.id);
                  if (ch.playlogs.length !== before) dirty = true;
                }
                if (dirty) affected.push(st);
              }
              for (const st of affected) {
                await putCfg('/api/config/stations/' + encodeURIComponent(st.id), st);
              }
              setStatus('Плейлог удалён');
              await loadPlaylogs();
            }),
        }),
      ),
    ));
  }
  table.appendChild(tbody);
  panel.appendChild(table);
}

async function loadPlaylogs() {
  state.playlogs = await getCfg('/api/config/playlogs');
  renderPlaylogs();
}

function clonePlaylog(pl) {
  const copy = JSON.parse(JSON.stringify(pl));
  copy.id = pl.id + '_copy';
  copy.name = pl.name + ' (копия)';
  openPlaylogEditor(copy, true);
}

function playlogDefaults() {
  return {
    id: '', name: '',
    sources: [{
      priority: 1, smb: { host: '', share: '', path: '', secret: '' },
      file_mask: '%Y-%m-%d.log', encoding: 'windows-1251',
      delimiter: ',', header_skip_prefix: 'FIELD LIST',
    }],
    fields: { ...DEFAULT_FIELDS },
    class_colors: { ...DEFAULT_CLASS_COLORS },
    class_names: { ...DEFAULT_CLASS_NAMES },
  };
}

// ── Playlog editor ───────────────────────────────────────────────────────────

function openPlaylogEditor(playlog, isNew) {
  const origId = playlog && !isNew ? playlog.id : null;
  const pl = playlog ? JSON.parse(JSON.stringify(playlog)) : playlogDefaults();
  pl.sources = pl.sources && pl.sources.length ? pl.sources : playlogDefaults().sources;
  pl.fields = { ...DEFAULT_FIELDS, ...(pl.fields || {}) };
  pl.class_colors = pl.class_colors || {};
  pl.class_names = pl.class_names || {};

  pushModal({
    title: (origId ? 'Плейлог: ' : 'Новый плейлог') + (pl.name || ''),
    wide: true,
    render() {
      const form = el('div', { class: 'cfg-form' });

      const idInput = el('input', { type: 'text', name: 'id', value: pl.id, placeholder: 'europaplus' });
      const nameInput = el('input', { type: 'text', name: 'name', value: pl.name, placeholder: 'Европа Плюс' });
      let idDirty = !!pl.id;
      idInput.addEventListener('input', () => { idDirty = true; });
      nameInput.addEventListener('input', () => {
        if (!idDirty) idInput.value = translit(nameInput.value);
      });
      form.appendChild(el('div', { class: 'cfg-row' },
        el('label', null, el('span', { text: 'ID' }), idInput),
        el('label', null, el('span', { text: 'Название' }), nameInput),
      ));

      // ── Sources
      const srcBox = el('div');
      const redrawSources = () => {
        srcBox.innerHTML = '';
        srcBox.appendChild(el('div', { class: 'cfg-subtable-header' },
          el('span', { text: `Источники (по приоритету)` }),
          el('button', {
            class: 'btn-green btn-sm', text: 'Добавить источник…',
            onclick: (ev) => {
              ev.preventDefault();
              pl.sources.push({
                priority: pl.sources.length + 1,
                smb: { host: '', share: '', path: '', secret: '' },
                file_mask: '%Y-%m-%d.log', encoding: 'windows-1251',
                delimiter: ',', header_skip_prefix: 'FIELD LIST',
              });
              redrawSources();
            },
          }),
        ));
        pl.sources.forEach((src, i) => {
          const fs = el('fieldset', { class: 'cfg-fieldset' });
          fs.appendChild(el('legend', { text: `Источник · priority ${i + 1}` }));

          const smbInput = el('input', {
            type: 'text', name: `src_smb_${i}`, value: smbToString(src.smb),
            placeholder: '\\\\AIR-1\\DigispotII\\djin\\PlaybackLog',
          });
          const secretSel = secretSelect(src.smb ? src.smb.secret : null);
          secretSel.name = `src_secret_${i}`;
          const maskInput = el('input', { type: 'text', name: `src_mask_${i}`, value: src.file_mask || '', placeholder: '%Y-%m-%d.log' });
          const encSel = el('select', { name: `src_enc_${i}` },
            el('option', { value: 'windows-1251', text: 'windows-1251' }),
            el('option', { value: 'utf-8', text: 'UTF-8' }),
          );
          encSel.value = (src.encoding || 'windows-1251').toLowerCase() === 'utf-8' ? 'utf-8' : 'windows-1251';
          const delimInput = el('input', { type: 'text', name: `src_delim_${i}`, value: src.delimiter ?? '', placeholder: ',' });
          const hdrInput = el('input', { type: 'text', name: `src_hdr_${i}`, value: src.header_skip_prefix || '', placeholder: 'FIELD LIST' });

          fs.appendChild(el('label', null, el('span', { text: 'SMB-путь (одной строкой)' }), smbInput));
          fs.appendChild(el('div', { class: 'cfg-row' },
            el('label', null, el('span', { text: 'Учётные данные (secret)' }), secretSel),
            el('label', null, el('span', { text: 'Маска файла' }), maskInput),
          ));
          fs.appendChild(el('div', { class: 'cfg-row' },
            el('label', null, el('span', { text: 'Кодировка' }), encSel),
            el('label', null, el('span', { text: 'Разделитель' }), delimInput),
            el('label', null, el('span', { text: 'Префикс заголовка' }), hdrInput),
          ));

          if (pl.sources.length > 1) {
            fs.appendChild(el('div', { class: 'cfg-actions', style: 'text-align:right' },
              el('button', {
                class: 'cfg-del', text: 'Удалить источник',
                onclick: (ev) => { ev.preventDefault(); pl.sources.splice(i, 1); redrawSources(); },
              }),
            ));
          }
          srcBox.appendChild(fs);
        });
      };
      redrawSources();
      form.appendChild(srcBox);

      // ── Fields mapping
      const fieldsFs = el('fieldset', { class: 'cfg-fieldset' });
      fieldsFs.appendChild(el('legend', { text: 'Маппинг полей CSV' }));
      const fRow = el('div', { class: 'cfg-row' });
      const fCol1 = el('div'), fCol2 = el('div');
      fCol1.style.flex = fCol2.style.flex = '1';
      Object.keys(DEFAULT_FIELDS).forEach((key, idx) => {
        const inp = el('input', {
          type: 'text', name: 'field_' + key,
          value: pl.fields[key] || '', placeholder: DEFAULT_FIELDS[key],
        });
        (idx % 2 ? fCol2 : fCol1).appendChild(el('label', null, el('span', { text: key }), inp));
      });
      fRow.appendChild(fCol1); fRow.appendChild(fCol2);
      fieldsFs.appendChild(fRow);
      form.appendChild(fieldsFs);

      // ── Классы: цвета и названия в одной таблице
      const clsFs = el('fieldset', { class: 'cfg-fieldset' });
      clsFs.appendChild(el('legend', { text: 'Классы (цвет и название)' }));
      const clsTable = el('table', { class: 'cfg-cls-table', 'data-cls': '1' });
      const clsBody = el('tbody');
      const addClsRow = (k) => {
        const keyInp = el('input', {
          type: 'text', value: k, placeholder: 'Класс', name: 'cls_key',
        });
        const colorInp = el('input', {
          type: 'color', name: 'cls_color',
          value: /^#[0-9a-fA-F]{6}$/.test(pl.class_colors[k]) ? pl.class_colors[k] : '#424242',
        });
        const nameInp = el('input', {
          type: 'text', value: pl.class_names[k] || '', placeholder: 'Название', name: 'cls_name',
        });
        clsBody.appendChild(el('tr', { class: 'cfg-cls-row' },
          el('td', { class: 'cfg-cls-key' }, keyInp),
          el('td', { class: 'cfg-cls-color' }, colorInp),
          el('td', null, nameInp),
          el('td', { class: 'cfg-cls-del' },
            el('button', {
              class: 'cfg-kv-del cfg-del', text: '✕',
              onclick: (ev) => { ev.preventDefault(); ev.target.closest('tr').remove(); },
            })),
        ));
      };
      const allKeys = [...new Set([...Object.keys(pl.class_colors), ...Object.keys(pl.class_names)])];
      allKeys.forEach(addClsRow);
      clsTable.appendChild(el('thead', null, el('tr', null,
        el('th', { text: 'Класс' }), el('th', { text: 'Цвет' }), el('th', { text: 'Название' }), el('th'),
      )));
      clsTable.appendChild(clsBody);
      clsFs.appendChild(clsTable);
      clsFs.appendChild(el('button', {
        class: 'btn-sm', text: '+ Класс',
        onclick: (ev) => { ev.preventDefault(); addClsRow(''); },
      }));
      form.appendChild(clsFs);

      return form;
    },
    async onSave(body) {
      pl.id = $('input[name=id]', body).value.trim();
      pl.name = $('input[name=name]', body).value.trim();
      if (!pl.id) throw new Error('Введите ID плейлога');
      if (!pl.name) throw new Error('Введите название плейлога');

      const sources = [];
      for (let i = 0; i < pl.sources.length; i++) {
        const smbStr = $(`input[name=src_smb_${i}]`, body).value.trim();
        if (!smbStr) throw new Error(`Источник ${i + 1}: введите SMB-путь`);
        const smb = parseSmbString(smbStr);
        const secretVal = $(`select[name=src_secret_${i}]`, body).value;
        if (secretVal) smb.secret = Number(secretVal);
        sources.push({
          priority: i + 1,
          smb,
          file_mask: $(`input[name=src_mask_${i}]`, body).value.trim() || '%Y-%m-%d.log',
          encoding: $(`select[name=src_enc_${i}]`, body).value,
          delimiter: $(`input[name=src_delim_${i}]`, body).value || ',',
          header_skip_prefix: $(`input[name=src_hdr_${i}]`, body).value.trim() || 'FIELD LIST',
        });
      }
      pl.sources = sources;

      const fields = {};
      for (const key of Object.keys(DEFAULT_FIELDS)) {
        fields[key] = $('input[name=field_' + key + ']', body).value.trim();
      }
      pl.fields = fields;

      const colors = {}, names = {};
      body.querySelectorAll('.cfg-cls-row').forEach(row => {
        const k = $('input[name=cls_key]', row).value.trim();
        if (!k) return;
        colors[k] = $('input[name=cls_color]', row).value;
        const nm = $('input[name=cls_name]', row).value.trim();
        if (nm) names[k] = nm;
      });
      pl.class_colors = colors;
      pl.class_names = names;

      if (origId) await putCfg('/api/config/playlogs/' + encodeURIComponent(origId), pl);
      else await postCfg('/api/config/playlogs', pl);

      // Каскад: ID переименован — обновить ссылки во всех каналах всех станций
      if (origId && origId !== pl.id) {
        await loadStations();
        const affected = [];
        for (const st of state.stations) {
          let dirty = false;
          for (const ch of st.channels || []) {
            const idx = (ch.playlogs || []).indexOf(origId);
            if (idx !== -1) { ch.playlogs[idx] = pl.id; dirty = true; }
          }
          if (dirty) affected.push(st);
        }
        for (const st of affected) {
          await putCfg('/api/config/stations/' + encodeURIComponent(st.id), st);
        }
        if (affected.length) {
          setStatus(`Плейлог сохранён, ссылки обновлены в станциях: ${affected.map(s => s.id).join(', ')}`);
        } else {
          setStatus('Плейлог сохранён');
        }
      } else {
        setStatus('Плейлог сохранён');
      }
      await loadPlaylogs();
    },
  });
}

// ════════════════════════════════════════════════════════════════════════════
// AUTH (ldap.yaml)
// ════════════════════════════════════════════════════════════════════════════

function renderAuth() {
  const panel = $('#panel-auth');
  panel.innerHTML = '';
  const cfg = state.ldap;
  const form = el('div', { class: 'cfg-form', style: 'max-width:900px' });

  const localCb = el('input', { type: 'checkbox' });
  localCb.checked = !!cfg.Local;
  const ldapCb = el('input', { type: 'checkbox' });
  ldapCb.checked = !!cfg.LDAP;
  form.appendChild(el('label', { class: 'cfg-flag' }, localCb,
    el('span', { text: 'Локальная авторизация (users.yaml)' })));
  form.appendChild(el('label', { class: 'cfg-flag' }, ldapCb,
    el('span', { text: 'Доменная авторизация (LDAP / Active Directory)' })));

  const groupsTa = el('textarea', { name: 'admin_groups' });
  groupsTa.value = (cfg.admin_groups || []).join('\n');
  form.appendChild(el('label', null,
    el('span', { text: 'Группы AD с правами администратора (по одной на строку)' }), groupsTa));

  const domainsBox = el('div');
  const redrawDomains = () => {
    domainsBox.innerHTML = '';
    domainsBox.appendChild(el('div', { class: 'cfg-subtable-header' },
      el('span', { text: `Домены (${cfg.domains.length})` }),
      el('button', {
        class: 'btn-green btn-sm', text: 'Добавить домен…',
        onclick: (ev) => { ev.preventDefault(); openDomainEditor(cfg, null, redrawDomains); },
      }),
    ));
    const tbl = el('table', { class: 'cfg-table' });
    tbl.appendChild(el('thead', null, el('tr', null,
      el('th', { text: 'Имя' }), el('th', { text: 'Сервер' }),
      el('th', { text: 'Base DN' }), el('th', { text: 'Bind secret' }),
      el('th', { text: 'Действия' }),
    )));
    const tb = el('tbody');
    cfg.domains.forEach((d, i) => {
      tb.appendChild(el('tr', null,
        el('td', { text: d.name || d.domain || '' }),
        el('td', { text: d.server || '' }),
        el('td', { text: d.base_dn || '' }),
        el('td', { text: d.bind_secret != null ? String(d.bind_secret) : '—' }),
        el('td', { class: 'cfg-actions' },
          el('button', {
            text: 'Изменить…',
            onclick: (ev) => { ev.preventDefault(); openDomainEditor(cfg, i, redrawDomains); },
          }),
          el('button', {
            class: 'cfg-del', text: '✕',
            onclick: (ev) => {
              ev.preventDefault();
              confirmDlg(`Удалить домен «${d.name || d.server}»?`, async () => {
                cfg.domains.splice(i, 1);
                redrawDomains();
              });
            },
          }),
        ),
      ));
    });
    tbl.appendChild(tb);
    domainsBox.appendChild(tbl);
  };
  cfg.domains = cfg.domains || [];
  redrawDomains();
  form.appendChild(domainsBox);

  form.appendChild(el('div', { class: 'cfg-toolbar', style: 'margin-top:12px' },
    el('button', {
      class: 'btn-green btn-sm', text: 'Сохранить',
      onclick: async () => {
        try {
          const payload = {
            Local: localCb.checked,
            LDAP: ldapCb.checked,
            admin_groups: groupsTa.value.split('\n').map(s => s.trim()).filter(Boolean),
            domains: cfg.domains,
          };
          await putCfg('/api/config/ldap', payload);
          setStatus('ldap.yaml сохранён');
        } catch (exc) { setStatus(String(exc.message || exc), true); }
      },
    }),
  ));
  panel.appendChild(form);
}

function openDomainEditor(cfg, index, onDone) {
  const isNew = index == null;
  const d = isNew ? { name: '', server: '', base_dn: '', bind_secret: '' }
                  : { ...cfg.domains[index] };
  pushModal({
    title: isNew ? 'Новый домен' : 'Домен: ' + (d.name || d.server),
    saveLabel: isNew ? 'Добавить' : 'Применить',
    render() {
      const form = el('div', { class: 'cfg-form' });
      form.appendChild(el('div', { class: 'cfg-row' },
        el('label', null, el('span', { text: 'Имя (NetBIOS)' }),
          el('input', { type: 'text', name: 'name', value: d.name || '', placeholder: 'EMG' })),
        el('label', null, el('span', { text: 'Сервер (ldap://…)' }),
          el('input', { type: 'text', name: 'server', value: d.server || '', placeholder: 'ldap://dc01.emg.local' })),
      ));
      form.appendChild(el('div', { class: 'cfg-row' },
        el('label', null, el('span', { text: 'Base DN' }),
          el('input', { type: 'text', name: 'base_dn', value: d.base_dn || '', placeholder: 'DC=emg,DC=local' })),
        el('label', null, el('span', { text: 'Сервисный аккаунт (bind secret)' }),
          (() => { const s = secretSelect(d.bind_secret); s.name = 'bind_secret'; return s; })()),
      ));
      return form;
    },
    async onSave(body) {
      const name = $('input[name=name]', body).value.trim();
      const server = $('input[name=server]', body).value.trim();
      const base_dn = $('input[name=base_dn]', body).value.trim();
      if (!name || !server || !base_dn) throw new Error('Заполните имя, сервер и Base DN');
      const secretVal = $('select[name=bind_secret]', body).value;
      const out = { name, server, base_dn };
      if (secretVal) out.bind_secret = Number(secretVal);
      if (isNew) cfg.domains.push(out);
      else cfg.domains[index] = out;
      if (onDone) onDone();
    },
  });
}

// ════════════════════════════════════════════════════════════════════════════
// USERS
// ════════════════════════════════════════════════════════════════════════════

function renderUsers() {
  const panel = $('#panel-users');
  panel.innerHTML = '';
  panel.appendChild(el('div', { class: 'cfg-toolbar' },
    el('button', {
      class: 'btn-green btn-sm', text: 'Создать…',
      onclick: () => openUserEditor(null),
    }),
  ));
  const table = el('table', { class: 'cfg-table' });
  table.appendChild(el('thead', null, el('tr', null,
    el('th', { text: 'Пользователь' }), el('th', { text: 'Администратор' }),
    el('th', { text: 'Пароль' }), el('th', { text: 'Действия' }),
  )));
  const tbody = el('tbody');
  for (const u of state.users) {
    tbody.appendChild(el('tr', null,
      el('td', { text: u.username }),
      el('td', { text: u.is_admin ? 'да' : 'нет' }),
      el('td', { text: u.has_password ? 'задан' : '—' }),
      el('td', { class: 'cfg-actions' },
        el('button', { text: 'Редактировать…', onclick: () => openUserEditor(u) }),
        el('button', {
          class: 'cfg-del', text: 'Удалить…',
          onclick: () => confirmDlg(
            `Удалить пользователя «${u.username}»?`,
            async () => {
              try {
                await delCfg('/api/config/users/' + encodeURIComponent(u.username));
                setStatus('Пользователь удалён');
                await loadUsers();
              } catch (exc) { setStatus(String(exc.message || exc), true); }
            }),
        }),
      ),
    ));
  }
  table.appendChild(tbody);
  panel.appendChild(table);
}

async function loadUsers() {
  state.users = await getCfg('/api/config/users');
  renderUsers();
}

function openUserEditor(user) {
  const isNew = !user;
  pushModal({
    title: isNew ? 'Новый пользователь' : 'Пользователь: ' + user.username,
    render() {
      const form = el('div', { class: 'cfg-form' });
      const nameInput = el('input', {
        type: 'text', name: 'username', value: user ? user.username : '', placeholder: 'ivanov',
      });
      if (!isNew) nameInput.readOnly = true;
      form.appendChild(el('label', null, el('span', { text: 'Имя пользователя' }), nameInput));
      form.appendChild(el('label', null,
        el('span', { text: isNew ? 'Пароль' : 'Новый пароль (пусто — не менять)' }),
        el('input', { type: 'password', name: 'password', autocomplete: 'new-password', placeholder: '••••••••' }),
      ));
      const admCb = el('input', { type: 'checkbox', name: 'is_admin' });
      admCb.checked = user ? !!user.is_admin : false;
      form.appendChild(el('label', { class: 'cfg-flag' }, admCb,
        el('span', { text: 'Администратор' })));
      return form;
    },
    async onSave(body) {
      const password = $('input[name=password]', body).value;
      const is_admin = $('input[name=is_admin]', body).checked;
      if (isNew) {
        const username = $('input[name=username]', body).value.trim();
        if (!username) throw new Error('Введите имя пользователя');
        if (!password) throw new Error('Введите пароль');
        await postCfg('/api/config/users', { username, password, is_admin });
      } else {
        await putCfg('/api/config/users/' + encodeURIComponent(user.username),
          { is_admin, ...(password ? { password } : {}) });
      }
      setStatus('Пользователь сохранён');
      await loadUsers();
    },
  });
}

// ════════════════════════════════════════════════════════════════════════════
// GENERAL (settings.yaml)
// ════════════════════════════════════════════════════════════════════════════

function renderGeneral() {
  const panel = $('#panel-general');
  panel.innerHTML = '';
  const s = state.settings;
  const srv = s.server || {};
  const ssl = srv.ssl || {};
  const form = el('div', { class: 'cfg-form', style: 'max-width:900px' });

  const text = (name, val, ph) => el('input', { type: 'text', name, value: val == null ? '' : esc(val), placeholder: ph || '' });
  const num = (name, val, ph) => el('input', { type: 'number', name, value: val == null ? '' : esc(val), placeholder: ph == null ? '' : String(ph) });
  const flag = (name, val) => {
    const cb = el('input', { type: 'checkbox', name });
    cb.checked = !!val;
    return el('label', { class: 'cfg-flag' }, cb, el('span', { text: '' }));
  };

  const devCb = el('input', { type: 'checkbox', name: 'develop' });
  devCb.checked = !!s.develop;
  form.appendChild(el('label', { class: 'cfg-flag' }, devCb, el('span', { text: 'Режим разработки (develop)' })));

  const serverFs = el('fieldset', { class: 'cfg-fieldset' });
  serverFs.appendChild(el('legend', { text: 'Сервер' }));
  serverFs.appendChild(el('div', { class: 'cfg-row' },
    el('label', null, el('span', { text: 'Хост' }), text('srv_host', srv.host, '0.0.0.0')),
    el('label', null, el('span', { text: 'Порт' }), num('srv_port', srv.port, 443)),
    el('label', null, el('span', { text: 'TTL сессии, сек' }), num('srv_session_ttl', srv.session_ttl, 604800)),
  ));
  const logScr = el('select', { name: 'srv_log_screen' });
  const logFile = el('select', { name: 'srv_log_file' });
  for (const lvl of ['DEBUG', 'INFO', 'WARNING', 'ERROR']) {
    logScr.appendChild(el('option', { value: lvl, text: lvl }));
    logFile.appendChild(el('option', { value: lvl, text: lvl }));
  }
  logScr.value = srv.log_screen || 'INFO';
  logFile.value = srv.log_file || 'DEBUG';
  serverFs.appendChild(el('div', { class: 'cfg-row' },
    el('label', null, el('span', { text: 'Лог на экран' }), logScr),
    el('label', null, el('span', { text: 'Лог в файл' }), logFile),
  ));
  const sslCb = el('input', { type: 'checkbox', name: 'ssl_enabled' });
  sslCb.checked = !!ssl.enabled;
  serverFs.appendChild(el('label', { class: 'cfg-flag' }, sslCb, el('span', { text: 'HTTPS (SSL) включён' })));
  serverFs.appendChild(el('div', { class: 'cfg-row' },
    el('label', null, el('span', { text: 'SSL сертификат' }), text('ssl_cert', ssl.cert, 'ssl/cert.crt')),
    el('label', null, el('span', { text: 'SSL ключ' }), text('ssl_key', ssl.key, 'ssl/key.key')),
    el('label', null, el('span', { text: 'Порт редиректа HTTP→HTTPS' }), num('ssl_redirect', ssl.http_redirect_port, 80)),
  ));
  form.appendChild(serverFs);

  const miscFs = el('fieldset', { class: 'cfg-fieldset' });
  miscFs.appendChild(el('legend', { text: 'Фоновые задачи' }));
  miscFs.appendChild(el('div', { class: 'cfg-row' },
    el('label', null, el('span', { text: 'Опрос SMB-шар, сек' }),
      num('watcher_poll', (s.watcher || {}).poll_interval, 120)),
    el('label', null, el('span', { text: 'Обновление сегодняшнего плейлога, сек' }),
      num('pl_refresh', (s.playlogs || {}).today_refresh_interval, 1800)),
  ));
  const rescanCb = el('input', { type: 'checkbox', name: 'rescan_all' });
  rescanCb.checked = !!(s.indexing || {}).rescan_all_on_startup;
  miscFs.appendChild(el('label', { class: 'cfg-flag' }, rescanCb,
    el('span', { text: 'Полное пересканирование при запуске' })));
  form.appendChild(miscFs);

  const expFs = el('fieldset', { class: 'cfg-fieldset' });
  expFs.appendChild(el('legend', { text: 'Экспорт' }));
  const expFmt = el('select', { name: 'exp_format' });
  for (const f of ['mp3', 'wav', 'aac']) expFmt.appendChild(el('option', { value: f, text: f }));
  expFmt.value = (s.export || {}).default_format || 'mp3';
  const expBr = el('select', { name: 'exp_bitrate' });
  for (const b of ['320k', '192k', '128k', '64k']) expBr.appendChild(el('option', { value: b, text: b }));
  expBr.value = (s.export || {}).default_bitrate || '192k';
  expFs.appendChild(el('div', { class: 'cfg-row' },
    el('label', null, el('span', { text: 'Формат по умолчанию' }), expFmt),
    el('label', null, el('span', { text: 'Битрейт по умолчанию' }), expBr),
    el('label', null, el('span', { text: 'Хранение экспорта, дней' }),
      num('exp_retention', (s.export || {}).retention_days, 1)),
    el('label', null, el('span', { text: 'Время очистки (HH:MM)' }),
      text('exp_cleanup', (s.export || {}).cleanup_time, '03:00')),
  ));
  form.appendChild(expFs);

  const miscFs2 = el('fieldset', { class: 'cfg-fieldset' });
  miscFs2.appendChild(el('legend', { text: 'Прочее' }));
  miscFs2.appendChild(el('div', { class: 'cfg-row' },
    el('label', null, el('span', { text: 'Путь к ffmpeg' }), text('ffmpeg_path', s.ffmpeg_path, 'ffmpeg')),
    el('label', null, el('span', { text: 'Часовой пояс UI' }), text('ui_tz', (s.ui || {}).timezone, 'Europe/Moscow')),
    el('label', null, el('span', { text: 'Репозиторий обновлений' }), text('update_repo', (s.update || {}).repo_url, 'https://github.com/…/apricot2')),
  ));
  form.appendChild(miscFs2);

  form.appendChild(el('div', { class: 'cfg-toolbar', style: 'margin-top:12px' },
    el('button', {
      class: 'btn-green btn-sm', text: 'Сохранить',
      onclick: async () => {
        try {
          const payload = {
            develop: devCb.checked,
            server: {
              host: $('input[name=srv_host]', form).value.trim() || '0.0.0.0',
              port: Number($('input[name=srv_port]', form).value) || 443,
              session_ttl: Number($('input[name=srv_session_ttl]', form).value) || 604800,
              log_screen: logScr.value,
              log_file: logFile.value,
              ssl: {
                enabled: sslCb.checked,
                cert: $('input[name=ssl_cert]', form).value.trim(),
                key: $('input[name=ssl_key]', form).value.trim(),
                http_redirect_port: Number($('input[name=ssl_redirect]', form).value) || 0,
              },
            },
            watcher: { poll_interval: Number($('input[name=watcher_poll]', form).value) || 120 },
            indexing: { rescan_all_on_startup: rescanCb.checked },
            playlogs: { today_refresh_interval: Number($('input[name=pl_refresh]', form).value) || 1800 },
            export: {
              default_format: expFmt.value,
              default_bitrate: expBr.value,
              retention_days: Number($('input[name=exp_retention]', form).value) || 1,
              cleanup_time: $('input[name=exp_cleanup]', form).value.trim() || '03:00',
            },
            ffmpeg_path: $('input[name=ffmpeg_path]', form).value.trim() || 'ffmpeg',
            ui: { timezone: $('input[name=ui_tz]', form).value.trim() || 'Europe/Moscow' },
            update: { repo_url: $('input[name=update_repo]', form).value.trim() },
          };
          await putCfg('/api/config/settings', payload);
          state.settings = payload;
          setStatus('settings.yaml сохранён. Изменения сервера/SSL применятся после перезапуска.');
        } catch (exc) { setStatus(String(exc.message || exc), true); }
      },
    }),
  ));
  panel.appendChild(form);
}

// ════════════════════════════════════════════════════════════════════════════
// INIT
// ════════════════════════════════════════════════════════════════════════════

(async function init() {
  try {
    [state.secrets, state.playlogs, state.users, state.ldap, state.settings] =
      await Promise.all([
        getCfg('/api/config/secrets'),
        getCfg('/api/config/playlogs'),
        getCfg('/api/config/users'),
        getCfg('/api/config/ldap'),
        getCfg('/api/config/settings'),
      ]);
    await loadStations();
    renderPlaylogs();
    renderAuth();
    renderUsers();
    renderGeneral();
  } catch (exc) {
    if (String(exc.message).includes('403') || String(exc.message).includes('Admin')) {
      document.body.innerHTML =
        '<div style="padding:40px;font-size:15px">Нужны права администратора. <a href="/">← Назад</a></div>';
    } else if (exc.message !== 'Unauthorized') {
      setStatus(String(exc.message || exc), true);
    }
  }
})();
