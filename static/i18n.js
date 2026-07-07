'use strict';

const I18n = (() => {
  const _cache  = {};
  let   _lang   = 'ru';
  let   _data   = {};

  function _get(key) {
    const parts = key.split('.');
    let cur = _data;
    for (const p of parts) {
      if (cur == null || typeof cur !== 'object') return null;
      cur = cur[p];
    }
    return typeof cur === 'string' ? cur : null;
  }

  function t(key, params = {}) {
    let s = _get(key) ?? key;
    for (const [k, v] of Object.entries(params))
      s = s.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v));
    return s;
  }

  function applyToDOM(root = document) {
    root.querySelectorAll('[data-i18n]').forEach(el => {
      el.textContent = t(el.dataset.i18n);
    });
    root.querySelectorAll('[data-i18n-html]').forEach(el => {
      el.innerHTML = t(el.dataset.i18nHtml);
    });
    root.querySelectorAll('[data-i18n-ph]').forEach(el => {
      el.placeholder = t(el.dataset.i18nPh);
    });
    root.querySelectorAll('[data-i18n-title]').forEach(el => {
      el.title = t(el.dataset.i18nTitle);
    });
    // settings.yaml: develop: true → mark the header so a debug server is
    // never mistaken for production. Applied after the translation pass so
    // it survives language switches.
    if (window.__DEVELOP__) {
      const appName = root.querySelector('#app-name');
      if (appName) appName.textContent += ' отладочный сервер';
    }
  }

  async function _load(lang) {
    if (!_cache[lang]) {
      const v = window.__APP_VERSION__ || '0';
      const resp = await fetch(`/static/languages/${lang}.json?v=${v}`);
      if (!resp.ok) throw new Error(`i18n: failed to load ${lang}`);
      _cache[lang] = await resp.json();
    }
    return _cache[lang];
  }

  async function setLang(lang) {
    _data = await _load(lang);
    _lang = lang;
    localStorage.setItem('apricot-lang', lang);
    applyToDOM();
    // Update active state on language buttons if present
    document.querySelectorAll('.menu-lang-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.lang === lang);
    });
  }

  async function init() {
    _lang = localStorage.getItem('apricot-lang') || 'ru';
    _data = await _load(_lang);
  }

  function getLang() { return _lang; }

  return { t, applyToDOM, setLang, init, getLang };
})();
