/**
 * HiveDeploy i18n — Lightweight client-side internationalisation
 *
 * Usage:
 *   <span data-i18n="dashboard.my_instances">我的实例</span>
 *   <button data-i18n-placeholder="login.username_placeholder">...</button>
 *   <title data-i18n-title="dashboard.title">HiveDeploy</title>
 *
 * Dynamic keys with {placeholder} substitution:
 *   data-i18n="dashboard.days_left" data-i18n-params='{"days":30}'
 *
 * The library falls back to the existing inner text if a key is missing,
 * making it safe to ship alongside production hard-coded strings.
 */

(function () {
  'use strict';

  const I18N_NAMESPACE = 'hive_i18n';

  // ── Storage helpers ──────────────────────────────────────────
  function getLang() {
    try {
      return localStorage.getItem(I18N_NAMESPACE + '_lang') || 'zh-CN';
    } catch (_) {
      return 'zh-CN';
    }
  }

  function setLang(code) {
    try {
      localStorage.setItem(I18N_NAMESPACE + '_lang', code);
    } catch (_) { /* quota exceeded – silently ignore */ }
  }

  // ── Resource cache ───────────────────────────────────────────
  const _cache = {};

  async function loadResource(code) {
    if (_cache[code]) return _cache[code];
    try {
      const resp = await fetch('/static/i18n/' + code + '.json');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      _cache[code] = data;
      return data;
    } catch (e) {
      console.warn('[i18n] Could not load language pack for ' + code + ':', e.message);
      return null;
    }
  }

  // ── Key resolution ───────────────────────────────────────────
  function resolveKey(resource, key) {
    const parts = key.split('.');
    let cursor = resource;
    for (let i = 0; i < parts.length; i++) {
      if (cursor == null || typeof cursor !== 'object') return undefined;
      cursor = cursor[parts[i]];
    }
    return cursor;
  }

  function interpolate(template, params) {
    if (!template || params == null) return template;
    return template.replace(/\{(\w+)\}/g, function (match, name) {
      return params[name] !== undefined ? params[name] : match;
    });
  }

  // ── DOM application ──────────────────────────────────────────
  function applyToElement(el, resource) {
    // text content
    if (el.hasAttribute('data-i18n')) {
      const key = el.getAttribute('data-i18n');
      const val = resolveKey(resource, key);
      if (val !== undefined) {
        let params = null;
        try {
          const raw = el.getAttribute('data-i18n-params');
          if (raw) params = JSON.parse(raw);
        } catch (_) { /* ignore malformed JSON */ }
        el.textContent = interpolate(val, params);
      }
    }

    // placeholder
    if (el.hasAttribute('data-i18n-placeholder')) {
      const key = el.getAttribute('data-i18n-placeholder');
      const val = resolveKey(resource, key);
      if (val !== undefined) el.setAttribute('placeholder', val);
    }

    // title attribute
    if (el.hasAttribute('data-i18n-title')) {
      const key = el.getAttribute('data-i18n-title');
      const val = resolveKey(resource, key);
      if (val !== undefined) el.setAttribute('title', val);
    }

    // aria-label
    if (el.hasAttribute('data-i18n-aria-label')) {
      const key = el.getAttribute('data-i18n-aria-label');
      const val = resolveKey(resource, key);
      if (val !== undefined) el.setAttribute('aria-label', val);
    }
  }

  async function applyAll(resource, root) {
    root = root || document.documentElement;

    // <title>
    const titleEl = root.querySelector('title');
    if (titleEl && titleEl.hasAttribute('data-i18n')) {
      const key = titleEl.getAttribute('data-i18n');
      const val = resolveKey(resource, key);
      if (val !== undefined) document.title = val;
    }

    // All elements with i18n attributes
    const els = root.querySelectorAll('[data-i18n], [data-i18n-placeholder], [data-i18n-title], [data-i18n-aria-label]');
    els.forEach(function (el) {
      applyToElement(el, resource);
    });
  }

  // ── Language switcher widget ─────────────────────────────────
  function renderSwitcher(container, currentCode) {
    if (!container) return;
    const languages = [
      { code: 'zh-CN', label: '简体中文' },
      { code: 'zh-TW', label: '繁體中文' },
      { code: 'en', label: 'English' },
      { code: 'ja', label: '日本語' },
      { code: 'ko', label: '한국어' },
      { code: 'fr', label: 'Français' },
      { code: 'de', label: 'Deutsch' },
      { code: 'es', label: 'Español' },
      { code: 'ru', label: 'Русский' },
      { code: 'ar', label: 'العربية' },
      { code: 'zh-lzh', label: '文言文' }
    ];
    var currentLabel = currentCode;
    for (var li = 0; li < languages.length; li++) {
      if (languages[li].code === currentCode) { currentLabel = languages[li].label; break; }
    }
    let html = '<div class="i18n-switcher dropdown">';
    html += '<button class="btn btn-sm btn-outline-secondary dropdown-toggle i18n-switcher-btn" type="button" data-bs-toggle="dropdown" aria-expanded="false">';
    html += '<i class="bi bi-translate me-1"></i><span class="i18n-current-label">' + currentLabel + '</span>';
    html += '</button>';
    html += '<ul class="dropdown-menu dropdown-menu-end" style="min-width:auto;">';
    languages.forEach(function (lang) {
      html += '<li><a class="dropdown-item i18n-lang-option' + (lang.code === currentCode ? ' active' : '') + '" href="#" data-lang="' + lang.code + '">' + lang.label + '</a></li>';
    });
    html += '</ul></div>';
    container.innerHTML = html;

    // Bind events
    container.querySelectorAll('.i18n-lang-option').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        var code = a.getAttribute('data-lang');
        if (code === getLang()) return;
        switchLanguage(code);
      });
    });
  }

  // ── Public API ───────────────────────────────────────────────
  function applyLangAttribute(code) {
    document.documentElement.lang = code;
  }

  async function switchLanguage(code) {
    setLang(code);
    applyLangAttribute(code);
    var resource = await loadResource(code);
    if (!resource) return;
    await applyAll(resource);
    var container = document.getElementById('i18n-switcher-container');
    if (container) renderSwitcher(container, code);
  }

  async function init() {
    var code = getLang();
    applyLangAttribute(code);
    var resource = await loadResource(code);
    if (!resource) return;

    // Apply strings
    await applyAll(resource);

    // Render the switcher in the navbar
    var container = document.getElementById('i18n-switcher-container');
    if (container) renderSwitcher(container, code);
  }

  // Expose to global
  // t(key, fallbackOrParams) — when the second arg is a string it's used as fallback;
  // when it's an object it's used for {placeholder} interpolation.
  function t(key, fallbackOrParams) {
    var resource = _cache[getLang()];
    var params = null;
    var fallback = key;
    if (fallbackOrParams !== undefined) {
      if (typeof fallbackOrParams === 'string') {
        fallback = fallbackOrParams;
      } else {
        params = fallbackOrParams;
      }
    }
    if (!resource) return fallback;
    var val = resolveKey(resource, key);
    if (val !== undefined) return interpolate(val, params);
    return params ? interpolate(fallback, params) : fallback;
  }

  window.HiveI18n = {
    init: init,
    switchLanguage: switchLanguage,
    getLang: getLang,
    t: t
  };
  // Short alias for use in inline JS
  window.__i18n = t;

  // Auto-init on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
