/* ═══════════════════════════════════════════════════════════════
   AREAPULSE REALTIME — realtime.js
   Firebase Firestore listener · SLA breach detector · Live updates
   ═══════════════════════════════════════════════════════════════ */

'use strict';

const RealtimeEngine = (() => {
  let db = null, unsubscribe = null;
  const POLL_INTERVAL = 60000; // 60s polling fallback - reduced server load

  async function init(role, filterParams = {}) {
    // Try Firebase first; fallback to polling if unavailable
    try {
      const tokenRes = await fetch('/api/realtime-token');
      if (!tokenRes.ok) throw new Error('No realtime token');
      const { firebase_config } = await tokenRes.json();
      if (!firebase_config) throw new Error('No firebase config');

      // Dynamically import Firebase (CDN)
      const { initializeApp, getApps } = await import('https://www.gstatic.com/firebasejs/10.7.1/firebase-app.js');
      const { getFirestore, collection, onSnapshot, query, where, orderBy, limit } = await import('https://www.gstatic.com/firebasejs/10.7.1/firebase-firestore.js');

      const app = getApps().length ? getApps()[0] : initializeApp(firebase_config);
      db = getFirestore(app);

      const issuesRef = collection(db, 'issues');
      const q = query(issuesRef, where('status', '!=', 'resolved'), orderBy('timestamp', 'desc'), limit(200));

      unsubscribe = onSnapshot(q, (snapshot) => {
        snapshot.docChanges().forEach((change) => {
          const data = change.doc.data();
          if (change.type === 'added')    _onNewIssue(data);
          if (change.type === 'modified') _onIssueUpdated(change.doc.id, data);
        });
      }, (err) => {
        console.warn('[Realtime] Firestore error, falling back to polling:', err);
        startPolling(role);
      });

      console.log('[Realtime] Firebase listener active');
    } catch (e) {
      console.log('[Realtime] Firebase unavailable, using polling fallback:', e.message);
      startPolling(role);
    }

    // SLA breach checker — every 5 minutes
    setInterval(() => checkSLABreaches(role), 300000);
    setTimeout(() => checkSLABreaches(role), 15000); // initial check after 15s
  }

  function startPolling(role) {
    // Only poll on pages that have KPI tiles (dashboard)
    const hasDashboard = document.getElementById('kpi-open-count');
    if (!hasDashboard) return;
    const endpoint = `/${role}/api/issues?limit=50&_t=${Date.now()}`;
    setInterval(async () => {
      try {
        const res = await fetch(endpoint);
        if (!res.ok) return;
        const data = await res.json();
        _updateKPITiles(data);
        updateSLACountdowns();
      } catch {}
    }, POLL_INTERVAL);
  }

  function _onNewIssue(issue) {
    const role = document.body.classList.contains('gov-mode') ? 'gov' : 'ngo';
    Toast.info(`🆕 New ${issue.tag} report in ${issue.area}`);

    // Update KPI tile count
    const openCount = document.getElementById('kpi-open-count');
    if (openCount && openCount.dataset.counter !== undefined) {
      const current = parseInt(openCount.textContent.replace(/\D/g, '')) || 0;
      animateCounter(openCount, current + 1);
    }

    // Add to activity feed
    const feed = document.getElementById('activity-feed');
    if (feed) {
      const item = document.createElement('div');
      item.className = 'activity-item animate-fadeDown';
      item.innerHTML = `
        <div class="activity-icon" style="background:var(--blue-soft);color:var(--blue)">🆕</div>
        <div class="activity-text">New <strong>${issue.tag}</strong> report in <strong>${issue.area}</strong></div>
        <div class="activity-time">Just now</div>`;
      feed.prepend(item);
      if (feed.children.length > 20) feed.lastElementChild?.remove();
    }

    // Update notification badge
    _incrementNotifBadge();
  }

  function _onIssueUpdated(id, issue) {
    // Update any visible row in queue table
    const row = document.querySelector(`[data-issue-id="${id}"]`);
    if (row) {
      const statusPill = row.querySelector('.status-pill');
      if (statusPill) {
        statusPill.textContent = formatStatus(issue.status);
        statusPill.className   = `badge badge-${statusColor(issue.status)} status-pill`;
      }
      row.dataset.status = issue.status;
    }
    updateSLACountdowns();
    _updateKPITiles();
  }

  function _updateKPITiles(data) {
    // Trigger a KPI refresh by re-fetching
    const role = document.body.classList.contains('gov-mode') ? 'gov' : 'ngo';
    fetch(`/${role}/api/issues?status=open&limit=1`).then(r => r.json()).then(d => {
      const openCount = document.getElementById('kpi-open-count');
      if (openCount && d.total !== undefined) animateCounter(openCount, d.total);
    }).catch(() => {});
  }

  async function checkSLABreaches(role) {
    if (role !== 'gov') return;
    try {
      const data = await fetch('/gov/api/sla-data').then(r => r.json());
      const breached = (data.breached || []);
      if (breached.length > 0) {
        // Show persistent SLA breach banner
        let banner = document.getElementById('sla-breach-banner');
        if (!banner) {
          banner = document.createElement('div');
          banner.id = 'sla-breach-banner';
          banner.style.cssText = `
            position:fixed;top:56px;left:0;right:0;z-index:150;
            background:var(--red);color:#fff;
            padding:8px 24px;font-size:13px;font-weight:600;
            display:flex;align-items:center;justify-content:space-between;
            box-shadow:0 2px 8px rgba(0,0,0,.2);`;
          banner.innerHTML = `
            <span>🚨 <span id="breach-count">${breached.length}</span> SLA breach${breached.length > 1 ? 'es' : ''} detected — immediate action required</span>
            <div style="display:flex;gap:10px;align-items:center">
              <a href="/gov/sla" style="color:#fff;font-weight:700;text-decoration:underline">View SLA Board →</a>
              <button onclick="this.parentElement.parentElement.remove()" style="background:rgba(255,255,255,.2);border:none;color:#fff;padding:3px 8px;border-radius:6px;cursor:pointer">✕</button>
            </div>`;
          document.body.appendChild(banner);
          // Adjust main content top
          const main = document.querySelector('.main-content');
          if (main) main.style.paddingTop = 'calc(var(--topbar-h) + 40px)';
        } else {
          document.getElementById('breach-count').textContent = breached.length;
        }
        // Update notification badge
        const badge = document.querySelector('.topbar-notif-badge');
        if (badge) badge.textContent = Math.min(99, parseInt(badge.textContent || '0') + breached.length);
      }
    } catch {}
  }

  function _incrementNotifBadge() {
    const badge = document.querySelector('.topbar-notif-badge');
    if (badge) {
      badge.textContent = Math.min(99, parseInt(badge.textContent || '0') + 1);
      badge.style.animation = 'none';
      requestAnimationFrame(() => { badge.style.animation = 'scaleIn .3s ease'; });
    }
  }

  function stop() {
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
  }

  return { init, stop };
})();

window.RealtimeEngine = RealtimeEngine;