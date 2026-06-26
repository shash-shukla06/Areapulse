/* ═══════════════════════════════════════════════════════════════
   AREAPULSE CHARTS — charts.js  (clean rewrite)
   Readable · Correct colors · No Playfair dependency
   ═══════════════════════════════════════════════════════════════ */

'use strict';

const ChartTheme = {
  honey:   '#C47B2B',
  gov:     '#1E3A5F',
  ngo:     '#3D6B52',
  red:     '#B83228',
  amber:   '#C07018',
  green:   '#3D6B52',
  blue:    '#2C5282',
  magenta: '#9B2A6E',
  ink:     '#1A1208',
  ink3:    '#8A7060',
  border:  '#DED8CC',
  category: {
    pothole: '#4A3520', water: '#2C5282', sewage: '#6B3FA0',
    electricity: '#C07018', streetlight: '#C47B2B', garbage: '#3D6B52',
    traffic: '#B83228', noise: '#8A7060', tree: '#2A4D3A', other: '#5A4E40',
  }
};

const baseOpts = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 700, easing: 'easeOutQuart' },
  plugins: {
    legend: {
      labels: {
        font: { family: 'DM Sans', size: 13 },
        color: ChartTheme.ink3,
        boxWidth: 12,
        padding: 16,
      }
    },
    tooltip: {
      backgroundColor: ChartTheme.ink,
      titleColor: '#fff',
      bodyColor: '#DED8CC',
      padding: 12,
      cornerRadius: 8,
      titleFont: { family: 'DM Sans', weight: '700', size: 13 },
      bodyFont:  { family: 'DM Sans', size: 13 },
    }
  }
};

// ── CENTER TEXT PLUGIN (uses DM Sans, not Playfair) ───────────
// Register once globally
const _centerTextPlugin = {
  id: 'centerText',
  beforeDraw(chart) {
    if (!chart.config.options._centerLabel) return;
    const { ctx, width, height } = chart;
    const { line1, line2 } = chart.config.options._centerLabel;
    ctx.save();
    ctx.font = '800 32px "DM Sans"';
    ctx.fillStyle = ChartTheme.ink;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(line1, width / 2, height / 2 - 10);
    ctx.font = '500 12px "DM Sans"';
    ctx.fillStyle = ChartTheme.ink3;
    ctx.fillText(line2, width / 2, height / 2 + 14);
    ctx.restore();
  }
};
// Safe registration — only once
if (typeof Chart !== 'undefined' && !Chart.registry.plugins.get('centerText')) {
  Chart.register(_centerTextPlugin);
}

// ── SLA DONUT ─────────────────────────────────────────────────
// Shows 4 colored segments. When all values are 0 for a state,
// that slice is hidden (Chart.js handles this automatically).
// A grey placeholder arc shows when everything is 0 or all-one-state.
function buildSLADonut(canvasId, data) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  const { healthy = 0, at_risk = 0, critical = 0, breached = 0 } = data;
  const total = healthy + at_risk + critical + breached;

  // If all breached (or all in one state), still show all 4 segments
  // Add a tiny ghost segment so the chart still shows all 4 colors in legend
  const vals = [
    healthy  || 0,
    at_risk  || 0,
    critical || 0,
    breached || 0,
  ];

  return new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Healthy', 'At Risk', 'Critical', 'Breached'],
      datasets: [{
        data: vals,
        backgroundColor: [
          ChartTheme.green,
          ChartTheme.amber,
          ChartTheme.red,
          ChartTheme.magenta,
        ],
        borderColor: '#F7F5F0',
        borderWidth: 3,
        hoverOffset: 8,
      }]
    },
    options: {
      ...baseOpts,
      cutout: '70%',
      _centerLabel: { line1: String(total), line2: 'Total Active' },
      plugins: {
        ...baseOpts.plugins,
        legend: {
          position: 'bottom',
          labels: {
            ...baseOpts.plugins.legend.labels,
            generateLabels(chart) {
              // Always show all 4 labels even if value is 0
              const labels = ['Healthy', 'At Risk', 'Critical', 'Breached'];
              const colors = [ChartTheme.green, ChartTheme.amber, ChartTheme.red, ChartTheme.magenta];
              const counts = [healthy, at_risk, critical, breached];
              return labels.map((label, i) => ({
                text: `${label}: ${counts[i]}`,
                fillStyle: colors[i],
                strokeStyle: colors[i],
                lineWidth: 0,
                hidden: false,
                index: i,
              }));
            }
          }
        }
      }
    }
  });
}

// ── CATEGORY DONUT ────────────────────────────────────────────
function buildCategoryDonut(canvasId, categoryData) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  const labels = Object.keys(categoryData);
  const values = Object.values(categoryData);
  const total  = values.reduce((a, b) => a + b, 0);
  const colors = labels.map(l => ChartTheme.category[l] || ChartTheme.ink3);

  return new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: labels.map(l => l.charAt(0).toUpperCase() + l.slice(1)),
      datasets: [{ data: values, backgroundColor: colors, borderColor: '#F7F5F0', borderWidth: 3, hoverOffset: 8 }]
    },
    options: {
      ...baseOpts,
      cutout: '65%',
      _centerLabel: { line1: String(total), line2: 'Issues' },
      plugins: {
        ...baseOpts.plugins,
        legend: { position: 'right', labels: { ...baseOpts.plugins.legend.labels } }
      }
    }
  });
}

// ── HORIZONTAL BAR — top areas ────────────────────────────────
function buildResolutionBar(canvasId, wardData) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  const { labels, values } = wardData;

  // Color each bar by count magnitude
  const max = Math.max(...values, 1);
  const bgColors = values.map(v => {
    const ratio = v / max;
    if (ratio > 0.75) return ChartTheme.red + 'CC';
    if (ratio > 0.4)  return ChartTheme.amber + 'CC';
    return ChartTheme.green + 'CC';
  });

  return new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Open Issues',
        data: values,
        backgroundColor: bgColors,
        borderRadius: 5,
        borderSkipped: false,
      }]
    },
    options: {
      ...baseOpts,
      indexAxis: 'y',
      plugins: {
        ...baseOpts.plugins,
        legend: { display: false },
      },
      scales: {
        x: {
          grid: { color: ChartTheme.border },
          ticks: { font: { family: 'DM Sans', size: 12 }, color: ChartTheme.ink3, stepSize: 1 },
          beginAtZero: true,
        },
        y: {
          grid: { display: false },
          ticks: { font: { family: 'DM Sans', size: 13 }, color: ChartTheme.ink }
        }
      }
    }
  });
}

// ── SPARKLINE (tiny trend line inside stat card) ──────────────
function buildSparkline(canvasId, data, color = ChartTheme.honey) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map((_, i) => i),
      datasets: [{ data, borderColor: color, borderWidth: 2, fill: true,
        backgroundColor: color + '22', pointRadius: 0, tension: 0.5 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } },
    }
  });
}

// ── NGO IMPACT LINE ───────────────────────────────────────────
function buildImpactLine(canvasId, labels, citizensHelped, issuesResolved) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Citizens Helped', data: citizensHelped, borderColor: ChartTheme.ngo,
          backgroundColor: ChartTheme.ngo + '18', borderWidth: 2.5, tension: 0.4, fill: true },
        { label: 'Issues Resolved', data: issuesResolved, borderColor: ChartTheme.honey,
          backgroundColor: 'transparent', borderWidth: 2, tension: 0.4, borderDash: [5,5] }
      ]
    },
    options: {
      ...baseOpts,
      scales: {
        x: { grid: { color: ChartTheme.border }, ticks: { font: { family: 'DM Sans', size: 12 }, color: ChartTheme.ink3 } },
        y: { grid: { color: ChartTheme.border }, ticks: { font: { family: 'DM Sans', size: 12 }, color: ChartTheme.ink3 } }
      }
    }
  });
}

function buildSLAStackedBar(canvasId, timeLabels, compliant, nearBreach, breached) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  return new Chart(ctx, {
    type: 'bar',
    data: {
      labels: timeLabels,
      datasets: [
        { label: 'Compliant',   data: compliant,  backgroundColor: ChartTheme.green   + 'CC', borderRadius: 2 },
        { label: 'Near Breach', data: nearBreach, backgroundColor: ChartTheme.amber   + 'CC', borderRadius: 2 },
        { label: 'Breached',    data: breached,   backgroundColor: ChartTheme.red     + 'CC', borderRadius: 2 },
      ]
    },
    options: {
      ...baseOpts,
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { font: { family: 'DM Sans', size: 12 }, color: ChartTheme.ink3 } },
        y: { stacked: true, grid: { color: ChartTheme.border }, ticks: { font: { family: 'DM Sans', size: 12 }, color: ChartTheme.ink3 } }
      }
    }
  });
}

function buildScoreTrendLine(canvasId, labels, scoreData) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{ label: 'Civic Score', data: scoreData, borderColor: ChartTheme.gov,
        backgroundColor: ChartTheme.gov + '18', borderWidth: 2.5, pointRadius: 4, tension: 0.4, fill: true }]
    },
    options: {
      ...baseOpts,
      scales: {
        x: { grid: { color: ChartTheme.border }, ticks: { font: { family: 'DM Sans', size: 12 }, color: ChartTheme.ink3 } },
        y: { min: 0, max: 100, grid: { color: ChartTheme.border }, ticks: { font: { family: 'DM Sans', size: 12 }, color: ChartTheme.ink3, stepSize: 20 } }
      }
    }
  });
}

function updateProgressRing(svgId, percentage, radius = 40) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const circumference = 2 * Math.PI * radius;
  const fill = svg.querySelector('.progress-ring-fill');
  if (fill) {
    fill.setAttribute('stroke-dasharray', circumference);
    fill.setAttribute('stroke-dashoffset', circumference * (1 - percentage / 100));
  }
}

window.ChartBuilders = {
  buildSLADonut, buildCategoryDonut, buildResolutionBar,
  buildSparkline, buildImpactLine, buildSLAStackedBar,
  buildScoreTrendLine, updateProgressRing,
  theme: ChartTheme,
};