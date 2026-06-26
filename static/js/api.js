/* ═══════════════════════════════════════════════════════════════
   AREAPULSE API CLIENT — api.js
   Fetch wrappers · Optimistic UI · Error handling
   ═══════════════════════════════════════════════════════════════ */

'use strict';

const API = (() => {
  const BASE = '';
  const DEFAULT_HEADERS = { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' };

  async function request(method, path, data = null) {
    const opts = { method, headers: { ...DEFAULT_HEADERS } };
    if (data) opts.body = JSON.stringify(data);
    try {
      const res  = await fetch(BASE + path, opts);
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json.error || json.message || `HTTP ${res.status}`);
      return json;
    } catch (err) {
      console.error(`[API] ${method} ${path}:`, err);
      throw err;
    }
  }

  return {
    get:    (path)        => request('GET',    path),
    post:   (path, data)  => request('POST',   path, data),
    put:    (path, data)  => request('PUT',    path, data),
    delete: (path)        => request('DELETE', path),

    // ── ISSUES ──────────────────────────────────────────────
    issues: {
      list: (params = {}) => {
        const q = new URLSearchParams(params).toString();
        return request('GET', `/gov/api/issues${q ? '?'+q : ''}`);
      },
      get:        (id)            => request('GET',  `/gov/api/issues/${id}`),
      updateStatus:(id, status, note) => request('POST', `/gov/update-status`, { id, status, note }),
      bulkUpdate: (ids, status)   => request('POST', `/gov/bulk-update`, { ids, status }),
      upvote:     (id)            => request('POST', `/issue/${id}/upvote`),
      escalate:   (id, reason)    => request('POST', `/gov/api/escalate`,   { id, reason }),
      deescalate: (id, note)      => request('POST', `/gov/api/deescalate`, { id, note }),
    },

    // ── SLA ─────────────────────────────────────────────────
    sla: {
      data: () => request('GET', '/gov/api/sla-data'),
    },

    // ── AI ──────────────────────────────────────────────────
    ai: {
      govChat: (message, history) => request('POST', '/gov/ai-chat', { message, history }),
      ngoChat: (message, history) => request('POST', '/ngo/ai-chat', { message, history }),
      govBriefing: ()             => request('POST', '/gov/api/ai-briefing', {}),
      ngoRecommend: ()            => request('GET',  '/ngo/api/ai-recommend'),
    },

    // ── NGO ─────────────────────────────────────────────────
    ngo: {
      opportunities: (params = {}) => {
        const q = new URLSearchParams(params).toString();
        return request('GET', `/ngo/api/opportunities${q ? '?'+q : ''}`);
      },
      commit:       (data)      => request('POST', '/ngo/commit',          data),
      deescalate:   (id, note)  => request('POST', '/ngo/api/deescalate',  { id, note }),
      impact:     ()       => request('GET',  '/ngo/api/impact-data'),
      projects:   ()       => request('GET',  '/ngo/api/projects'),
    },

    // ── NOTIFICATIONS ────────────────────────────────────────
    notifications: {
      list: (role, page = 1) => request('GET', `/${role}/api/notifications?page=${page}`),
      markRead:   (id, role) => request('POST', `/${role}/api/notifications/${id}/read`),
      markAllRead: (role)    => request('POST', `/${role}/api/notifications/read-all`),
    },

    // ── WHATSAPP ─────────────────────────────────────────────
    whatsapp: {
      send: (phone, message) => request('POST', '/gov/api/send-whatsapp', { phone, message }),
    },

    // ── EXPORT ──────────────────────────────────────────────
    export: {
      csvUrl:   (params = {}) => `/gov/api/export-csv?${new URLSearchParams(params)}`,
      pdfUrl:   (type)        => `/gov/api/export-pdf?type=${type}`,
      impactPdf: ()           => '/ngo/api/export-impact-pdf',
    },

    // ── HEALTH ──────────────────────────────────────────────
    health: () => request('GET', '/api/health'),

    // ── SETTINGS ────────────────────────────────────────────
    settings: {
      save: (key, value) => request('POST', '/api/settings', { key, value }),
    },
  };
})();

// ── STATUS UPDATE WITH OPTIMISTIC UI ─────────────────────────
async function updateIssueStatus(issueId, newStatus, rowEl) {
  const oldStatus = rowEl?.dataset.status;
  const statusPill = rowEl?.querySelector('.status-pill');

  // Optimistic update
  if (statusPill) {
    statusPill.textContent = formatStatus(newStatus);
    statusPill.className = `badge badge-${statusColor(newStatus)} status-pill`;
  }
  if (rowEl) rowEl.dataset.status = newStatus;

  try {
    await API.issues.updateStatus(issueId, newStatus, '');
    Toast.success(`Issue ${issueId} → ${formatStatus(newStatus)}`);
    // Refresh SLA countdowns
    updateSLACountdowns();
  } catch (err) {
    // Rollback
    if (statusPill && oldStatus) {
      statusPill.textContent = formatStatus(oldStatus);
      statusPill.className = `badge badge-${statusColor(oldStatus)} status-pill`;
    }
    if (rowEl) rowEl.dataset.status = oldStatus;
    Toast.error(`Failed to update: ${err.message}`);
  }
}

function formatStatus(s) {
  const map = { open:'Open', acknowledged:'Acknowledged', in_progress:'In Progress', resolved:'Resolved', escalated:'Escalated' };
  return map[s] || s;
}
function statusColor(s) {
  const map = { open:'blue', acknowledged:'honey', in_progress:'amber', resolved:'green', escalated:'red' };
  return map[s] || 'blue';
}

// ── STREAMING AI RESPONSE ────────────────────────────────────
async function streamAIResponse(endpoint, payload, onChunk, onDone) {
  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (!res.body) {
      // Fallback for non-streaming
      const data = await res.json();
      onChunk(data.response || data.answer || '');
      onDone?.();
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      // Parse SSE: "data: {...}\n\n"
      chunk.split('\n').forEach(line => {
        if (line.startsWith('data: ')) {
          try { const d = JSON.parse(line.slice(6)); onChunk(d.content || d.chunk || ''); }
          catch { onChunk(line.slice(6)); }
        }
      });
    }
    onDone?.();
  } catch (err) {
    Toast.error('AI request failed: ' + err.message);
    onDone?.();
  }
}

window.API = API;
window.updateIssueStatus = updateIssueStatus;
window.streamAIResponse  = streamAIResponse;
window.formatStatus = formatStatus;
window.statusColor  = statusColor;