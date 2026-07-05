// ══════════════════════════════════════════════════════════════
// HiveDeploy 前端工具库 — Toast 通知、确认弹窗、步骤指引
// ══════════════════════════════════════════════════════════════

// ── Toast 通知 ────────────────────────────────────────────────
const HDT = {
  _container: null,

  _ensureContainer() {
    if (!this._container) {
      this._container = document.createElement('div');
      this._container.className = 'toast-container';
      document.body.appendChild(this._container);
    }
    return this._container;
  },

  /**
   * 显示一条通知
   * @param {string} msg      - 消息文本
   * @param {string} type     - 'info' | 'success' | 'warning' | 'error'
   * @param {number} duration - 自动消失毫秒数，0 为不自动消失
   */
  notify(msg, type = 'info', duration = 3500) {
    const container = this._ensureContainer();
    const icons = {
      info: 'bi-info-circle', success: 'bi-check-circle',
      warning: 'bi-exclamation-triangle', error: 'bi-x-circle',
    };
    const colors = {
      info: '#58a6ff', success: '#3fb950',
      warning: '#d29922', error: '#f85149',
    };

    const el = document.createElement('div');
    el.className = 'toast-item';
    el.innerHTML = `
      <span class="toast-icon" style="color:${colors[type] || colors.info}">
        <i class="bi ${icons[type] || icons.info}"></i>
      </span>
      <span class="toast-body">${msg}</span>
      <button class="toast-close" onclick="this.parentElement.remove()">
        <i class="bi bi-x"></i>
      </button>
    `;
    container.appendChild(el);

    if (duration > 0) {
      setTimeout(() => {
        el.classList.add('leaving');
        el.addEventListener('animationend', () => el.remove(), { once: true });
      }, duration);
    }
    return el;
  },

  // 快捷方法
  info(msg, d)    { return this.notify(msg, 'info', d); },
  success(msg, d) { return this.notify(msg, 'success', d); },
  warning(msg, d) { return this.notify(msg, 'warning', d); },
  error(msg, d)   { return this.notify(msg, 'error', d); },
};

// ── 复制到剪贴板 ──────────────────────────────────────────────
function hdWriteClipboard(text) {
  const value = String(text || '').trim();
  if (typeof navigator !== 'undefined' && navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(value);
  }
  return new Promise((resolve, reject) => {
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    textarea.style.top = '0';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
      const ok = document.execCommand('copy');
      textarea.remove();
      ok ? resolve() : reject(new Error('copy command failed'));
    } catch (err) {
      textarea.remove();
      reject(err);
    }
  });
}

function copyText(text, btnEl) {
  hdWriteClipboard(text).then(() => {
    HDT.success('已复制到剪贴板', 1500);
    if (btnEl) {
      const icon = btnEl.querySelector('i');
      if (icon) {
        const orig = icon.className;
        icon.className = icon.className.replace(/bi-[a-z0-9-]+/, 'bi-check2');
        setTimeout(() => { icon.className = orig; }, 1500);
      }
    }
  }).catch(() => {
    HDT.error('复制失败，请手动复制', 2000);
  });
}

// 兼容旧版调用
if (typeof window.copyText_legacy === 'undefined') {
  window.copyText_legacy = function(text) {
    hdWriteClipboard(text).then(() => {
      if (typeof event !== 'undefined' && event.target) {
        const el = event.target;
        const orig = el.className;
        el.className = el.className.replace('bi-copy','bi-check2');
        setTimeout(() => el.className = orig, 1500);
      }
    }).catch(() => HDT.error('复制失败，请手动复制', 2000));
  };
}

// ── 确认弹窗（替代原生 confirm） ──────────────────────────────
function hdConfirm(title, message, confirmText = '确认', cancelText = '取消', danger = false) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'custom-confirm-overlay';
    overlay.innerHTML = `
      <div class="custom-confirm-box">
        <h5 style="color:#e6edf3; margin-bottom:8px;">${title}</h5>
        <p style="color:#8b949e; font-size:.9rem; margin-bottom:20px;">${message}</p>
        <div class="d-flex gap-2 justify-content-end">
          <button class="btn btn-sm btn-outline-secondary cancel-btn">${cancelText}</button>
          <button class="btn btn-sm ${danger ? 'btn-danger' : 'btn-primary'} confirm-btn">${confirmText}</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    overlay.querySelector('.cancel-btn').onclick = () => {
      overlay.remove(); resolve(false);
    };
    overlay.querySelector('.confirm-btn').onclick = () => {
      overlay.remove(); resolve(true);
    };
    overlay.onclick = (e) => {
      if (e.target === overlay) { overlay.remove(); resolve(false); }
    };
  });
}

// ── 步骤指引（新手引导） ──────────────────────────────────────
const HDGuide = {
  _backdrop: null,
  _tooltip: null,
  _current: -1,
  _steps: [],
  _onComplete: null,

  /**
   * 启动步骤指引
   * @param {Array} steps - [{ target: 'css-selector', content: '说明文字',
   *                            place: 'bottom'|'top'|'left'|'right' }]
   * @param {Function} onComplete - 全部完成回调
   */
  start(steps, onComplete) {
    this._steps = steps;
    this._onComplete = onComplete;
    this._current = -1;

    // 创建遮罩（如果没有）
    if (!this._backdrop) {
      this._backdrop = document.createElement('div');
      this._backdrop.className = 'guide-backdrop';
      this._backdrop.onclick = () => this._skip();
      document.body.appendChild(this._backdrop);
    }
    this._backdrop.style.display = 'block';

    // 创建提示（如果没有）
    if (!this._tooltip) {
      this._tooltip = document.createElement('div');
      this._tooltip.className = 'guide-tooltip';
      this._tooltip.innerHTML = `
        <div class="guide-content"></div>
        <div class="d-flex justify-content-between align-items-center mt-2 gap-2">
          <small class="text-secondary guide-counter"></small>
          <div>
            <button class="btn btn-sm btn-outline-secondary guide-prev me-1"
                    style="font-size:.72rem; padding:.15rem .45rem;">上一步</button>
            <button class="btn btn-sm btn-primary guide-next"
                    style="font-size:.72rem; padding:.15rem .45rem;">下一步</button>
          </div>
        </div>
      `;
      document.body.appendChild(this._tooltip);

      this._tooltip.querySelector('.guide-prev').onclick = () => this._prev();
      this._tooltip.querySelector('.guide-next').onclick = () => this._next();
    }
    this._tooltip.style.display = 'block';

    this._next();
  },

  _showStep(idx) {
    if (idx < 0 || idx >= this._steps.length) {
      this._finish();
      return;
    }
    this._current = idx;
    const step = this._steps[idx];

    // 移除旧高亮
    document.querySelectorAll('.guide-highlight').forEach(el => {
      el.classList.remove('guide-highlight');
    });

    const target = document.querySelector(step.target);
    if (!target) {
      // 跳过不存在的元素
      this._next();
      return;
    }

    target.classList.add('guide-highlight');
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });

    // 更新提示内容
    this._tooltip.querySelector('.guide-content').textContent = step.content;
    this._tooltip.querySelector('.guide-counter').textContent =
      `${idx + 1} / ${this._steps.length}`;
    this._tooltip.querySelector('.guide-prev').style.display =
      idx === 0 ? 'none' : 'inline-block';
    this._tooltip.querySelector('.guide-next').textContent =
      idx === this._steps.length - 1 ? '完成' : '下一步';

    // 设置提示位置
    const place = step.place || 'bottom';
    this._tooltip.className = `guide-tooltip ${place}`;

    // 定位提示
    setTimeout(() => {
      const tRect = target.getBoundingClientRect();
      const tpRect = this._tooltip.getBoundingClientRect();
      let top = 0, left = 0;

      if (place === 'bottom') {
        top = tRect.bottom + 10;
        left = tRect.left + tRect.width / 2 - tpRect.width / 2;
      } else if (place === 'top') {
        top = tRect.top - tpRect.height - 10;
        left = tRect.left + tRect.width / 2 - tpRect.width / 2;
      } else if (place === 'right') {
        top = tRect.top + tRect.height / 2 - tpRect.height / 2;
        left = tRect.right + 10;
      } else if (place === 'left') {
        top = tRect.top + tRect.height / 2 - tpRect.height / 2;
        left = tRect.left - tpRect.width - 10;
      }

      // 边界约束
      if (left < 10) left = 10;
      if (left + tpRect.width > window.innerWidth - 10) left = window.innerWidth - tpRect.width - 10;
      if (top < 10) top = 10;
      if (top + tpRect.height > window.innerHeight - 10) top = window.innerHeight - tpRect.height - 10;

      this._tooltip.style.top = top + 'px';
      this._tooltip.style.left = left + 'px';
    }, 100);
  },

  _next() {
    this._showStep(this._current + 1);
  },

  _prev() {
    this._showStep(this._current - 1);
  },

  _skip() {
    this._finish();
  },

  _finish() {
    this._backdrop.style.display = 'none';
    this._tooltip.style.display = 'none';
    document.querySelectorAll('.guide-highlight').forEach(el => {
      el.classList.remove('guide-highlight');
    });
    if (this._onComplete) this._onComplete();
  },
};

// ── 加载状态按钮 ──────────────────────────────────────────────
function setBtnLoading(btn, loading = true, text = '处理中...') {
  const origText = btn.dataset.origText || btn.innerHTML;
  if (loading) {
    btn.dataset.origText = origText;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1" role="status"></span>${text}`;
  } else {
    btn.disabled = false;
    btn.innerHTML = btn.dataset.origText || origText;
  }
}

// ── 格式化字节 ─────────────────────────────────────────────────
function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

// ── 相对时间 ───────────────────────────────────────────────────
function timeAgo(dateStr) {
  const now = Date.now();
  const dt = new Date(dateStr + (dateStr.includes('+') ? '' : '+08:00'));
  const diff = now - dt.getTime();
  const mins = Math.floor(diff / 60000);
  const hrs = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);
  if (mins < 1) return '刚刚';
  if (mins < 60) return `${mins} 分钟前`;
  if (hrs < 24) return `${hrs} 小时前`;
  if (days < 30) return `${days} 天前`;
  return new Date(dateStr).toLocaleDateString('zh-CN');
}

// ── AJAX 封装（自动处理 CSRF / 错误提示） ────────────────────
async function hdFetch(url, options = {}) {
  const opts = { ...options };
  if (!opts.headers) opts.headers = {};
  if (opts.body instanceof FormData) {
    // 不设 Content-Type，让浏览器自动设 multipart
  } else if (typeof opts.body === 'string') {
    opts.headers['Content-Type'] = 'application/json';
  }
  try {
    const resp = await fetch(url, opts);
    if (resp.status === 401) {
      HDT.error('登录已过期，请重新登录', 3000);
      setTimeout(() => { window.location.href = '/login'; }, 2000);
      throw new Error('Unauthorized');
    }
    if (!resp.ok) {
      const text = await resp.text();
      let err = text;
      try { const j = JSON.parse(text); err = j.error || j.detail || text; } catch (_) {}
      throw new Error(err);
    }
    const contentType = resp.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      return await resp.json();
    }
    return resp;
  } catch (e) {
    if (e.message !== 'Unauthorized') {
      HDT.error(e.message || '请求失败', 4000);
    }
    throw e;
  }
}

// ── 页面加载完成初始化 ────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // 绑定所有 [data-copy] 元素
  document.querySelectorAll('[data-copy]').forEach(el => {
    el.addEventListener('click', () => copyText(el.dataset.copy, el));
  });

  // 绑定所有 [data-confirm] 表单
  document.querySelectorAll('form[data-confirm]').forEach(form => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const ok = await hdConfirm('确认操作', form.dataset.confirm || '确定执行此操作？');
      if (ok) form.submit();
    });
  });
});

// 导出到全局
window.HDT = HDT;
window.HDGuide = HDGuide;
window.copyText = copyText;
window.hdConfirm = hdConfirm;
/**
 * 动态样式初始化 — 将 data-style-* 属性转换为实际样式
 * 用于避免 Jinja2 {{ }} 在 style 属性中触发 VS Code CSS 解析器报错
 */
function initDynamicStyles() {
  document.querySelectorAll('[data-style-width]').forEach(el => {
    el.style.width = el.dataset.styleWidth;
  });
  document.querySelectorAll('[data-style-bg]').forEach(el => {
    el.style.background = el.dataset.styleBg;
  });
  document.querySelectorAll('[data-style-color]').forEach(el => {
    el.style.color = el.dataset.styleColor;
  });
  document.querySelectorAll('[data-style-border-left]').forEach(el => {
    el.style.borderLeft = el.dataset.styleBorderLeft;
  });
  document.querySelectorAll('[data-style-animation-delay]').forEach(el => {
    el.style.animationDelay = el.dataset.styleAnimationDelay;
  });
}

document.addEventListener('DOMContentLoaded', initDynamicStyles);

window.setBtnLoading = setBtnLoading;
window.fmtBytes = fmtBytes;
window.timeAgo = timeAgo;
window.hdFetch = hdFetch;
