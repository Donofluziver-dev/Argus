// ===== Hash-Router: vier Ansichten, EIN Dokument =====
// Kein Neuaufbau pro Seitenwechsel: der WebGL-Orb muesste sonst jedes Mal Shader und
// Partikel-Buffer neu erzeugen, und der Chat-Verlauf ginge mit. Die Ansichten werden
// nur ein- und ausgeblendet; was Rechenzeit kostet, meldet sich ueber die Hooks an
// und ab. Gestartet wird der Router erst nach erfolgreicher Anmeldung (auth.js).
const VIEWS = ['live', 'chat', 'monitor'];
const ADMIN_VIEWS = ['monitor'];
const DEFAULT_VIEW = 'live';
let currentView = null;

function viewAllowed(name) {
  if (ADMIN_VIEWS.indexOf(name) < 0) return true;
  return !!(window.argusAuth && window.argusAuth.isAdmin());
}

function viewFromHash() {
  const raw = (location.hash || '').replace(/^#\/?/, '').trim();
  const name = VIEWS.indexOf(raw) >= 0 ? raw : DEFAULT_VIEW;
  // Ein Benutzer-Konto, das #/monitor eintippt, landet auf der Live-Ansicht. Die Daten
  // schuetzt das nicht -- das tun die Gates an den Routen -- es verhindert nur eine
  // leere Seite voller Fehlermeldungen.
  return viewAllowed(name) ? name : DEFAULT_VIEW;
}

function applyView(name) {
  if (name === currentView) return;
  const previous = currentView;
  currentView = name;

  VIEWS.forEach(function (v) {
    const el = $('view-' + v);
    if (el) el.classList.toggle('active', v === name);
  });
  document.querySelectorAll('.nav-link[data-view]').forEach(function (a) {
    a.classList.toggle('active', a.dataset.view === name);
  });

  // Monitor: 3s-Polling nur, solange die Zahlen auch jemand sieht. Was der einzelne
  // Reiter braucht (Phoenix, Kontenliste), holt er sich beim Öffnen selbst.
  if (name === 'monitor') {
    if (window.argusOps) window.argusOps.start();
    if (window.argusMonitorTabs) window.argusMonitorTabs.set(window.argusMonitorTabs.current());
  } else if (previous === 'monitor' && window.argusOps) {
    window.argusOps.stop();
  }

  if (name === 'chat' && window.argusChat) window.argusChat.enter();
  // Beim Verlassen der Live-Ansicht Ton und Aufnahme beenden.
  if (window.argusLive) {
    if (name === 'live') window.argusLive.enter();
    else if (previous === 'live') window.argusLive.leave();
  }

  // Orb laeuft nur auf der Live-Ansicht: ein verborgener Canvas hat clientWidth 0.
  // Direkt starten, nicht ueber requestAnimationFrame -- rAF feuert im
  // Hintergrund-Tab nicht.
  const canvas = $('galaxy-canvas');
  if (window.vcOrb && canvas) {
    if (name === 'live') window.vcOrb.start(canvas);
    else if (previous === 'live') window.vcOrb.stop();
  }

  // Der Hintergrundnebel laeuft nicht mehr mit (siehe bg.js). Die Klasse bleibt,
  // weil das Glas-Thema die Live-Buehne darueber anspricht.
  document.body.classList.toggle('live-aktiv', name === 'live');
  if (window.argusBg) window.argusBg.stop();
}

function routeNow() { applyView(viewFromHash()); }

window.addEventListener('hashchange', routeNow);
window.argusRouteNow = routeNow;

// Von auth.js gerufen, sobald eine Sitzung steht. Vorher darf nichts anlaufen:
// jeder Aufruf gaebe 401 und riefe die Anmeldung wieder auf den Plan.
window.argusBoot = function () {
  initGalaxy();
  if (!location.hash) location.hash = '#/' + DEFAULT_VIEW;
  currentView = null;
  routeNow();
};
