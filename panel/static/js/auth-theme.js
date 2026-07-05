(function () {
  const storageKey = 'hive_theme_mode';
  const modes = [
    { value: 'light', icon: 'bi-sun', key: 'theme.light', fallback: '日间模式' },
    { value: 'dark', icon: 'bi-moon-stars', key: 'theme.dark', fallback: '夜间模式' },
    { value: 'system', icon: 'bi-circle-half', key: 'theme.system', fallback: '跟随系统' }
  ];

  function translate(key, fallback) {
    return window.__i18n ? window.__i18n(key, fallback) : fallback;
  }

  function getMode() {
    return localStorage.getItem(storageKey) || 'dark';
  }

  function resolveTheme(mode) {
    if (mode === 'system') {
      return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }
    return mode === 'light' ? 'light' : 'dark';
  }

  function applyTheme(mode) {
    const theme = resolveTheme(mode);
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.setAttribute('data-bs-theme', theme);
  }

  function renderSwitcher(container) {
    const currentMode = getMode();
    const activeMode = modes.find((mode) => mode.value === currentMode) || modes[1];
    container.className = 'auth-theme-switcher';
    container.innerHTML = `
      <button class="auth-theme-button" type="button" aria-label="${translate('theme.title', '主题')}">
        <i class="bi ${activeMode.icon}"></i>
      </button>
      <div class="auth-theme-menu" role="menu">
        ${modes.map((mode) => `
          <button type="button" class="auth-theme-item ${mode.value === currentMode ? 'active' : ''}" data-theme-mode="${mode.value}" role="menuitem">
            <i class="bi ${mode.icon}"></i><span>${translate(mode.key, mode.fallback)}</span>
          </button>
        `).join('')}
      </div>
    `;
  }

  function initSwitcher() {
    const container = document.getElementById('authThemeSwitcher');
    if (!container) return;
    renderSwitcher(container);
    container.addEventListener('click', (event) => {
      const toggle = event.target.closest('.auth-theme-button');
      const item = event.target.closest('[data-theme-mode]');
      if (toggle) {
        container.classList.toggle('open');
        return;
      }
      if (item) {
        localStorage.setItem(storageKey, item.dataset.themeMode);
        applyTheme(item.dataset.themeMode);
        renderSwitcher(container);
      }
    });
    document.addEventListener('click', (event) => {
      if (!container.contains(event.target)) container.classList.remove('open');
    });
    document.addEventListener('hive:i18n-change', () => renderSwitcher(container));
  }

  applyTheme(getMode());
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
      if (getMode() === 'system') applyTheme('system');
    });
  }
  document.addEventListener('DOMContentLoaded', initSwitcher);
})();
