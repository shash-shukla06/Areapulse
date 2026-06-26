/* ═══════════════════════════════════════════════════════════════
   AREAPULSE EXPORT ENGINE — export.js
   Client-side CSV · Server PDF trigger · Print helpers
   ═══════════════════════════════════════════════════════════════ */

'use strict';

const ExportEngine = (() => {

  // ── CSV EXPORT ───────────────────────────────────────────────
  function exportCSV(data, filename = 'areapulse-issues.csv') {
    if (!data || !data.length) { Toast.warning('No data to export'); return; }

    const cols = ['id','area','tag','description','severity','status','upvotes','timestamp'];
    const headers = ['Ticket ID','Area','Category','Description','Severity','Status','Upvotes','Filed At'];
    const rows = data.map(row =>
      cols.map(c => {
        let v = row[c] || '';
        if (c === 'timestamp' && v) v = new Date(v * 1000).toLocaleString('en-IN');
        if (c === 'id') v = `AP-${v}`;
        return `"${String(v).replace(/"/g, '""')}"`;
      }).join(',')
    );

    const csv = [headers.join(','), ...rows].join('\r\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    Toast.success(`Exported ${data.length} issues to CSV`);
  }

  // ── SERVER-SIDE PDF TRIGGER ──────────────────────────────────
  function downloadPDF(type = 'summary', params = {}) {
    const role = document.body.classList.contains('gov-mode') ? 'gov' : 'ngo';
    const url  = role === 'gov'
      ? `/gov/api/export-pdf?type=${type}&${new URLSearchParams(params)}`
      : `/ngo/api/export-impact-pdf?${new URLSearchParams(params)}`;

    Toast.info('Generating PDF report...');
    const a = document.createElement('a');
    a.href  = url;
    a.target = '_blank';
    a.click();
  }

  // ── PRINT STYLESHEET INJECT ──────────────────────────────────
  function printSection(sectionId) {
    const section = document.getElementById(sectionId);
    if (!section) return;
    const win = window.open('', '_blank');
    win.document.write(`
      <html><head>
        <title>AreaPulse Report</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;600;700&family=Playfair+Display:wght@700&display=swap" rel="stylesheet">
        <style>
          body { font-family: 'DM Sans', sans-serif; color: #1A1208; padding: 24px; }
          h1, h2 { font-family: 'Playfair Display', serif; }
          table { width: 100%; border-collapse: collapse; }
          th, td { padding: 8px 12px; border: 1px solid #DED8CC; font-size: 12px; }
          th { background: #F0EDE6; font-weight: 700; }
          @media print { body { padding: 0; } }
        </style>
      </head><body>${section.innerHTML}</body></html>`);
    win.document.close();
    setTimeout(() => win.print(), 500);
  }

  return { exportCSV, downloadPDF, printSection };
})();

window.ExportEngine = ExportEngine;
