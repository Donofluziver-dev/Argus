window.currentLlmThroughput = 0;
window.currentTtsThroughput = 0;
window.currentKvCache = 0;

// Nur fuer die kleine KV-Cache-Kugel (2D-Canvas); der Haupt-Orb laeuft als
// WebGL-Sphaere in window.vcOrb.
const kvShellPoints = [];
const kvInnerDust = [];
const kvRimMesh = [];
let kvSphereAngle = 0;

// ===== Orb-Zustaende: idle/listening/research/thinking/speaking + tool =====
// acShell/acCore = Farbpaar, acShell2/acCore2 = Anker der Farbdrift, breath/breathHz
// = Atmung, wander = Drift, rot = Rotation, veil/vturb = Schleier.
const ORB_T = {
  idle:      { rmul: 1.00, coreW: 0.12, ring: 0.10, acShell: [0, 229, 255],   acCore: [255, 43, 214],
               acShell2: [166, 75, 255], acCore2: [40, 150, 255],  breath: 0.045, breathHz: 0.85, wander: 0.34, rot: 1.0,  veil: 0.45, vturb: 0.45 },
  listening: { rmul: 0.95, coreW: 0.14, ring: 0.12, acShell: [120, 255, 205], acCore: [0, 190, 255],
               acShell2: [60, 235, 140], acCore2: [0, 229, 255],   breath: 0.10,  breathHz: 0.45, wander: 0,    rot: 0.55, veil: 0.55, vturb: 0.30 },
  research:  { rmul: 1.05, coreW: 0.22, ring: 0.55, acShell: [255, 200, 80],  acCore: [255, 90, 30],
               acShell2: [255, 150, 90], acCore2: [255, 210, 60],  breath: 0.06,  breathHz: 1.3,  wander: 0.10, rot: 1.35, veil: 0.35, vturb: 0.55 },
  thinking:  { rmul: 1.10, coreW: 0.20, ring: 0.85, acShell: [166, 75, 255],  acCore: [255, 43, 214],
               acShell2: [90, 120, 255], acCore2: [200, 80, 255],  breath: 0.05,  breathHz: 1.6,  wander: 0.08, rot: 1.6,  veil: 1.0,  vturb: 1.0 },
  speaking:  { rmul: 1.16, coreW: 1.00, ring: 0.30, acShell: [40, 150, 255],  acCore: [0, 235, 255],
               acShell2: [0, 235, 255],  acCore2: [140, 190, 255], breath: 0.04,  breathHz: 1.2,  wander: 0.12, rot: 1.1,  veil: 0.22, vturb: 0.60 }
};
const ORB_PAL = [[0, 229, 255], [255, 43, 214], [166, 75, 255], [255, 200, 80], [120, 255, 205]];
let orbState = 'idle', orbToolIdx = 0;
const OS = { rmul: 1, coreW: 0.12, ring: 0.10, spin: 0, fill: 1, acShell: [0, 229, 255], acCore: [255, 43, 214],
             breath: 0.045, breathHz: 0.85, wander: 0.34, rot: 1.0, veil: 0.30, vturb: 0.45 };
window.__orbOS = OS;  // fuer den WebGL-Orb lesbar machen

// Neuron-Pulse (Denkt-nach) + Wachstum (Recherche/Denkt-nach), vom Renderer gelesen.
let orbPulses = [], _nextOrbPulse = 0, _orbGrow = 1;
let _orbShellT = [0, 225, 255], _orbCoreT = [0, 120, 255], _orbRingT = 0.10;
// Tool-Farb-Sperre: solange aktiv, ueberschreibt die Farb-Drift die per setOrbTool
// gesetzte Signatur-Farbe NICHT; danach kehrt die State-Farbe automatisch zurueck.
let _orbToolLockUntil = 0, _orbColorPhase = Math.random() * 6.28;
// Idle-Mikro-Impulse ("da drin arbeitet jemand"): seltene Spin-Kicks.
let _nextIdleKick = 0;

// ===== Orb =====
// Der Orb liegt als eigenes Modul in js/argus-orb.js. Hier steht nur die Bruecke
// zu den vorhandenen Signalen: der Router spricht window.vcOrb.start/stop an,
// voice.js schreibt eine ZAHL in window.__orbLevel, und setOrbState muss neben
// dem Orb weiterhin die kleine KV-Kugel und die Werkzeug-Signaturfarbe treiben.
// Vom alldem weiss das Modul nichts.
window.vcOrb = (function () {
  var orb = null, cref = null;

  // Werte aus der Einbauanleitung des Entwurfs.
  var OPTS = {
    count: 13000, scale: 0.55, exposure: 2.15, rim: 0.75, halo: 0.45,
    size: 1.4, neuro: 0.25, fire: 0.15, band: 1.0,
    somata: 22, neuroDensity: 1, stars: 6720, starGain: 1
  };

  return {
    start: function (canvas) {
      try {
        if (!window.argusCreateOrb) { console.warn('vcOrb: Modul nicht geladen.'); return false; }
        cref = canvas;
        if (orb) { orb.resume(); return true; }
        orb = window.argusCreateOrb(canvas, OPTS);
        if (!orb) return false;
        // Der Router startet den Orb u.U. erst, wenn schon ein Zustand gesetzt ist.
        if (typeof orbState !== 'undefined') orb.setState(orbState);
        return true;
      } catch (e) { console.error('vcOrb start', e); return false; }
    },
    stop: function () { if (orb) orb.pause(); },
    zustand: function (s) { if (orb) orb.setState(s); },
    pegel: function (v) { if (orb) orb.setLevel(v); },
    puls: function (stark) { if (orb) orb.pulse(stark); },

    // Von Hand aufrufbar, wenn der Orb nichts zeigt.
    diagnose: function () {
      if (!window.argusCreateOrb) return { bereit: false, grund: 'Modul nicht geladen' };
      if (!orb) return { bereit: false, grund: 'nicht gestartet oder WebGL2 fehlt' };
      return { bereit: true, zustand: orb.state(), modul: 'argus-orb.js',
               canvas: cref ? cref.width + 'x' + cref.height : '-' };
    }
  };
})();

// voice.js schreibt und liest window.__orbLevel als ZAHL (level += ...). Ein
// Accessor haelt diese Schnittstelle und reicht den Wert zugleich ans Modul
// weiter -- so bleibt voice.js unberuehrt.
(function () {
  var wert = 0;
  Object.defineProperty(window, '__orbLevel', {
    get: function () { return wert; },
    set: function (v) { wert = v; window.vcOrb.pegel(v); },
    configurable: true
  });
})();

function _ez(a, b, k) { return a + (b - a) * (k || 0.05); }

window.setOrbState = function (s) {
  //--- Der Orb zuerst: er ignoriert unbekannte Werte selbst, und 'tool' ist
  //    kein Orb-Zustand sondern nur eine Farbsignatur fuer die KV-Kugel.
  if (s !== 'tool') window.vcOrb.zustand(s);
  if (s === 'tool') {
    orbToolIdx = (orbToolIdx + 1) % ORB_PAL.length;
    _orbShellT = ORB_PAL[orbToolIdx].slice(); _orbCoreT = ORB_PAL[orbToolIdx].slice();
    _orbToolLockUntil = performance.now() + 4000;  // Drift pausieren, Tool-Farbe halten
    OS.spin += 0.8; return;
  }
  // Derselbe Zustand loest keine neue Transition aus.
  if (s === orbState) return;
  orbPulses.length = 0;
  orbState = s;
  _orbToolLockUntil = 0;  // echter State-Wechsel loest eine evtl. Tool-Farbsperre
  OS.spin += 0.8;
  const t = ORB_T[s] || ORB_T.idle;
  _orbShellT = t.acShell.slice(); _orbCoreT = t.acCore.slice(); _orbRingT = t.ring;
  if (s === 'research') { _orbGrow = 0.02; OS.fill = 0.05; }
  else if (s === 'thinking') { _orbGrow = 0.0; OS.fill = 0.5; _nextOrbPulse = 0; }
};

function updateOrb(dt) {
  const now = performance.now();
  const t = ORB_T[orbState] || ORB_T.idle;
  if (orbState === 'research') { _orbGrow = Math.min(1, _orbGrow + dt / 16000); OS.fill = 0.05 + 0.95 * _orbGrow; }  // ruhiges Auffuellen ueber ~16s ("Wissen kommt rein"), weiches Fade im Shader
  else if (orbState === 'thinking') { _orbGrow = Math.min(1, _orbGrow + dt / 7000); OS.fill = 0.5 + 0.5 * _orbGrow; }
  else { OS.fill = _ez(OS.fill, 1, 0.1); }
  OS.rmul = _ez(OS.rmul, t.rmul, 0.06);
  OS.coreW = _ez(OS.coreW, t.coreW);
  OS.ring = _ez(OS.ring, _orbRingT, 0.05);
  OS.spin *= 0.90;
  OS.breath = _ez(OS.breath, t.breath == null ? 0.03 : t.breath, 0.03);
  OS.breathHz = _ez(OS.breathHz, t.breathHz || 1.1, 0.03);
  OS.wander = _ez(OS.wander, t.wander == null ? 0 : t.wander, 0.03);
  OS.rot = _ez(OS.rot, t.rot || 1, 0.04);
  OS.veil = _ez(OS.veil, t.veil == null ? 0.3 : t.veil, 0.025);
  OS.vturb = _ez(OS.vturb, t.vturb == null ? 0.45 : t.vturb, 0.025);
  // Farb-Drift zwischen den beiden Ankern des Zustands; waehrend der Tool-Sperre
  // bleibt die Signatur-Farbe stehen.
  if (now > _orbToolLockUntil) {
    const k = 0.5 + 0.5 * Math.sin(now * 0.00033 + _orbColorPhase);
    const sh2 = t.acShell2 || t.acShell, co2 = t.acCore2 || t.acCore;
    for (let i = 0; i < 3; i++) {
      _orbShellT[i] = t.acShell[i] + (sh2[i] - t.acShell[i]) * k;
      _orbCoreT[i] = t.acCore[i] + (co2[i] - t.acCore[i]) * k;
    }
  }
  for (let i = 0; i < 3; i++) { OS.acShell[i] = _ez(OS.acShell[i], _orbShellT[i]); OS.acCore[i] = _ez(OS.acCore[i], _orbCoreT[i]); }
  if (orbState === 'idle') {
    // Mikro-Lebenszeichen im Leerlauf.
    if (now > _nextIdleKick) {
      OS.spin += 0.10 + Math.random() * 0.15;
      _nextIdleKick = now + 5000 + Math.random() * 6000;
    }
  }
  for (let i = orbPulses.length - 1; i >= 0; i--) { orbPulses[i].r += orbPulses[i].sp * dt; if (orbPulses[i].r > 2.0) orbPulses.splice(i, 1); }
}

function _orbInjectControls() {
  if (document.getElementById('orb-state-switch')) return;
  const bar = document.createElement('div'); bar.id = 'orb-state-switch';
  bar.style.cssText = 'position:fixed;bottom:12px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;gap:6px;background:rgba(8,12,22,0.85);border:1px solid #1d2740;border-radius:10px;padding:6px;font-family:sans-serif';
  [['idle', 'Bereit'], ['listening', 'Hört zu'], ['research', 'Recherche'], ['thinking', 'Denkt nach'], ['speaking', 'Spricht'], ['tool', 'Tool ↻']].forEach(function (o) {
    const b = document.createElement('button'); b.textContent = o[1];
    b.style.cssText = 'font-size:12px;padding:6px 10px;border-radius:7px;border:1px solid #243150;background:#0e1626;color:#aeb8cf;cursor:pointer';
    b.onclick = function () { window.setOrbState(o[0]); }; bar.appendChild(b);
  });
  document.body.appendChild(bar);
}
if (new URLSearchParams(location.search).has('orbdev')) {
  if (document.readyState !== 'loading') _orbInjectControls();
  else document.addEventListener('DOMContentLoaded', _orbInjectControls);
}

// ===== Tool -> Signatur-Farbe (per Status-Emoji aus _TOOL_STATUS im Backend) =====
const TOOL_COLORS = {
  '\u{1F50D}': [60, 150, 255],   // web_search / deep_research -> blau
  '\u{1F4C4}': [60, 150, 255],   // read_webpage -> blau
  '\u{1F4DA}': [0, 229, 255],    // document_search -> cyan
  '\u{1F522}': [120, 255, 120],  // calculate -> gruen
  '\u{1F4BB}': [120, 255, 120],  // execute_code -> gruen
  '\u{1F504}': [255, 150, 40],   // restart_service -> orange (Host-Aktion)
  '\u{1F6E1}': [255, 150, 40],   // windows updates -> orange
  '\u{1F527}': [255, 150, 40],   // generischer Tool-Fallback -> orange
  '\u{1F4E5}': [230, 230, 235],  // ingest/index -> weiss-grau
  '\u{1F5D1}': [230, 230, 235],  // delete_indexed -> weiss-grau
  '\u{1F4D1}': [230, 230, 235],  // list_indexed -> weiss-grau
  '\u{1F9F5}': [255, 210, 90],   // research / parallel_agents -> gelb
  '\u{1F916}': [255, 210, 90]    // invoke_subagent -> gelb
};
window.setOrbTool = function (emoji) {
  const col = TOOL_COLORS[emoji] || ORB_T.research.acShell;
  if (orbState !== 'research') { orbState = 'research'; _orbGrow = 0.05; OS.fill = 0.05; _orbRingT = ORB_T.research.ring; }
  _orbShellT = col.slice();
  _orbCoreT = col.slice();
  _orbToolLockUntil = performance.now() + 4000;  // Signatur-Farbe halten, dann zurueck zur State-Farbe
  OS.spin += 0.9;
};

const GOLDEN = Math.PI * (3 - Math.sqrt(5));

const KV_SHELL_N = 600;
const KV_INNER_DUST_N = 120;
for (let i = 0; i < KV_SHELL_N; i++) {
  const sy = 1 - (i / (KV_SHELL_N - 1)) * 2;
  const sr = Math.sqrt(Math.max(0, 1 - sy * sy));
  const sa = GOLDEN * i;
  const shell = 0.94 + Math.random() * 0.08;
  const bright = Math.random() < 0.24;

  kvShellPoints.push({
    x: Math.cos(sa) * sr * shell,
    y: sy * shell,
    z: Math.sin(sa) * sr * shell,
    size: bright ? Math.random() * 0.42 + 0.18 : Math.random() * 0.26 + 0.10,
    color: bright ? '#ffffff' : (Math.random() < 0.65 ? '#ff2bd6' : '#a64bff'),
    mesh: Math.random() < 0.46
  });
}

for (let i = 0; i < KV_INNER_DUST_N; i++) {
  const theta = Math.random() * Math.PI * 2;
  const phi = Math.acos(Math.random() * 2 - 1);
  const radius = Math.pow(Math.random(), 0.7) * 0.62;

  kvInnerDust.push({
    x: Math.sin(phi) * Math.cos(theta) * radius,
    y: Math.sin(phi) * Math.sin(theta) * radius,
    z: Math.cos(phi) * radius,
    size: Math.random() * 0.24 + 0.08,
    alpha: Math.random() * 0.13 + 0.035,
    color: Math.random() < 0.25 ? '#ffffff' : (Math.random() < 0.65 ? '#ff2bd6' : '#a64bff')
  });
}

for (let i = 0; i < kvShellPoints.length && kvRimMesh.length < 800; i++) {
  if (!kvShellPoints[i].mesh) continue;
  for (let j = i + 1; j < kvShellPoints.length && kvRimMesh.length < 800; j++) {
    if (!kvShellPoints[j].mesh) continue;
    const dx = kvShellPoints[i].x - kvShellPoints[j].x;
    const dy = kvShellPoints[i].y - kvShellPoints[j].y;
    const dz = kvShellPoints[i].z - kvShellPoints[j].z;
    const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
    if (dist > 0.105 && dist < 0.165 && Math.random() < 0.18) {
      kvRimMesh.push({ a: i, b: j, alpha: Math.random() * 0.18 + 0.08 });
    }
  }
}

function initGalaxy() {
  // galaxy-canvas gehoert dem WebGL-Orb; hier laeuft nur die 2D-KV-Kugel.
  const canvas = $('galaxy-canvas');
  if (!canvas) return;

  // KV Cache Canvas
  const kvCanvas = $('kv-galaxy-canvas');
  const kvCtx = kvCanvas ? kvCanvas.getContext('2d') : null;
  const kvWidth = kvCanvas ? kvCanvas.width : 240;
  const kvHeight = kvCanvas ? kvCanvas.height : 240;
  const kvCenterX = kvWidth / 2;
  const kvCenterY = kvHeight / 2;

  let lastTime = Date.now();

  function draw() {
    const now = Date.now();
    const dt = now - lastTime;
    lastTime = now;
    updateOrb(dt);

    if (kvCtx) {
      kvCtx.clearRect(0, 0, kvWidth, kvHeight);

      // Radius waechst mit der Auslastung: 50 bis 85.
      const kvBaseR = 50;
      const kvTargetR = kvBaseR + (window.currentKvCache * 35);
      const kvBreathe = 1.0 + Math.sin(Date.now() / 1500) * 0.06;
      const kvR = kvTargetR * kvBreathe;
      const speedFactor = 1.0 + (window.currentKvCache * 1.5);

      const kvGrad = kvCtx.createRadialGradient(kvCenterX, kvCenterY, 2, kvCenterX, kvCenterY, (kvTargetR + 10) * kvBreathe);
      kvGrad.addColorStop(0, 'rgba(255, 43, 214, 0.16)');
      kvGrad.addColorStop(0.55, 'rgba(166, 75, 255, 0.05)');
      kvGrad.addColorStop(1, 'rgba(0, 0, 0, 0)');
      kvCtx.fillStyle = kvGrad;
      kvCtx.beginPath();
      kvCtx.arc(kvCenterX, kvCenterY, kvTargetR + 20, 0, Math.PI * 2);
      kvCtx.fill();

      const kvTiltX = 0.52 + Math.sin(Date.now() / 4800) * 0.08;
      const kvTiltY = Math.cos(Date.now() / 6500) * 0.05;

      kvSphereAngle += 0.00175 * speedFactor;

      const ay = kvSphereAngle + kvTiltY;
      const cosA = Math.cos(ay), sinA = Math.sin(ay);
      const cosT = Math.cos(kvTiltX), sinT = Math.sin(kvTiltX);
      const DIST = 2.8;

      const projectKvSphere = (p) => {
        const rx = p.x * cosA + p.z * sinA;
        const rz = -p.x * sinA + p.z * cosA;
        const ry = p.y * cosT - rz * sinT;
        const dz = p.y * sinT + rz * cosT;
        const sc = DIST / (DIST - dz);
        return { x: kvCenterX + rx * kvR * sc, y: kvCenterY + ry * kvR * sc, z: dz, sc: sc };
      };

      const kvRimStrength = (q) => {
        const radial = Math.hypot(q.x - kvCenterX, q.y - kvCenterY) / (kvR * 1.08);
        return Math.max(0, Math.min(1, (radial - 0.50) / 0.46));
      };

      kvCtx.globalCompositeOperation = 'lighter';

      kvInnerDust.forEach(p => {
        const q = projectKvSphere(p);
        const zN = Math.max(0, Math.min(1, (q.z + 1) / 2));
        const alpha = p.alpha * (0.45 + zN * 0.7);
        kvCtx.fillStyle = p.color === '#ffffff'
          ? 'rgba(255, 255, 255, ' + alpha.toFixed(3) + ')'
          : (p.color === '#ff2bd6' ? 'rgba(255, 43, 214, ' + alpha.toFixed(3) + ')' : 'rgba(166, 75, 255, ' + alpha.toFixed(3) + ')');
        kvCtx.beginPath();
        kvCtx.arc(q.x, q.y, p.size * q.sc, 0, Math.PI * 2);
        kvCtx.fill();
      });

      kvRimMesh.forEach(edge => {
        const a = projectKvSphere(kvShellPoints[edge.a]);
        const b = projectKvSphere(kvShellPoints[edge.b]);
        const rim = Math.min(kvRimStrength(a), kvRimStrength(b));
        if (rim < 0.22) return;
        const zN = Math.max(0, Math.min(1, ((a.z + b.z) * 0.5 + 1) / 2));
        const alpha = edge.alpha * rim * (0.35 + zN * 0.65);
        kvCtx.strokeStyle = 'rgba(255, 43, 214, ' + alpha.toFixed(3) + ')';
        kvCtx.lineWidth = 0.42 + rim * 0.42;
        kvCtx.beginPath();
        kvCtx.moveTo(a.x, a.y);
        kvCtx.lineTo(b.x, b.y);
        kvCtx.stroke();
      });

      kvShellPoints.forEach(p => {
        const q = projectKvSphere(p);
        const zN = Math.max(0, Math.min(1, (q.z + 1) / 2));
        const rim = kvRimStrength(q);
        const alpha = 0.025 + zN * 0.10 + rim * 0.68;
        const size = p.size * (0.45 + rim * 1.2 + zN * 0.28) * q.sc;
        
        kvCtx.fillStyle = p.color === '#ffffff'
          ? 'rgba(255, 255, 255, ' + alpha.toFixed(3) + ')'
          : (p.color === '#ff2bd6' ? 'rgba(255, 43, 214, ' + alpha.toFixed(3) + ')' : 'rgba(166, 75, 255, ' + alpha.toFixed(3) + ')');
        kvCtx.beginPath();
        kvCtx.arc(q.x, q.y, size, 0, Math.PI * 2);
        kvCtx.fill();
      });

      // Ein nach innen laufender Ring, wenn gerade kompaktiert wurde: die Kugel
      // schrumpft ohnehin, der Ring sagt, dass das kein Zufall war.
      const flashUntil = window.kvCompactFlashUntil || 0;
      if (flashUntil > now) {
        const remaining = (flashUntil - now) / 6000;      // 1 -> 0
        const cycle = (now % 1500) / 1500;                 // 0 -> 1 je Sekunde-Halb
        const ringR = kvR * (1.45 - cycle * 0.42);
        const ringAlpha = (1 - cycle) * 0.55 * Math.min(1, remaining * 2);
        kvCtx.strokeStyle = 'rgba(0, 229, 255, ' + ringAlpha.toFixed(3) + ')';
        kvCtx.lineWidth = 1.6;
        kvCtx.beginPath();
        kvCtx.arc(kvCenterX, kvCenterY, ringR, 0, Math.PI * 2);
        kvCtx.stroke();
      }
    }

    requestAnimationFrame(draw);
  }
  
  draw();
}
