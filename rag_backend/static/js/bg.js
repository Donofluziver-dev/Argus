// ===== WebGL-Hintergrund: driftender Nebel + Sternfeld =====
// Laeuft in jeder Ansicht hinter dem Inhalt. Ein einzelner Fullscreen-Quad mit
// Fractal-Noise plus ein Punkt-Layer -- billig genug, um dauerhaft zu laufen.
// Faellt WebGL aus, bleibt der Canvas leer; die CSS-Farbkreise tragen dann allein.
window.argusBg = (function () {
  var gl, quadProg, starProg, raf = 0, canvas = null, t0 = 0, started = false;
  var quadBuf, starBuf, U = {}, SU = {}, NSTARS = 900;

  var QUAD_VS = 'attribute vec2 p; varying vec2 uv;' +
    'void main(){ uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0.0, 1.0); }';

  var QUAD_FS = 'precision mediump float; varying vec2 uv;' +
    'uniform float time; uniform vec2 res;' +
    'float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }' +
    'float noise(vec2 p){ vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);' +
    ' return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), f.x),' +
    '            mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x), f.y); }' +
    'float fbm(vec2 p){ float v = 0.0, a = 0.5;' +
    ' for (int i = 0; i < 5; i++) { v += a * noise(p); p *= 2.02; a *= 0.5; } return v; }' +
    'void main(){' +
    ' vec2 q = uv; q.x *= res.x / max(res.y, 1.0);' +
    // Drei gegenlaeufige Noise-Ebenen mit inkommensurablen Tempi: der Nebel
    // wiederholt sich dadurch nicht sichtbar, und die Farbzonen wandern gegeneinander.
    ' float n1 = fbm(q * 2.0 + vec2(time * 0.013, time * 0.008));' +
    ' float n2 = fbm(q * 3.1 - vec2(time * 0.009, time * 0.017) + n1 * 0.7);' +
    ' float n3 = fbm(q * 1.4 + vec2(time * 0.006, -time * 0.011) + n2 * 0.4);' +
    ' float neb = smoothstep(0.26, 0.92, n1 * 0.6 + n2 * 0.45 + n3 * 0.25);' +
    ' vec3 cyan    = vec3(0.05, 0.80, 1.00);' +
    ' vec3 magenta = vec3(1.00, 0.14, 0.84);' +
    ' vec3 violet  = vec3(0.55, 0.22, 1.00);' +
    ' vec3 blau    = vec3(0.12, 0.36, 0.95);' +
    // Zweistufige Mischung: erst die kalte Achse (blau -> cyan), dann Violett und
    // Magenta darueber. So entstehen Schwaden mit erkennbar eigener Farbe statt
    // eines einheitlichen Farbstichs.
    ' vec3 col = mix(blau, cyan, smoothstep(0.15, 0.85, n1));' +
    ' col = mix(col, violet, smoothstep(0.25, 0.90, n3));' +
    ' col = mix(col, magenta, smoothstep(0.40, 1.00, n2) * 0.85);' +
    // Dichte Stellen zusaetzlich aufhellen -- gibt dem Rauch Tiefe.
    ' col += vec3(0.16, 0.05, 0.20) * pow(neb, 2.5);' +
    // Zur Bildmitte hin dunkler: der Inhalt liegt darueber und braucht Kontrast.
    ' float vign = 1.0 - smoothstep(0.15, 1.05, length(uv - 0.5) * 1.35);' +
    ' float alpha = neb * 0.30 * (0.32 + 0.68 * vign);' +
    ' gl_FragColor = vec4(col * alpha, alpha); }';

  // Warp: die Sterne laufen radial nach aussen, beschleunigen dabei und werden
  // groesser -- ein Flug durchs Sternenfeld, nur sehr langsam.
  var STAR_VS = 'attribute vec3 a; uniform float time; uniform vec2 res;' +
    'varying float vb; varying float vt;' +
    'void main(){' +
    // a.z traegt Tempo und Phase zugleich: langsame Sterne wirken weiter entfernt.
    ' float speed = 0.0035 + a.z * 0.0075;' +
    ' float t = fract(a.y + time * speed);' +
    ' float ang = a.x * 6.2831853;' +
    // Quadratisch: nahe der Mitte kaum Bewegung, nach aussen hin schnell. Genau
    // das erzeugt den Sog.
    ' float d = t * t * 1.15;' +
    ' vec2 dir = vec2(cos(ang), sin(ang) * min(res.x / max(res.y, 1.0), 2.2));' +
    ' vec2 pos = vec2(0.5) + dir * d * 0.5;' +
    // In der Mitte einblenden, am Rand ausblenden -- sonst blitzen sie auf.
    ' vb = smoothstep(0.0, 0.18, t) * (1.0 - smoothstep(0.72, 1.0, t))' +
    '      * (0.45 + 0.55 * (0.5 + 0.5 * sin(time * (0.25 + a.z) + a.z * 30.0)));' +
    // Farbanteil aus derselben Zufallszahl, aber anders gefaltet -- Tempo und
    // Farbe sollen nicht miteinander korrelieren.
    ' vt = fract(a.z * 7.31 + a.x * 3.17);' +
    ' gl_Position = vec4(pos.x * 2.0 - 1.0, pos.y * 2.0 - 1.0, 0.0, 1.0);' +
    ' gl_PointSize = (0.5 + a.z * 1.4 + d * 2.2) * min(res.y / 800.0 + 0.7, 2.0); }';

  var STAR_FS = 'precision mediump float; varying float vb; varying float vt;' +
    'void main(){ float d = length(gl_PointCoord - vec2(0.5));' +
    ' float al = smoothstep(0.5, 0.0, d) * vb;' +
    // Nicht alle Sterne weiss: ein Teil zieht ins Cyan, ein Teil ins Magenta --
    // dieselbe Palette wie der Nebel, damit beides zusammengehoert.
    ' vec3 kalt = vec3(0.70, 0.90, 1.00);' +
    ' vec3 warm = vec3(1.00, 0.72, 0.95);' +
    ' vec3 col = mix(kalt, warm, vt);' +
    ' gl_FragColor = vec4(col * al, al); }';

  function shader(type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      console.error('argusBg shader:', gl.getShaderInfoLog(s));
      return null;
    }
    return s;
  }

  function program(vs, fs) {
    var v = shader(gl.VERTEX_SHADER, vs), f = shader(gl.FRAGMENT_SHADER, fs);
    if (!v || !f) return null;
    var p = gl.createProgram();
    gl.attachShader(p, v);
    gl.attachShader(p, f);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      console.error('argusBg link:', gl.getProgramInfoLog(p));
      return null;
    }
    return p;
  }

  function resize() {
    // Bewusst auf 1.0 statt devicePixelRatio gedeckelt: der Hintergrund ist
    // weichgezeichnet, eine hoehere Aufloesung kostet Fuellrate ohne Gewinn.
    // clientWidth statt innerWidth: in einem verborgenen Fenster ist innerWidth 0,
    // und der Canvas bliebe dann dauerhaft auf Nullgroesse stehen.
    var w = Math.round(canvas.clientWidth || window.innerWidth || 0);
    var h = Math.round(canvas.clientHeight || window.innerHeight || 0);
    if (!w || !h) return false;
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
    }
    return true;
  }

  function init() {
    canvas = document.getElementById('bg-canvas');
    if (!canvas) return false;
    gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: false });
    if (!gl) return false;

    quadProg = program(QUAD_VS, QUAD_FS);
    starProg = program(STAR_VS, STAR_FS);
    if (!quadProg || !starProg) return false;

    quadBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
    gl.bufferData(gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);

    var stars = new Float32Array(NSTARS * 3);
    for (var i = 0; i < NSTARS; i++) {
      stars[i * 3] = Math.random();
      stars[i * 3 + 1] = Math.random();
      stars[i * 3 + 2] = Math.random();
    }
    starBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, starBuf);
    gl.bufferData(gl.ARRAY_BUFFER, stars, gl.STATIC_DRAW);

    U.p = gl.getAttribLocation(quadProg, 'p');
    U.time = gl.getUniformLocation(quadProg, 'time');
    U.res = gl.getUniformLocation(quadProg, 'res');
    SU.a = gl.getAttribLocation(starProg, 'a');
    SU.time = gl.getUniformLocation(starProg, 'time');
    SU.res = gl.getUniformLocation(starProg, 'res');

    gl.disable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    resize();
    window.addEventListener('resize', resize);
    return true;
  }

  function frame(now) {
    var time = (now - t0) / 1000;
    // Ohne nutzbare Flaeche gar nicht erst zeichnen, aber weiter takten: sobald das
    // Fenster sichtbar wird, greift resize() und das Bild steht.
    if (!resize()) { raf = requestAnimationFrame(frame); return; }
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(quadProg);
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
    gl.enableVertexAttribArray(U.p);
    gl.vertexAttribPointer(U.p, 2, gl.FLOAT, false, 0, 0);
    gl.uniform1f(U.time, time);
    gl.uniform2f(U.res, canvas.width, canvas.height);
    gl.drawArrays(gl.TRIANGLES, 0, 6);

    gl.useProgram(starProg);
    gl.bindBuffer(gl.ARRAY_BUFFER, starBuf);
    gl.enableVertexAttribArray(SU.a);
    gl.vertexAttribPointer(SU.a, 3, gl.FLOAT, false, 0, 0);
    gl.uniform1f(SU.time, time);
    gl.uniform2f(SU.res, canvas.width, canvas.height);
    gl.drawArrays(gl.POINTS, 0, NSTARS);

    raf = requestAnimationFrame(frame);
  }

  return {
    start: function () {
      if (started) return;
      // Wer Bewegung im Betriebssystem abbestellt hat, bekommt hier keine.
      if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      try {
        if (!init()) return;
      } catch (e) {
        console.warn('argusBg nicht verfügbar:', e);
        return;
      }
      started = true;
      t0 = performance.now();
      raf = requestAnimationFrame(frame);
    },
    stop: function () { cancelAnimationFrame(raf); raf = 0; }
  };
})();

// Nicht mehr automatisch gestartet: der Entwurf hat keinen Nebel, das Canvas ist
// per CSS aus. Die Schnittstelle bleibt, damit ein Wiedereinschalten ein Aufruf
// von argusBg.start() ist und kein Rueckbau.

// ===== Orb auf dem Anmelde-Bildschirm =====
// Eigene, schlanke Implementierung statt vcOrb: der Haupt-Orb ist ein Singleton
// mit festem GL-Kontext an EINEM Canvas.
window.argusAuthOrb = (function () {
  var gl, prog, raf = 0, canvas = null, buf, A = {}, U = {}, t0 = 0, ready = false, running = false;
  var N = 2000;

  var VS = 'precision mediump float; attribute vec4 a; uniform float time, aspect;' +
    'varying float vb;' +
    'void main(){' +
    ' float ang = time * 0.16 + a.w * 0.0;' +
    ' float cs = cos(ang), sn = sin(ang);' +
    ' vec3 p = vec3(a.x * cs + a.z * sn, a.y, -a.x * sn + a.z * cs);' +
    ' float tilt = 0.4;' +
    ' p = vec3(p.x, p.y * cos(tilt) - p.z * sin(tilt), p.y * sin(tilt) + p.z * cos(tilt));' +
    ' float breathe = 1.0 + 0.035 * sin(time * 0.8);' +
    ' float persp = 2.8 / (2.8 - p.z);' +
    ' float depth = (p.z + 1.0) * 0.5;' +
    ' float rim = pow(clamp(length(p.xy), 0.0, 1.0), 2.2);' +
    ' float tw = 0.6 + 0.4 * sin(time * 1.6 + a.w * 40.0);' +
    ' vb = (0.22 + depth * 0.4 + rim * 0.95) * tw;' +
    ' gl_Position = vec4(p.x * 0.42 * breathe * persp * aspect, p.y * 0.42 * breathe * persp, 0.0, 1.0);' +
    ' gl_PointSize = (1.1 + depth * 2.6) * tw; }';

  var FS = 'precision mediump float; varying float vb;' +
    'void main(){ float d = length(gl_PointCoord - vec2(0.5));' +
    ' float al = smoothstep(0.5, 0.0, d);' +
    ' vec3 col = mix(vec3(0.0, 0.85, 1.0), vec3(0.85, 0.2, 0.82), clamp(vb * 0.7, 0.0, 1.0));' +
    ' gl_FragColor = vec4(col * vb * al, al * vb); }';

  function compile(type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null;
  }

  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.round((canvas.clientWidth || 600) * dpr);
    var h = Math.round((canvas.clientHeight || 600) * dpr);
    if (!w || !h) return false;
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
    }
    return true;
  }

  function init() {
    canvas = document.getElementById('auth-orb-canvas');
    if (!canvas) return false;
    gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: true });
    if (!gl) return false;
    var v = compile(gl.VERTEX_SHADER, VS), f = compile(gl.FRAGMENT_SHADER, FS);
    if (!v || !f) return false;
    prog = gl.createProgram();
    gl.attachShader(prog, v);
    gl.attachShader(prog, f);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return false;

    // Fibonacci-Spirale: verteilt Punkte gleichmaessig auf der Kugel, ohne Pol-Cluster.
    var golden = Math.PI * (3 - Math.sqrt(5));
    var pts = new Float32Array(N * 4);
    for (var i = 0; i < N; i++) {
      var y = 1 - (i / (N - 1)) * 2;
      var r = Math.sqrt(Math.max(0, 1 - y * y));
      var th = golden * i;
      var jitter = 0.97 + Math.random() * 0.05;
      pts[i * 4] = Math.cos(th) * r * jitter;
      pts[i * 4 + 1] = y * jitter;
      pts[i * 4 + 2] = Math.sin(th) * r * jitter;
      pts[i * 4 + 3] = Math.random();
    }
    buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, pts, gl.STATIC_DRAW);

    A.a = gl.getAttribLocation(prog, 'a');
    U.time = gl.getUniformLocation(prog, 'time');
    U.aspect = gl.getUniformLocation(prog, 'aspect');
    gl.disable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    resize();
    window.addEventListener('resize', resize);
    ready = true;
    return true;
  }

  function frame(now) {
    if (!resize()) { raf = requestAnimationFrame(frame); return; }
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.enableVertexAttribArray(A.a);
    gl.vertexAttribPointer(A.a, 4, gl.FLOAT, false, 0, 0);
    gl.uniform1f(U.time, (now - t0) / 1000);
    gl.uniform1f(U.aspect, canvas.height / Math.max(canvas.width, 1));
    gl.drawArrays(gl.POINTS, 0, N);
    raf = requestAnimationFrame(frame);
  }

  return {
    start: function () {
      if (running) return;
      try {
        if (!ready && !init()) return;
      } catch (e) { return; }
      running = true;
      t0 = performance.now();
      raf = requestAnimationFrame(frame);
    },
    stop: function () { cancelAnimationFrame(raf); raf = 0; running = false; }
  };
})();
