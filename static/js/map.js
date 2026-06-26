/* ═══════════════════════════════════════════════════════════════
   AREAPULSE MAP ENGINE — map.js
   Leaflet · MapTiler · SLA pins · Heatmap · Clustering
   ═══════════════════════════════════════════════════════════════ */

'use strict';

const MapEngine = (() => {
  const MAPTILER_KEY  = window.MAPTILER_KEY || '';
  const DELHI_CENTER  = [28.6139, 77.2090];
  const DEFAULT_ZOOM  = 11;

  const SLA_COLORS = {
    healthy:  '#3D6B52',
    at_risk:  '#C07018',
    critical: '#B83228',
    breached: '#9B2A6E',
  };
  const CAT_EMOJI = {
    pothole: '🕳', water: '💧', sewage: '🚰', electricity: '⚡',
    streetlight: '💡', garbage: '🗑', traffic: '🚦', noise: '🔊',
    tree: '🌳', other: '📍',
  };

  function calcSLAState(issue) {
    const SLA_H = { sewage:24, electricity:24, traffic:24, noise:24, water:48, streetlight:48, garbage:72, other:120, pothole:168, tree:168 };
    const slaH  = SLA_H[issue.tag] || 120;
    const elapsed = (Date.now() / 1000 - issue.timestamp) / 3600;
    const pct = elapsed / slaH;
    if (pct >= 1)    return 'breached';
    if (pct >= 0.75) return 'critical';
    if (pct >= 0.5)  return 'at_risk';
    return 'healthy';
  }

  function createSLAIcon(issue, slaState) {
    const color    = SLA_COLORS[slaState];
    const emoji    = CAT_EMOJI[issue.tag] || '📍';
    const isCrowd  = (issue.upvotes || 0) >= 25;
    const size     = Math.min(10 + (issue.upvotes || 0) / 3, 22);
    const glow     = slaState === 'breached' ? `animation: map-glow 1.2s ease-in-out infinite;` : '';

    const html = `
      <div style="
        position:relative;
        width:${28+size}px;height:${28+size}px;
        display:flex;align-items:center;justify-content:center;
      ">
        ${slaState === 'breached' || slaState === 'critical' ? `
          <div style="
            position:absolute;inset:-6px;border-radius:50%;
            background:${color}30;${glow}
          "></div>
        ` : ''}
        <div style="
          width:${20+size}px;height:${20+size}px;
          background:${color};
          border-radius:50% 50% 50% 0;
          transform:rotate(-45deg);
          border:2.5px solid #fff;
          box-shadow:0 2px 8px ${color}60;
          display:flex;align-items:center;justify-content:center;
        ">
          <div style="transform:rotate(45deg);font-size:${9+size/3}px;line-height:1">${emoji}</div>
        </div>
        ${isCrowd ? `<div style="position:absolute;top:-4px;right:-4px;font-size:12px">🔥</div>` : ''}
      </div>`;

    return L.divIcon({ html, className: '', iconSize: [28+size, 28+size], iconAnchor: [(14+size/2), 28+size], popupAnchor: [0, -(28+size)] });
  }

  function createIssuePopup(issue, slaState, role) {
    const elapsed = Math.floor((Date.now() / 1000 - issue.timestamp) / 3600);
    const SLA_H   = { sewage:24, electricity:24, traffic:24, noise:24, water:48, streetlight:48, garbage:72, other:120, pothole:168, tree:168 };
    const remaining = SLA_H[issue.tag] - elapsed;
    const statusColor = { open:'#2C5282', acknowledged:'#C47B2B', in_progress:'#C07018', resolved:'#3D6B52', escalated:'#B83228' };

    const govActions = role === 'gov' ? `
      <div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap">
        <button onclick="quickUpdate(${issue.id},'acknowledged',this)" style="padding:5px 10px;background:#E0EAF4;color:#1E3A5F;border:none;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer">✓ ACK</button>
        <button onclick="quickUpdate(${issue.id},'in_progress',this)" style="padding:5px 10px;background:#FEF0D0;color:#C07018;border:none;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer">▶ IN PROGRESS</button>
        <button onclick="quickUpdate(${issue.id},'resolved',this)"   style="padding:5px 10px;background:#E0EDE4;color:#3D6B52;border:none;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer">✅ DONE</button>
      </div>` : role === 'ngo' ? `
      <div style="margin-top:10px">
        <button onclick="openCommitModal(${issue.id})" style="width:100%;padding:7px;background:#3D6B52;color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer">🤝 Start Project</button>
      </div>` : '';

    return `
      <div style="width:240px;font-family:'DM Sans',sans-serif">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
          <span style="font-size:18px">${CAT_EMOJI[issue.tag]}</span>
          <div>
            <div style="font-size:12px;font-weight:700;color:#1A1208">${(issue.tag||'other').toUpperCase()}</div>
            <div style="font-size:11px;color:#8A7060">#AP-${issue.id} · ${issue.area}</div>
          </div>
          <div style="margin-left:auto;padding:3px 8px;background:${(statusColor[issue.status]||'#2C5282')}20;color:${statusColor[issue.status]||'#2C5282'};border-radius:999px;font-size:10px;font-weight:700">${(issue.status||'open').replace('_',' ').toUpperCase()}</div>
        </div>
        <p style="font-size:12px;color:#4A3520;line-height:1.5;margin-bottom:8px">${(issue.description||'').slice(0,100)}${(issue.description||'').length>100?'...':''}</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
          <span style="font-size:11px;background:#F0EDE6;padding:2px 7px;border-radius:999px;color:#8A7060">👍 ${issue.upvotes||0}</span>
          <span style="font-size:11px;background:${SLA_COLORS[slaState]}20;color:${SLA_COLORS[slaState]};padding:2px 7px;border-radius:999px;font-weight:700">
            ${slaState==='breached' ? `OVERDUE +${Math.abs(remaining)}h` : remaining > 0 ? `${remaining}h left` : 'BREACHED'}
          </span>
          <span style="font-size:11px;background:#F0EDE6;padding:2px 7px;border-radius:999px;color:#8A7060">${issue.severity||'?'}</span>
        </div>
        ${issue.image ? `<img src="${issue.image}" style="width:100%;height:80px;object-fit:cover;border-radius:8px;margin-bottom:8px" />` : ''}
        ${govActions}
        <a href="/${role}/issue/${issue.id}" style="display:block;text-align:center;font-size:11px;color:#8A7060;margin-top:8px">View full detail →</a>
      </div>`;
  }

  function initMap(containerId, options = {}) {
    const mapEl = document.getElementById(containerId);
    if (!mapEl) return null;

    const map = L.map(containerId, {
      center:    options.center || DELHI_CENTER,
      zoom:      options.zoom   || DEFAULT_ZOOM,
      zoomControl: false,
    });

    // MapTiler Voyager (warm, premium)
    const tileUrl = MAPTILER_KEY
      ? `https://api.maptiler.com/maps/hybrid/{z}/{x}/{y}.jpg?key=${MAPTILER_KEY}`
      : 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';

    L.tileLayer(tileUrl, {
      maxZoom: 19,
      attribution: MAPTILER_KEY ? '© <a href="https://www.maptiler.com/">MapTiler</a> © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' : '© OpenStreetMap contributors',
    }).addTo(map);

    // Custom zoom control
    L.control.zoom({ position: 'bottomright' }).addTo(map);

    // Attribution
    L.control.attribution({ prefix: false, position: 'bottomleft' }).addTo(map);

    return map;
  }

  // ── RENDER ISSUES ON MAP ────────────────────────────────────
  function renderIssues(map, issues, role = 'gov', existingLayerGroup = null) {
    const group = existingLayerGroup || L.layerGroup().addTo(map);
    group.clearLayers();

    const validIssues = (issues || []).filter(i => i.lat && i.lng && !isNaN(parseFloat(i.lat)) && !isNaN(parseFloat(i.lng)));
    
    if (!validIssues.length) {
      console.log('[MapEngine] No issues with coordinates to render');
      return group;
    }

    validIssues.forEach((issue, idx) => {
      const slaState = calcSLAState(issue);
      const icon     = createSLAIcon(issue, slaState);
      const marker   = L.marker([issue.lat, issue.lng], { icon });

      marker.bindPopup(createIssuePopup(issue, slaState, role), {
        maxWidth: 260, closeButton: false, className: 'areapulse-popup'
      });

      marker.on('mouseover', function() { this.openPopup(); });
      marker.on('click', function() { map.flyTo([issue.lat, issue.lng], 16, { duration: 0.8 }); });

      group.addLayer(marker);
    });

    // Auto-fit bounds to markers if map is small
    try {
      const bounds = group.getBounds();
      if (bounds.isValid() && validIssues.length > 1) {
        map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
      }
    } catch(e) {}

    return group;
  }

  // ── HEATMAP ─────────────────────────────────────────────────
  function toggleHeatmap(map, issues, heatLayer) {
    if (heatLayer) { map.removeLayer(heatLayer); return null; }
    if (!window.L.heatLayer) return null;
    const pts = issues.filter(i => i.lat && i.lng).map(i => {
      const intensity = { high: 1, medium: 0.6, low: 0.3 }[i.severity] || 0.5;
      return [i.lat, i.lng, intensity];
    });
    return L.heatLayer(pts, { radius: 25, blur: 15, gradient: { 0.4:'#3D6B52', 0.6:'#C07018', 0.8:'#B83228', 1.0:'#9B2A6E' } }).addTo(map);
  }

  // ── MINI MAP (embedded in page, 300px tall) ──────────────────
  function initMiniMap(containerId, issues, role = 'gov') {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.style.height = '300px';
    container.style.borderRadius = '12px';
    container.style.overflow = 'hidden';

    const map = initMap(containerId, { zoom: 11 });
    if (!map) return;
    renderIssues(map, issues, role);
    return map;
  }

  // ── WARD BOUNDARIES ─────────────────────────────────────────
  async function loadWardBoundaries(map) {
    // Simplified Delhi ward centroids for display
    const DELHI_AREAS = [
      { name: 'Connaught Place', lat: 28.6315, lng: 77.2167, health: 82 },
      { name: 'Karol Bagh',      lat: 28.6520, lng: 77.1904, health: 61 },
      { name: 'Rohini',          lat: 28.7493, lng: 77.1000, health: 74 },
      { name: 'Dwarka',          lat: 28.5921, lng: 77.0460, health: 79 },
      { name: 'Lajpat Nagar',    lat: 28.5700, lng: 77.2373, health: 68 },
      { name: 'Okhla',           lat: 28.5355, lng: 77.2785, health: 55 },
      { name: 'Vasant Kunj',     lat: 28.5200, lng: 77.1569, health: 85 },
      { name: 'Chandni Chowk',   lat: 28.6507, lng: 77.2334, health: 49 },
      { name: 'Saket',           lat: 28.5245, lng: 77.2066, health: 77 },
      { name: 'Pitampura',       lat: 28.7100, lng: 77.1279, health: 70 },
    ];
    const healthColor = (h) => h >= 80 ? '#3D6B52' : h >= 65 ? '#C07018' : h >= 50 ? '#B83228' : '#9B2A6E';
    DELHI_AREAS.forEach(area => {
      L.circleMarker([area.lat, area.lng], {
        radius: 20, color: '#fff', weight: 2,
        fillColor: healthColor(area.health), fillOpacity: .4,
      }).bindTooltip(`<b>${area.name}</b><br>Health: ${area.health}/100`).addTo(map);
    });
  }

  return { initMap, renderIssues, toggleHeatmap, initMiniMap, loadWardBoundaries, calcSLAState, SLA_COLORS };
})();

window.MapEngine = MapEngine;

// ── QUICK STATUS UPDATE (called from map popups) ─────────────
window.quickUpdate = async function(id, status, btn) {
  btn.textContent = '...';
  btn.disabled = true;
  try {
    await API.issues.updateStatus(id, status, '');
    Toast.success(`Issue #${id} → ${status.replace('_', ' ')}`);
    btn.textContent = '✓';
    btn.style.background = '#E0EDE4';
    btn.style.color = '#3D6B52';
  } catch (e) {
    btn.textContent = '✕';
    Toast.error('Update failed: ' + e.message);
  }
};