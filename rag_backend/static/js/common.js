const $ = (id) => document.getElementById(id);

// Linien-Symbole. Zentral, damit ein Symbol nur an einer Stelle liegt, und als
// SVG statt Emoji: sie erben ueber currentColor die Textfarbe und sehen auf jedem
// System gleich aus. Vorlagen aus dem Design-Prototyp.
const ICONS = {
  mic:      '<path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M5 10v1a7 7 0 0 0 14 0v-1"/><path d="M12 19v3"/>',
  speaker:  '<path d="M11 5 6 9H3v6h3l5 4V5z"/><path d="M16 9a4 4 0 0 1 0 6"/><path d="M19 6.5a8 8 0 0 1 0 11"/>',
  keyboard: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h12"/>',
  folder:   '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>',
  clip:     '<path d="M21 11.5 12.5 20a5 5 0 0 1-7-7l8-8a3.5 3.5 0 0 1 5 5l-8 8a2 2 0 0 1-3-3l7-7"/>',
  send:     '<path d="M4 12h15"/><path d="M13 6l6 6-6 6"/>',
  copy:     '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>',
  reload:   '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/>',
  trash:    '<path d="M4 7h16"/><path d="M9 7V5h6v2"/><path d="M6 7l1 13h10l1-13"/><path d="M10 11v6M14 11v6"/>',
  export:   '<path d="M12 3v12"/><path d="M8 11l4 4 4-4"/><path d="M4 19h16"/>',
  stop:     '<rect x="7" y="7" width="10" height="10" rx="2"/>',
  x:        '<path d="M6 6l12 12M18 6L6 18"/>',
  haken:    '<path d="M4.6 12.4l4.4 4.4 10-10"/>',
  strich:   '<path d="M6 12h12"/>',
  hirn:     '<path d="M12 3.4a5.6 5.6 0 0 0-5.6 5.6c0 1.9 1 3.2 1.8 4.2.6.8.9 1.5.9 2.4v.6h5.8v-.6c0-.9.3-1.6.9-2.4.8-1 1.8-2.3 1.8-4.2A5.6 5.6 0 0 0 12 3.4Z"/><path d="M10 19.4h4M10.6 21.6h2.8"/>',
  chevron:  '<path d="M6 9.5l6 6 6-6"/>',
  laut:     '<path d="M4 9.5h3.2L12 5.4v13.2L7.2 14.5H4z"/><path d="M16.4 9.6a3.6 3.6 0 0 1 0 4.8"/>',
  lupe:     '<circle cx="11" cy="11" r="6.4"/><path d="M15.8 15.8 20 20"/>',
  plus:     '<path d="M12 5v14M5 12h14"/>',
  datei:    '<path d="M6.4 3.4h7.2l4.4 4.4v12.8H6.4z"/><path d="M13.6 3.4v4.4H18"/>',
  hoch:     '<path d="M12 15.6V4.4M7.8 8.6 12 4.4l4.2 4.2"/><path d="M4.4 19.6h15.2"/>',
  pfeil:    '<path d="M4.6 12h13M12.4 6.6l5.2 5.4-5.2 5.4"/>',
  person:   '<circle cx="12" cy="8.4" r="3.6"/><path d="M5.2 19.4a6.8 6.8 0 0 1 13.6 0"/>',
  schloss:  '<rect x="4.6" y="10.4" width="14.8" height="9.4" rx="2.2"/><path d="M8.4 10.4V7.8a3.6 3.6 0 0 1 7.2 0v2.6"/>',
  extern:   '<path d="M13.4 4.6H19.4v6M19.4 4.6 11 13"/><path d="M18 14.4v3.4a2 2 0 0 1-2 2H6.2a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3.4"/>',
  ring:     '<circle cx="12" cy="12" r="8.2"/><circle cx="12" cy="12" r="4.6" opacity=".45"/><circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none"/><path d="M12 1.6v2.6M12 19.8v2.6M1.6 12h2.6M19.8 12h2.6"/>'
};

// groesse: Kantenlaenge in px. stroke ist immer currentColor -- die Farbe kommt
// aus dem umgebenden Element, nicht aus dem Symbol.
function icon(name, groesse) {
  const d = ICONS[name];
  if (!d) return '';
  const g = groesse || 15;
  return '<svg width="' + g + '" height="' + g + '" viewBox="0 0 24 24" fill="none" '
       + 'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
       + 'stroke-linejoin="round" aria-hidden="true">' + d + '</svg>';
}
window.argusIcon = icon;

// Symbole im festen Markup: data-icon statt eingebettetem SVG, damit ein Zeichen
// nur an einer Stelle liegt. Die Anmeldung fuellt ihre eigenen schon vorher --
// beides zu tun schadet nicht, das Ergebnis ist dasselbe.
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('[data-icon]').forEach(function (n) {
    n.innerHTML = icon(n.dataset.icon, Number(n.dataset.iconGroesse) || 15);
  });
});

function escapeHtml(text) {
  if (!text) return "";
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Eine Anlaufstelle fuer alle Backend-Aufrufe. Bei 401 (Sitzung abgelaufen oder
// abgemeldet) uebernimmt die Anmeldung -- sonst liefen Polling und Chat weiter und
// zeigten Fehler, deren Ursache nirgends steht.
async function api(path, opts) {
  opts = opts || {};
  const r = await fetch(path, opts);
  if (r.status === 401) {
    if (window.argusAuth) window.argusAuth.sessionLost();
    // Die Meldung des Backends weitergeben: bei der Anmeldung ist das
    // "Anmeldung fehlgeschlagen.", sonst der Grund fuer das Sitzungsende.
    let grund = "";
    try { grund = (await r.json()).detail || ""; } catch (e) { /* kein JSON-Fehlerkoerper */ }
    throw new Error(grund || "Nicht angemeldet");
  }
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch (e) { /* kein JSON-Fehlerkoerper */ }
    throw new Error(detail || ("HTTP " + r.status));
  }
  return r.json();
}

function apiPost(path, body) {
  const opts = { method: "POST" };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  return api(path, opts);
}

// Deutsche Schreibweise. Kilobyte ohne Nachkommastelle -- die eine Stelle
// hilft dort nicht beim Einordnen, ab Megabyte schon.
function formatBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  const stellen = i <= 1 ? 0 : 1;
  return n.toFixed(stellen).replace(".", ",") + " " + units[i];
}

// Ein Chevron, gedreht statt getauscht: zwei Zeichen waeren zwei Glyphen mit
// unterschiedlicher Breite und liessen den Kopf beim Klappen springen.
function togglePanel(id) {
  const content = $(id);
  const icon = $(id + "-icon");
  if (content && icon) {
    icon.classList.toggle("is-zu", content.classList.toggle("collapsed"));
  }
}

function fmt(v, d) { return (v === null || v === undefined) ? "-" : (d ? Number(v).toFixed(d) : Math.round(v)); }
// Das Backend liefert Zeitstempel in UTC ohne Zonenangabe. Ohne das "Z" liest der
// Browser sie als Ortszeit: Uhrzeiten laegen um die Zeitverschiebung daneben, und
// ein Chat kurz nach Mitternacht staende unter "Gestern".
function argusDate(iso) {
  if (!iso) return new Date(NaN);
  const s = String(iso);
  return new Date(/(Z|[+-][0-9][0-9]:?[0-9][0-9])$/.test(s) ? s : s + "Z");
}

function tcell(ts) {
  if (!ts) return "";
  const d = argusDate(ts);
  if (isNaN(d)) return String(ts).slice(0, 16).replace("T", " ");
  const p = function (n) { return String(n).padStart(2, "0"); };
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
    + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function updateSparkline(svgId, valHistory, maxVal) {
  const svg = $(svgId);
  if (!svg) return;
  svg.valHistory = valHistory;
  svg.maxVal = maxVal;
  if (!valHistory || valHistory.length < 2) {
    svg.innerHTML = "";
    return;
  }
  renderSparklineWithHover(svg, valHistory, maxVal, undefined);
}

function renderSparklineWithHover(svg, valHistory, maxVal, hoverIdx) {
  if (!valHistory || valHistory.length < 2) return;
  const width = svg.viewBox.baseVal.width || 120;
  const height = svg.viewBox.baseVal.height || 35;
  
  const points = valHistory.map((v, i) => {
    const x = (i / (valHistory.length - 1)) * width;
    const y = height - (v / maxVal) * (height - 4) - 2;
    return { x, y, val: v, index: i };
  });
  
  const pointsStr = points.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" L ");
  const pathData = "M " + pointsStr;
  const line = `<path d="${pathData}" fill="none" stroke="var(--color-blue)" stroke-width="1.5" style="filter: drop-shadow(0 0 2px var(--color-blue));" />`;
  
  const areaPoints = [...points.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`), `${width},${height}`, `0,${height}`];
  const areaPath = "M " + areaPoints.join(" L ") + " Z";
  const area = `<path d="${areaPath}" fill="url(#sparkline-grad)" stroke="none" opacity="0.1" />`;
  
  const defs = `<defs><linearGradient id="sparkline-grad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="var(--color-blue)" /><stop offset="100%" stop-color="transparent" /></linearGradient></defs>`;
  
  let interactiveElements = "";
  if (hoverIdx !== undefined && hoverIdx >= 0 && hoverIdx < points.length) {
    const hp = points[hoverIdx];
    const verticalLine = `<line x1="${hp.x.toFixed(1)}" y1="0" x2="${hp.x.toFixed(1)}" y2="${height}" stroke="rgba(0, 229, 255, 0.6)" stroke-dasharray="2,2" stroke-width="1" />`;
    const dot = `<circle cx="${hp.x.toFixed(1)}" cy="${hp.y.toFixed(1)}" r="3" fill="#ffffff" stroke="var(--color-blue)" stroke-width="1.5" style="filter: drop-shadow(0 0 3px var(--color-blue));" />`;
    interactiveElements = verticalLine + dot;
  }
  
  svg.innerHTML = defs + area + line + interactiveElements;
}

function updateRing(id, val, max, circumference) {
  const el = $(id);
  if (!el) return;
  const clamped = Math.max(0, Math.min(val, max));
  const offset = circumference - (clamped / max) * circumference;
  el.style.strokeDashoffset = offset;
}
