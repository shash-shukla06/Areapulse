/* ═══════════════════════════════════════════════════════════════
   AREAPULSE CORE — core.js
   Toast · Ripple · Tooltip · Theme · Command Palette · Sidebar
   ═══════════════════════════════════════════════════════════════ */

'use strict';

// ── TOAST SYSTEM ─────────────────────────────────────────────
const Toast = (() => {
  let container = null;
  const init = () => {
    if (container) return;
    container = document.getElementById('toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'toast-container';
      document.body.appendChild(container);
    }
  };
  const show = (message, type = 'info', duration = 3500) => {
    init();
    const icons = { success: '✓', error: '✕', info: 'ℹ', warning: '⚠' };
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `<span class="toast-icon">${icons[type] || icons.info}</span><span>${message}</span>`;
    toast.addEventListener('click', () => dismiss(toast));
    container.appendChild(toast);
    // Trigger animation
    requestAnimationFrame(() => {
      toast.style.animation = 'toast-in 320ms cubic-bezier(.34,1.56,.64,1) forwards';
    });
    const timer = setTimeout(() => dismiss(toast), duration);
    toast._timer = timer;
    return toast;
  };
  const dismiss = (toast) => {
    clearTimeout(toast._timer);
    toast.classList.add('removing');
    setTimeout(() => toast.remove(), 200);
  };
  return { show, success: (m,d) => show(m,'success',d), error: (m,d) => show(m,'error',d), info: (m,d) => show(m,'info',d), warning: (m,d) => show(m,'warning',d) };
})();

// ── RIPPLE EFFECT ─────────────────────────────────────────────
function addRipple(el) {
  if (!el) return;
  el.classList.add('ripple-container');
  el.addEventListener('click', function(e) {
    const existing = this.querySelector('.ripple-effect');
    if (existing) existing.remove();
    const ripple = document.createElement('span');
    ripple.className = 'ripple-effect';
    const rect = this.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height) * 2;
    ripple.style.width = ripple.style.height = `${size}px`;
    ripple.style.left = `${e.clientX - rect.left - size/2}px`;
    ripple.style.top  = `${e.clientY - rect.top  - size/2}px`;
    this.appendChild(ripple);
    setTimeout(() => ripple.remove(), 700);
  });
}
document.querySelectorAll('.btn').forEach(addRipple);

// ── ANIMATED COUNTER ─────────────────────────────────────────
function animateCounter(el, target, duration = 1200, prefix = '', suffix = '') {
  const start = performance.now();
  const startVal = parseInt(el.textContent.replace(/\D/g,'')) || 0;
  const update = (now) => {
    const elapsed = now - start;
    const progress = Math.min(elapsed / duration, 1);
    const ease = 1 - Math.pow(1 - progress, 3); // ease out cubic
    const current = Math.round(startVal + (target - startVal) * ease);
    el.textContent = prefix + current.toLocaleString('en-IN') + suffix;
    if (progress < 1) requestAnimationFrame(update);
  };
  requestAnimationFrame(update);
}
function initCounters() {
  document.querySelectorAll('[data-counter]').forEach(el => {
    const target = parseInt(el.dataset.counter);
    const prefix = el.dataset.prefix || '';
    const suffix = el.dataset.suffix || '';
    const obs = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) { animateCounter(el, target, 1200, prefix, suffix); obs.disconnect(); }
    }, { threshold: .3 });
    obs.observe(el);
  });
}

// ── REVEAL ANIMATIONS ────────────────────────────────────────
// Simple: CSS handles the animation, no JS needed since we fixed
// animations.css to not use opacity:0 as default state.
function initReveal() {
  // Just add visible class to everything immediately
  // The animation is defined in CSS and runs on page load
  document.querySelectorAll('.reveal').forEach(el => {
    el.classList.add('visible');
  });
}

// ── TOPBAR SCROLL EFFECT ─────────────────────────────────────
function initTopbarScroll() {
  const topbar = document.querySelector('.topbar');
  if (!topbar) return;
  let ticking = false;
  window.addEventListener('scroll', () => {
    if (ticking) return;
    requestAnimationFrame(() => {
      topbar.classList.toggle('scrolled', window.scrollY > 10);
      ticking = false;
    });
    ticking = true;
  });
}

// ── SIDEBAR ──────────────────────────────────────────────────
function initSidebar() {
  const sidebar  = document.querySelector('.sidebar');
  const mainContent = document.querySelector('.main-content');
  const topbar   = document.querySelector('.topbar');
  const collapseBtn = document.getElementById('sidebar-collapse-btn');
  const hamburger = document.getElementById('topbar-hamburger');
  if (!sidebar) return;

  const STORAGE_KEY = 'areapulse_sidebar_collapsed';
  const isCollapsed = localStorage.getItem(STORAGE_KEY) === 'true';
  if (isCollapsed) setCollapsed(true);

  function setCollapsed(val) {
    sidebar.classList.toggle('collapsed', val);
    if (mainContent) mainContent.classList.toggle('sidebar-collapsed', val);
    if (topbar) topbar.classList.toggle('sidebar-collapsed', val);
    localStorage.setItem(STORAGE_KEY, val);
    if (collapseBtn) {
      const icon = collapseBtn.querySelector('.collapse-icon');
      if (icon) icon.style.transform = val ? 'rotate(180deg)' : '';
    }
  }

  if (collapseBtn) collapseBtn.addEventListener('click', () => setCollapsed(!sidebar.classList.contains('collapsed')));
  if (hamburger) hamburger.addEventListener('click', () => {
    sidebar.classList.toggle('mobile-open');
  });
  // Tablet hover expand
  sidebar.addEventListener('mouseenter', () => { if (window.innerWidth >= 768 && window.innerWidth < 1024 && sidebar.classList.contains('collapsed')) { sidebar.classList.add('hover-expanded'); } });
  sidebar.addEventListener('mouseleave', () => sidebar.classList.remove('hover-expanded'));
}

// ── TOPBAR CLOCK ─────────────────────────────────────────────
function initClock() {
  const clockEl = document.getElementById('topbar-clock');
  if (!clockEl) return;
  const update = () => {
    const now = new Date();
    clockEl.textContent = now.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });
  };
  update();
  setInterval(update, 1000);
}

// ── COMMAND PALETTE ──────────────────────────────────────────
const CommandPalette = (() => {
  let backdrop, input, results;
  let items = [];
  let focusIdx = -1;

  const NAV_ITEMS = {
    gov: [
      { icon: '📊', label: 'Dashboard',      sub: 'Overview & KPIs',      url: '/gov/dashboard', group: 'Navigation' },
      { icon: '📋', label: 'Issue Queue',     sub: 'All active issues',    url: '/gov/queue', group: 'Navigation' },
      { icon: '⏱',  label: 'SLA Board',       sub: 'Kanban SLA tracker',   url: '/gov/sla', group: 'Navigation' },
      { icon: '🗺',  label: 'Live Map',        sub: 'Geo issue view',       url: '/gov/map', group: 'Navigation' },
      { icon: '📊', label: 'Analytics',       sub: 'Ward performance',     url: '/gov/analytics', group: 'Navigation' },
      { icon: '🏢', label: 'Departments',     sub: 'Dept management',      url: '/gov/departments', group: 'Navigation' },
      { icon: '🤝', label: 'NGO Partners',    sub: 'Coordination view',    url: '/gov/ngo-coordination', group: 'Navigation' },
      { icon: '📄', label: 'Reports',         sub: 'Export & generate',    url: '/gov/reports', group: 'Navigation' },
      { icon: '🤖', label: 'AI Copilot',      sub: 'AI assistant',         url: '/gov/ai-assistant', group: 'Navigation' },
      { icon: '🔔', label: 'Notifications',   sub: 'All alerts',           url: '/gov/notifications', group: 'Navigation' },
      { icon: '⚙',  label: 'Settings',        sub: 'Account & prefs',      url: '/gov/settings', group: 'Navigation' },
    ],
    ngo: [
      { icon: '📊', label: 'Dashboard',       sub: 'Impact overview',      url: '/ngo/dashboard', group: 'Navigation' },
      { icon: '🗺',  label: 'Opportunities',   sub: 'Matching issues',      url: '/ngo/opportunities', group: 'Navigation' },
      { icon: '📁', label: 'Active Projects',  sub: 'Current commitments',  url: '/ngo/projects', group: 'Navigation' },
      { icon: '💚', label: 'Impact Tracker',   sub: 'Metrics & proof',      url: '/ngo/impact', group: 'Navigation' },
      { icon: '🗺',  label: 'Live Map',         sub: 'Geo issue view',       url: '/ngo/map', group: 'Navigation' },
      { icon: '📊', label: 'Analytics',        sub: 'Area analysis',        url: '/ngo/analytics', group: 'Navigation' },
      { icon: '🤝', label: 'Gov Coordination', sub: 'Partnership view',     url: '/ngo/gov-coordination', group: 'Navigation' },
      { icon: '📄', label: 'Reports',          sub: 'Impact reports',       url: '/ngo/reports', group: 'Navigation' },
      { icon: '🤖', label: 'AI Assistant',     sub: 'Smart recommendations',url: '/ngo/ai-assistant', group: 'Navigation' },
      { icon: '🔔', label: 'Notifications',    sub: 'All alerts',           url: '/ngo/notifications', group: 'Navigation' },
      { icon: '⚙',  label: 'Settings',         sub: 'Account & prefs',      url: '/ngo/settings', group: 'Navigation' },
    ]
  };

  const init = () => {
    backdrop = document.getElementById('cmdpal-backdrop');
    input    = document.getElementById('cmdpal-input');
    results  = document.getElementById('cmdpal-results');
    if (!backdrop) return;

    const role = document.body.classList.contains('gov-mode') ? 'gov' : 'ngo';
    items = NAV_ITEMS[role] || NAV_ITEMS.gov;

    document.getElementById('topbar-search')?.addEventListener('click', open);
    document.getElementById('sidebar-search')?.addEventListener('click', open);
    document.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); open(); }
      if (e.key === '/' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) { e.preventDefault(); open(); }
    });
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
    input?.addEventListener('input', search);
    input?.addEventListener('keydown', handleKeydown);
  };

  const open = () => {
    if (!backdrop) return;
    backdrop.classList.add('open');
    input?.focus();
    render(items);
  };
  const close = () => {
    backdrop?.classList.remove('open');
    if (input) input.value = '';
    focusIdx = -1;
  };
  const search = () => {
    const q = input.value.toLowerCase().trim();
    if (!q) { render(items); return; }
    const filtered = items.filter(i => i.label.toLowerCase().includes(q) || i.sub.toLowerCase().includes(q));
    render(filtered.length ? filtered : [{ icon: '🔍', label: `Search: "${input.value}"`, sub: 'No results found', url: null, group: 'Results' }]);
  };
  const render = (list) => {
    if (!results) return;
    results.innerHTML = '';
    focusIdx = -1;
    const grouped = {};
    list.forEach(i => { (grouped[i.group] = grouped[i.group] || []).push(i); });
    Object.entries(grouped).forEach(([group, groupItems]) => {
      const label = document.createElement('div');
      label.className = 'cmdpal-group-label';
      label.textContent = group;
      results.appendChild(label);
      groupItems.forEach((item, idx) => {
        const el = document.createElement('div');
        el.className = 'cmdpal-item';
        el.innerHTML = `<span class="cmdpal-item-icon">${item.icon}</span><div><div class="cmdpal-item-text">${item.label}</div><div class="cmdpal-item-sub">${item.sub}</div></div>`;
        el.addEventListener('click', () => { if (item.url) window.location.href = item.url; close(); });
        results.appendChild(el);
      });
    });
  };
  const handleKeydown = (e) => {
    const itemEls = results?.querySelectorAll('.cmdpal-item') || [];
    if (e.key === 'ArrowDown') { e.preventDefault(); focusIdx = Math.min(focusIdx + 1, itemEls.length - 1); updateFocus(itemEls); }
    if (e.key === 'ArrowUp')   { e.preventDefault(); focusIdx = Math.max(focusIdx - 1, 0); updateFocus(itemEls); }
    if (e.key === 'Enter' && focusIdx >= 0) itemEls[focusIdx]?.click();
    if (e.key === 'Escape') close();
  };
  const updateFocus = (itemEls) => {
    itemEls.forEach((el, i) => el.classList.toggle('focused', i === focusIdx));
    itemEls[focusIdx]?.scrollIntoView({ block: 'nearest' });
  };
  return { init, open, close };
})();

// ── DROPDOWN MENUS ────────────────────────────────────────────
function initDropdowns() {
  document.querySelectorAll('.dropdown').forEach(dropdown => {
    const trigger = dropdown.querySelector('.dropdown-trigger');
    if (!trigger) return;
    trigger.addEventListener('click', (e) => { e.stopPropagation(); dropdown.classList.toggle('open'); });
  });
  document.addEventListener('click', () => document.querySelectorAll('.dropdown.open').forEach(d => d.classList.remove('open')));
}

// ── THEME TOGGLE ──────────────────────────────────────────────
function initTheme() {
  // Always light mode — dark mode removed for consistency
  document.body.classList.remove('dark-mode');
  localStorage.removeItem('areapulse_theme');
  window.setTheme = (theme) => {
    // Dark mode disabled
  };
}

// ── MODAL SYSTEM ──────────────────────────────────────────────
const Modal = (() => {
  const _listeners = {};
  const open = (id) => {
    const backdrop = document.getElementById(id);
    if (!backdrop) return;
    // Remove any stale listener before adding new one
    if (_listeners[id]) backdrop.removeEventListener('click', _listeners[id]);
    _listeners[id] = (e) => { if (e.target === backdrop) close(id); };
    backdrop.addEventListener('click', _listeners[id]);
    backdrop.style.display = 'flex';
    requestAnimationFrame(() => backdrop.classList.add('open'));
    document.addEventListener('keydown', function esc(e) {
      if (e.key === 'Escape') { close(id); document.removeEventListener('keydown', esc); }
    });
  };
  const close = (id) => {
    const backdrop = document.getElementById(id);
    if (!backdrop) return;
    backdrop.classList.remove('open');
    setTimeout(() => { backdrop.style.display = 'none'; }, 220);
  };
  return { open, close };
})();

// ── DRAWER SYSTEM ─────────────────────────────────────────────
const Drawer = (() => {
  // Track backdrop listeners so we never add duplicates
  const _listeners = {};

  const open = (id) => {
    const drawer   = document.getElementById(id);
    const backdrop = document.getElementById(id + '-backdrop');
    if (!drawer) return;

    // Show backdrop — it starts display:none in CSS
    if (backdrop) {
      backdrop.style.display = 'block';
      // Remove any existing listener first to prevent stacking
      if (_listeners[id]) {
        backdrop.removeEventListener('click', _listeners[id]);
      }
      _listeners[id] = () => close(id);
      backdrop.addEventListener('click', _listeners[id]);
      requestAnimationFrame(() => backdrop.classList.add('open'));
    }

    drawer.classList.add('open');
    // DO NOT set body overflow hidden — it freezes the page
    // The drawer is a side panel, page can stay scrollable
  };

  const close = (id) => {
    const drawer   = document.getElementById(id);
    const backdrop = document.getElementById(id + '-backdrop');
    if (drawer)   drawer.classList.remove('open');
    if (backdrop) {
      backdrop.classList.remove('open');
      setTimeout(() => { backdrop.style.display = 'none'; }, 220);
    }
    // Restore body scroll in case anything set it
    document.body.style.overflow = '';
  };
  return { open, close };
})();

// ── TABS ─────────────────────────────────────────────────────
function initTabs(containerSelector) {
  document.querySelectorAll(containerSelector || '.drawer-tabs').forEach(tabContainer => {
    const tabs    = tabContainer.querySelectorAll('.drawer-tab');
    const panelId = tabContainer.dataset.panels;
    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        if (panelId) {
          const panels = document.querySelectorAll(`[data-panel-group="${panelId}"]`);
          panels.forEach(p => p.classList.toggle('hidden', p.dataset.panel !== tab.dataset.tab));
        }
      });
    });
  });
}

// ── SLA COUNTDOWN UPDATE ─────────────────────────────────────
function updateSLACountdowns() {
  document.querySelectorAll('[data-sla-deadline]').forEach(el => {
    const deadline = parseFloat(el.dataset.slaDeadline) * 1000;
    const now = Date.now();
    const diff = deadline - now;
    const el2  = el.querySelector('.sla-time') || el;

    if (diff <= 0) {
      const hours = Math.abs(Math.floor(diff / 3600000));
      el2.textContent = `OVERDUE +${hours}h`;
      el.className = el.className.replace(/sla-\w+/g, '') + ' sla-breached';
    } else {
      const days  = Math.floor(diff / 86400000);
      const hours = Math.floor((diff % 86400000) / 3600000);
      const mins  = Math.floor((diff % 3600000) / 60000);
      const totalH = diff / 3600000;
      let text, cls;
      if (days > 2) { text = `${days}d ${hours}h`; cls = 'sla-healthy'; }
      else if (totalH > 6) { text = `${days > 0 ? days+'d ' : ''}${hours}h ${mins}m`; cls = 'sla-at-risk'; }
      else { text = `${hours}h ${mins}m`; cls = 'sla-critical'; }
      el2.textContent = text;
      el.className = el.className.replace(/sla-\w+/g, '') + ' ' + cls;
    }
  });
}
setInterval(updateSLACountdowns, 60000);

// ── AUTO-SAVE SETTINGS ────────────────────────────────────────
function initAutoSave() {
  document.querySelectorAll('[data-autosave]').forEach(input => {
    let timer;
    const handler = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const key = input.dataset.autosave;
        const val = input.type === 'checkbox' ? input.checked : input.value;
        try {
          const res = await fetch('/api/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ key, value: val }) });
          if (res.ok) Toast.success('Settings saved');
        } catch { Toast.error('Save failed'); }
      }, 600);
    };
    input.addEventListener('change', handler);
    if (input.type !== 'checkbox') input.addEventListener('input', handler);
  });
}

// ── INIT ALL ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initCounters();
  initReveal();
  initTopbarScroll();
  initSidebar();
  initClock();
  CommandPalette.init();
  initDropdowns();
  initTheme();
  initTabs();
  initAutoSave();
  updateSLACountdowns();
  document.querySelectorAll('.btn').forEach(addRipple);
});

// Expose globals
window.Toast       = Toast;
window.Modal       = Modal;
window.Drawer      = Drawer;
window.CommandPalette = CommandPalette;
window.animateCounter = animateCounter;