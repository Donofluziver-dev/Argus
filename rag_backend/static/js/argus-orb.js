// Argus-Orb v3
//
//   Sternenfeld  Eigene Lage im Bildschirmraum -- gehoert NICHT zur Kugel, dreht
//                nicht mit. Sterne entstehen, funkeln, verschwinden; bei einem
//                Ereignis reisst es sie nach aussen (Warp).
//   Schale       Punkte in einer duennen Lage um r=1. Rand leuchtet (rim), Mitte
//                bleibt offen. Umlaufende Lichtbaender geben Bewegung.
//   Neuronen     Baeume aus Punktketten. Jede Zelle FEUERT: eine Helligkeitswelle
//                laeuft vom Soma nach aussen durch ihre Aeste -- Grundton dunkel.
//   Staub        Naher Hof um die Schale.
//
// Ruhe-Regeln: Atmung laeuft ueber eine AKKUMULIERTE Phase (nie uTime*Frequenz --
// sonst springt die Phase, wenn die Frequenz beim Zustandswechsel wandert; genau
// das war das Vibrieren). Zustandswechsel setzt nichts zurueck.

const PRESETS = {
  idle:      { colA: [0.30, 0.95, 1.00], colB: [0.04, 0.30, 0.85], rot: 0.085, thick: 0.115, breath: 0.026, breathHz: 0.16, sparkle: 0.45, gain: 1.00, dustA: 1.00, neuro: 0.85, fire: 0.35, band: 0.35, stars: 1.00 },
  listening: { colA: [0.24, 1.00, 0.58], colB: [0.00, 0.60, 0.42], rot: 0.045, thick: 0.090, breath: 0.040, breathHz: 0.11, sparkle: 0.22, gain: 1.05, dustA: 0.70, neuro: 0.70, fire: 0.20, band: 0.20, stars: 0.85 },
  research:  { colA: [1.00, 0.80, 0.32], colB: [0.80, 0.26, 0.04], rot: 0.20, thick: 0.145, breath: 0.026, breathHz: 0.26, sparkle: 1.00, gain: 1.05, dustA: 1.25, neuro: 1.00, fire: 0.95, band: 0.85, stars: 1.15 },
  thinking:  { colA: [0.80, 0.48, 1.00], colB: [0.42, 0.08, 0.85], rot: 0.26, thick: 0.130, breath: 0.022, breathHz: 0.32, sparkle: 0.80, gain: 1.10, dustA: 1.05, neuro: 1.25, fire: 1.60, band: 1.00, stars: 1.25 },
  speaking:  { colA: [0.30, 0.95, 1.00], colB: [0.04, 0.30, 0.85], rot: 0.115, thick: 0.100, breath: 0.020, breathHz: 0.22, sparkle: 0.55, gain: 1.18, dustA: 0.90, neuro: 1.05, fire: 0.90, band: 0.60, stars: 1.00 }
};

const COMMON = `
float hash(vec3 p){p=fract(p*0.3183099+0.1);p*=17.0;return fract(p.x*p.y*p.z*(p.x+p.y+p.z));}
float h1(float i){return fract(sin(i*127.1+7.3)*43758.5453);}
float noise(vec3 x){vec3 i=floor(x),f=fract(x);f=f*f*(3.0-2.0*f);
  return mix(mix(mix(hash(i),hash(i+vec3(1,0,0)),f.x),mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
             mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);}
float fbm(vec3 p){float v=0.0,a=0.5;for(int i=0;i<3;i++){v+=a*noise(p);p*=2.03;a*=0.5;}return v;}
`;

const VERT = `#version 300 es
layout(location=0) in vec3 aP;      // Schale/Staub: Richtung. Neuronen: Position. Sterne: xy im Bild, z Tiefe.
layout(location=1) in vec4 aR;      // x: radiale Lage, y: Groesse, z: Bogenlage/Phase, w: Grundhelligkeit
layout(location=2) in vec2 aX;      // Neuronen: x Zell-Nummer, y Knoten-Flag
uniform float uTime,uAng,uTilt,uScale,uAspX,uAspY,uThick,uRim,uBreath,uPhase,
              uLevel,uSize,uHalo,uFill,uSparkle,uKind,uGain,uDpr,uTurb,uNeuro,uFire,
              uBand,uStars,uWarp;
uniform vec3 uColA,uColB;
uniform vec4 uPulse;
out vec4 vCol;
${COMMON}
vec3 lage(vec3 p){
  float ca=cos(uAng),sa=sin(uAng);
  p=vec3(p.x*ca+p.z*sa,p.y,-p.x*sa+p.z*ca);
  float ct=cos(uTilt),st=sin(uTilt);
  return vec3(p.x,p.y*ct-p.z*st,p.y*st+p.z*ct);
}
void main(){
  float hell=0.0, groesse=1.0;
  vec3 c=uColA;
  float atem=1.0+uBreath*sin(uPhase*6.2831)+uLevel*0.045;

  if(uKind>3.5){
    // ---- Warp-Streifen (GL_LINES) ----
    vec2 sp=aP.xy;
    float tief=0.25+0.75*aP.z;
    float rr=length(sp)+0.0001;
    vec2 dir=sp/rr;
    float len=uWarp*(0.07+0.30*rr)*tief*0.85;
    sp+=dir*(uWarp*0.06*tief + len*aX.x);
    gl_Position=vec4(sp,0.0,1.0);
    float schweif=aX.x>0.5?0.22:1.0;
    float rOrb=length(vec2(sp.x/max(uAspX,0.001),sp.y/max(uAspY,0.001)))/max(uScale,0.001);
    float frei=smoothstep(1.05,1.45,rOrb);
    hell=(0.07+0.95*aR.w)*uStars*uWarp*2.8*schweif*frei;
    c=mix(vec3(0.80,0.90,1.0),uColA,0.20);
    vCol=vec4(c*hell,1.0);
    gl_PointSize=1.0;
    return;
  }
  if(uKind>2.5){
    // ---- Sternenfeld: eigener Raum, dreht nicht mit der Kugel ----
    vec2 sp=aP.xy;
    float tief=0.25+0.75*aP.z;
    sp+=vec2(sin(uTime*0.021+aR.z*6.2831),cos(uTime*0.017+aR.z*6.2831))*0.012*tief;
    float rr=length(sp)+0.0001;
    sp+=(sp/rr)*uWarp*(0.02+0.10*rr)*tief;                       // dezenter Zug nach aussen
    gl_Position=vec4(sp,0.0,1.0);
    // Funkeln + Werden/Vergehen: jeder Stern hat eigene Lebensdauer
    float leben=fract(uTime*(0.035+0.05*aR.x)+aR.z*3.17);
    float an=smoothstep(0.0,0.16,leben)*(1.0-smoothstep(0.62,1.0,leben));
    float tw=0.30+0.70*pow(0.5+0.5*sin(uTime*(0.7+aR.x*2.6)+aR.z*17.0),3.0);
    hell=(0.05+0.85*aR.w)*tw*an*uStars*(1.0+uWarp*0.85);
    c=mix(vec3(0.78,0.88,1.0),uColA,0.22+0.35*aR.w);
    groesse=(0.55+aR.y*1.7)*uDpr*(1.0+uWarp*0.35);
    vCol=vec4(c*hell*(uHalo>0.5?0.16:1.0),1.0);
    gl_PointSize=groesse*(1.0+uHalo*2.6);
    return;
  }

  vec3 pos;
  if(uKind>1.5){
    // ---- Neuronen: Zelle feuert, Welle laeuft vom Soma nach aussen ----
    vec3 q=aP;
    float w=fbm(q*1.7+vec3(uTime*0.06,-uTime*0.05,uTime*0.04))-0.5;
    q+=normalize(q+vec3(0.001))*w*0.05;
    // zusaetzlich zappeln die Fasern minimal in sich
    q+=0.004*vec3(sin(uTime*0.55+aR.z*2.0),cos(uTime*0.48+aR.z*1.7),sin(uTime*0.62+aR.z*1.4));
    q*=atem;
    pos=q;
    float rl=length(q);
    float zelle=aX.x, knoten=aX.y, bogen=aR.z;
    float takt=0.028+0.095*uFire;                                   // Feuerrate
    float zyk=fract(uTime*takt+h1(zelle*3.77));
    float fenster=0.17;
    float blitz=0.0;
    if(zyk<fenster){
      float prog=zyk/fenster;
      float t=(bogen-prog)*11.0;
      blitz=exp(-t*t)*(1.0-prog*0.25);
    }
    float grund=pow(max(uNeuro,0.001),0.4);
    hell=(0.20+knoten*0.22)*grund + blitz*(1.5+2.0*uFire)*uNeuro;
    hell*=1.0-smoothstep(0.44,0.88,rl);
    float perle=knoten*step(0.9965,fract(sin((zelle*57.3+aR.z*311.7+floor(uTime*2.3))*91.7)*43758.5453));
    hell+=perle*1.4*grund;
    c=mix(uColB,uColA,clamp(0.10+0.85*blitz+knoten*0.18+perle,0.0,1.0))*0.70;
    c=mix(c,vec3(1.0),clamp(blitz-0.55,0.0,0.6));                 // Blitzkopf zieht ins Weisse
    groesse=(0.85+aR.y*0.9+knoten*1.5+blitz*0.7)*uSize;
  } else if(uKind>0.5){
    // ---- naher Staub ----
    vec3 d=normalize(aP);
    float dens=fbm(d*3.1+vec3(uTime*0.05,uTime*0.04,-uTime*0.045));
    float lobe=fbm(d*1.05+vec3(uTime*0.012))-0.5;
    float r=(1.04+abs(aR.x)*0.75+lobe*0.10)*atem;
    pos=d*r;
    hell=0.30*(0.30+1.4*pow(dens,1.9))*uTurb;
    c=mix(uColB,uColA,0.55);
    groesse=(0.6+aR.y*1.1)*uSize*0.8;
  } else {
    // ---- Schale ----
    vec3 d=normalize(aP);
    float dens=fbm(d*3.1+vec3(uTime*0.055,uTime*0.041,-uTime*0.048));
    float lobe=fbm(d*1.05+vec3(uTime*0.013,-uTime*0.011,uTime*0.009))-0.5;
    float r=(1.0+lobe*0.13+aR.x*uThick*(0.45+0.95*dens))*atem;
    float pb=0.0;
    for(int i=0;i<3;i++){ float pr=uPulse[i]; if(pr>0.001){ float t=(r-pr)*7.0; pb+=exp(-t*t)*uPulse.w; } }
    pos=d*r;
    vec3 dr=lage(d);
    float rim=pow(1.0-abs(dr.z),3.0);
    hell=(0.15+uRim*rim)*(0.22+1.55*pow(dens,1.7));
    hell*=mix(1.0,0.62,smoothstep(0.15,1.0,dr.z));
    // umlaufende Lichtbaender: zwei Wellen gegeneinander, damit es nicht kreist wie ein Zeiger
    float az=atan(d.z,d.x);
    float band=pow(0.5+0.5*sin(az*3.0-uTime*0.34),6.0)*0.75
              +pow(0.5+0.5*sin(az*5.0+uTime*0.19+d.y*2.0),8.0)*0.55;
    hell*=1.0+uBand*band*(0.35+rim);
    float fl=step(0.9955,fract(sin((aR.z*311.7+floor(uTime*0.8))*91.7)*43758.5453));
    hell+=fl*uSparkle*2.8*(0.35+rim)+pb;
    c=mix(uColB,uColA,clamp(rim*1.25+0.10,0.0,1.0));
    groesse=(0.75+aR.y*1.5)*uSize;
  }

  vec3 p=lage(pos);
  float persp=3.0/(3.0-p.z);
  gl_Position=vec4(p.x*uScale*persp*uAspX, p.y*uScale*persp*uAspY, 0.0, 1.0);
  hell*=uGain*uFill;
  c=mix(c,vec3(1.0),clamp(hell-1.15,0.0,0.55));
  gl_PointSize=groesse*persp*uDpr*(1.0+uHalo*4.2);
  vCol=vec4(c*hell*(uHalo>0.5?0.16:1.0),1.0);
}`;

const TONE_VERT = `#version 300 es
layout(location=0) in vec2 aQ;
out vec2 vUv;
void main(){ vUv=aQ*0.5+0.5; gl_Position=vec4(aQ,0.0,1.0); }`;

const TONE_FRAG = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uTex;
uniform float uExp;
out vec4 frag;
void main(){
  vec3 h=texture(uTex,vUv).rgb;
  vec3 c=vec3(1.0)-exp(-max(h,0.0)*uExp);
  c=pow(c,vec3(0.92));
  float a=clamp(pow(max(max(c.r,c.g),c.b),0.62),0.0,1.0);
  frag=vec4(c,a);
}`;

const FRAG = `#version 300 es
precision highp float;
in vec4 vCol;
uniform float uHalo;
out vec4 frag;
void main(){
  float d=length(gl_PointCoord-0.5)*2.0;
  if(d>1.0) discard;
  float m=uHalo>0.5 ? exp(-d*d*2.0) : exp(-d*d*3.4);
  frag=vec4(vCol.rgb*m,1.0);
}`;

function compile(gl, src, type) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { console.error('Orb-Shader:', gl.getShaderInfoLog(s)); return null; }
  return s;
}

function upload(gl, pos, rnd, ex) {
  const vao = gl.createVertexArray(); gl.bindVertexArray(vao);
  const b0 = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b0);
  gl.bufferData(gl.ARRAY_BUFFER, pos, gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
  const b1 = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b1);
  gl.bufferData(gl.ARRAY_BUFFER, rnd, gl.STATIC_DRAW);
  gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 0, 0);
  if (ex) {
    const b2 = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b2);
    gl.bufferData(gl.ARRAY_BUFFER, ex, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(2); gl.vertexAttribPointer(2, 2, gl.FLOAT, false, 0, 0);
  }
  gl.bindVertexArray(null);
  return { vao, n: pos.length / 3 };
}

function sphereCloud(gl, n, shell) {
  const pos = new Float32Array(n * 3), rnd = new Float32Array(n * 4);
  const ga = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - (i + 0.5) / n * 2, rr = Math.sqrt(Math.max(0, 1 - y * y)), th = ga * i;
    pos[i * 3] = Math.cos(th) * rr + (Math.random() - 0.5) * 0.035;
    pos[i * 3 + 1] = y + (Math.random() - 0.5) * 0.035;
    pos[i * 3 + 2] = Math.sin(th) * rr + (Math.random() - 0.5) * 0.035;
    const u = Math.random() * 2 - 1;
    rnd[i * 4] = shell ? Math.sign(u) * Math.pow(Math.abs(u), 1.6) : u;
    rnd[i * 4 + 1] = Math.random();
    rnd[i * 4 + 2] = Math.random();
    rnd[i * 4 + 3] = Math.random();
  }
  return upload(gl, pos, rnd);
}

// Sterne im Bildschirmraum: gleichmaessig ueber die ganze Flaeche, drei Tiefen.
function starCloud(gl, n) {
  const pos = new Float32Array(n * 3), rnd = new Float32Array(n * 4);
  for (let i = 0; i < n; i++) {
    pos[i * 3] = Math.random() * 2.1 - 1.05;
    pos[i * 3 + 1] = Math.random() * 2.1 - 1.05;
    pos[i * 3 + 2] = Math.random();                       // Tiefe -> Parallaxe, Warp-Staerke
    rnd[i * 4] = Math.random();                           // Funkelrate
    rnd[i * 4 + 1] = Math.pow(Math.random(), 2.2);        // Groesse: wenige grosse
    rnd[i * 4 + 2] = Math.random();                       // Phase
    rnd[i * 4 + 3] = Math.pow(Math.random(), 2.6);        // Grundhelligkeit
  }
  return upload(gl, pos, rnd);
}

function streakCloud(gl, n) {
  const pos = new Float32Array(n * 6), rnd = new Float32Array(n * 8), ex = new Float32Array(n * 4);
  for (let i = 0; i < n; i++) {
    const x = Math.random() * 2.1 - 1.05, y = Math.random() * 2.1 - 1.05, z = Math.random();
    const hell = Math.pow(Math.random(), 2.0);
    for (let v = 0; v < 2; v++) {
      const o = i * 2 + v;
      pos[o * 3] = x; pos[o * 3 + 1] = y; pos[o * 3 + 2] = z;
      rnd[o * 4] = Math.random(); rnd[o * 4 + 1] = Math.random(); rnd[o * 4 + 2] = Math.random(); rnd[o * 4 + 3] = hell;
      ex[o * 2] = v; ex[o * 2 + 1] = 0;
    }
  }
  return upload(gl, pos, rnd, ex);
}

// Neuronen: Somata im Innern, drei Verzweigungsebenen, dichte Punktketten.
// aR.z = Bogenlage 0..1 vom Soma zur Spitze (die Blitzwelle laeuft darueber),
// aX = (Zell-Nummer, Knoten-Flag).
function neuronCloud(gl, somata, dichte) {
  const pos = [], rnd = [], ex = [];
  const rndUnit = () => {
    let x, y, z, l;
    do { x = Math.random() * 2 - 1; y = Math.random() * 2 - 1; z = Math.random() * 2 - 1; l = Math.hypot(x, y, z); } while (l < 0.001);
    return [x / l, y / l, z / l];
  };
  const ARC = [0.0, 0.34, 0.66, 1.0];

  for (let s = 0; s < somata; s++) {
    const push = (p, arc, node, size) => {
      pos.push(p[0], p[1], p[2]);
      rnd.push(Math.random(), size, arc, Math.random());
      ex.push(s, node);
    };
    const u = rndUnit(), rad = 0.08 + Math.pow(Math.random(), 0.6) * 0.32;
    const soma = [u[0] * rad, u[1] * rad, u[2] * rad];
    for (let k = 0; k < 16; k++) {
      const d = rndUnit(), r = Math.random() * 0.014;
      push([soma[0] + d[0] * r, soma[1] + d[1] * r, soma[2] + d[2] * r], 0.0, 1, Math.random());
    }
    const grow = (start, dir, len, level) => {
      const seg = Math.max(10, Math.round(dichte * (level === 0 ? 40 : level === 1 ? 28 : 18)));
      const bend = rndUnit();
      let p = start.slice(), d = dir.slice();
      const a0 = ARC[level], a1 = ARC[level + 1];
      for (let i = 1; i <= seg; i++) {
        const f = i / seg;
        d = [d[0] + bend[0] * 0.05, d[1] + bend[1] * 0.05, d[2] + bend[2] * 0.05];
        const l = Math.hypot(d[0], d[1], d[2]);
        d = [d[0] / l, d[1] / l, d[2] / l];
        const step = len / seg;
        p = [p[0] + d[0] * step, p[1] + d[1] * step, p[2] + d[2] * step];
        const jit = 0.006 * (1 + level);
        push([p[0] + (Math.random() - 0.5) * jit, p[1] + (Math.random() - 0.5) * jit, p[2] + (Math.random() - 0.5) * jit],
          a0 + f * (a1 - a0), 0, 0.2 + Math.random() * 0.6);
      }
      if (level < 2) {
        push(p, a1, 1, 0.5);
        const kinder = level === 0 ? 3 : 2;
        for (let c = 0; c < kinder; c++) {
          const nd = rndUnit();
          const mix = [d[0] * 0.62 + nd[0] * 0.55, d[1] * 0.62 + nd[1] * 0.55, d[2] * 0.62 + nd[2] * 0.55];
          const ml = Math.hypot(mix[0], mix[1], mix[2]);
          grow(p, [mix[0] / ml, mix[1] / ml, mix[2] / ml], len * 0.72, level + 1);
        }
      }
    };
    for (let t = 0; t < 3; t++) grow(soma, rndUnit(), 0.30 + Math.random() * 0.16, 0);
  }
  return upload(gl, new Float32Array(pos), new Float32Array(rnd), new Float32Array(ex));
}

// Als klassisches Skript geladen statt als ES-Modul: die Ladereihenfolge ist
// damit fest (vor router.js), und ein Modul-Schnipsel mit Cache-Busting-Query
// in index.html entfaellt. Aus 'export function' wird deshalb eine globale
// Fabrik -- der Rest des Entwurfs bleibt woertlich.
window.argusCreateOrb = function createOrb(canvas, opts = {}) {
  const gl = canvas.getContext('webgl2', { alpha: true, antialias: false, premultipliedAlpha: false, preserveDrawingBuffer: true });
  if (!gl) { console.warn('Orb: WebGL2 fehlt.'); return null; }

  const P = {
    count: opts.count || 90000,
    dust: opts.dust != null ? opts.dust : 18000,
    stars: opts.stars != null ? opts.stars : 4200,
    starGain: opts.starGain != null ? opts.starGain : 1,
    somata: opts.somata || 22,
    neuroDensity: opts.neuroDensity != null ? opts.neuroDensity : 1,
    rim: opts.rim != null ? opts.rim : 1.0,
    size: opts.size != null ? opts.size : 1.5,
    scale: opts.scale != null ? opts.scale : 0.6,
    turb: opts.turb != null ? opts.turb : 1.0,
    tilt: opts.tilt != null ? opts.tilt : 0.34,
    halo: opts.halo != null ? opts.halo : 0.45,
    neuro: opts.neuro != null ? opts.neuro : 1.0,
    fire: opts.fire != null ? opts.fire : 1.0,
    band: opts.band != null ? opts.band : 1.0,
    exposure: opts.exposure != null ? opts.exposure : 1.6
  };

  function link(vsrc, fsrc, tag) {
    const a = compile(gl, vsrc, gl.VERTEX_SHADER), b = compile(gl, fsrc, gl.FRAGMENT_SHADER);
    if (!a || !b) return null;
    const p = gl.createProgram();
    gl.attachShader(p, a); gl.attachShader(p, b); gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) { console.error('Orb-Link ' + tag + ':', gl.getProgramInfoLog(p)); return null; }
    return p;
  }
  const prog = link(VERT, FRAG, 'punkte');
  if (!prog) return null;
  gl.useProgram(prog);

  const floatOk = !!gl.getExtension('EXT_color_buffer_float');
  const progTone = floatOk ? link(TONE_VERT, TONE_FRAG, 'tone') : null;
  let fbo = null, tex = null, quad = null, uTex = null, uExp = null;
  if (progTone) {
    fbo = gl.createFramebuffer();
    tex = gl.createTexture();
    quad = gl.createVertexArray();
    gl.bindVertexArray(quad);
    const qb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, qb);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    uTex = gl.getUniformLocation(progTone, 'uTex');
    uExp = gl.getUniformLocation(progTone, 'uExp');
  }

  const U = {};
  for (const k of ['uTime', 'uAng', 'uTilt', 'uScale', 'uAspX', 'uAspY', 'uThick', 'uRim', 'uBreath', 'uPhase',
    'uLevel', 'uSize', 'uHalo', 'uFill', 'uSparkle', 'uKind', 'uGain', 'uDpr', 'uTurb', 'uNeuro', 'uFire',
    'uBand', 'uStars', 'uWarp', 'uColA', 'uColB', 'uPulse']) U[k] = gl.getUniformLocation(prog, k);

  let shell = sphereCloud(gl, P.count, true);
  let starC = P.stars > 0 ? starCloud(gl, P.stars) : null;
  let streakC = P.stars > 0 ? streakCloud(gl, Math.round(P.stars * 0.12)) : null;
  let neuroC = neuronCloud(gl, P.somata, P.neuroDensity);

  gl.disable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.ONE, gl.ONE);

  let dpr = 1, w = 1, h = 1;
  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    const r = canvas.getBoundingClientRect();
    w = Math.max(1, Math.round(r.width * dpr)); h = Math.max(1, Math.round(r.height * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0, 0, w, h);
    if (tex) {
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, w, h, 0, gl.RGBA, gl.HALF_FLOAT, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }
  }
  resize();
  const ro = new ResizeObserver(resize); ro.observe(canvas);

  const cur = JSON.parse(JSON.stringify(PRESETS.idle));
  let target = PRESETS.idle, name = 'idle';
  let ang = 0, phase = 0, warp = 0, warpT = 0, t0 = performance.now(), last = t0, raf = 0;
  let level = 0, levelZiel = 0, fill = 0.02;
  const pulses = [];
  let nextPulse = 0;

  const ez = (a, b, k) => a + (b - a) * k;

  function frame(now) {
    const dt = Math.min(50, now - last); last = now;
    const t = (now - t0) / 1000;
    const k = 1 - Math.pow(0.001, dt / 1000);

    for (const key of ['rot', 'thick', 'breath', 'breathHz', 'sparkle', 'gain', 'dustA', 'neuro', 'fire', 'band', 'stars'])
      cur[key] = ez(cur[key], target[key], k * 0.5);
    for (let i = 0; i < 3; i++) {
      cur.colA[i] = ez(cur.colA[i], target.colA[i], k * 0.5);
      cur.colB[i] = ez(cur.colB[i], target.colB[i], k * 0.5);
    }
    level = ez(level, levelZiel, 1 - Math.pow(0.05, dt / 1000));
    fill = Math.min(1, fill + dt / 1200);
    // Atmung ueber akkumulierte Phase: Frequenzwechsel ohne Sprung.
    phase += cur.breathHz * dt / 1000;
    ang += cur.rot * (1 + level * 0.2) * dt / 1000;
    if (warpT) {
      const x = (now - warpT) / 1200;
      if (x >= 1) { warpT = 0; warp = 0; }
      else {
        const sm = (a, b, v) => { const u = Math.min(1, Math.max(0, (v - a) / (b - a))); return u * u * (3 - 2 * u); };
        warp = sm(0, 0.25, x) * (1 - sm(0.4, 1.0, x));
      }
    } else warp = 0;

    if (name === 'thinking' && now > nextPulse) { pulses.push({ r: 0.2 }); nextPulse = now + 1100 + Math.random() * 700; }
    for (let i = pulses.length - 1; i >= 0; i--) { pulses[i].r += dt / 1500; if (pulses[i].r > 1.9) pulses.splice(i, 1); }

    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.viewport(0, 0, w, h);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(prog);
    gl.uniform1f(U.uTime, t);
    gl.uniform1f(U.uAng, ang);
    gl.uniform1f(U.uTilt, P.tilt + Math.sin(t * 0.06) * 0.09);
    gl.uniform1f(U.uScale, P.scale);
    gl.uniform1f(U.uAspX, w >= h ? h / w : 1);
    gl.uniform1f(U.uAspY, w >= h ? 1 : w / h);
    gl.uniform1f(U.uThick, cur.thick);
    gl.uniform1f(U.uRim, P.rim);
    gl.uniform1f(U.uBreath, cur.breath);
    gl.uniform1f(U.uPhase, phase);
    gl.uniform1f(U.uLevel, level);
    gl.uniform1f(U.uSize, P.size);
    gl.uniform1f(U.uFill, fill);
    gl.uniform1f(U.uSparkle, cur.sparkle);
    gl.uniform1f(U.uGain, cur.gain * (1 + level * 0.22));
    gl.uniform1f(U.uDpr, dpr);
    gl.uniform1f(U.uTurb, P.turb * cur.dustA);
    gl.uniform1f(U.uNeuro, cur.neuro * P.neuro);
    gl.uniform1f(U.uFire, cur.fire * P.fire);
    gl.uniform1f(U.uBand, cur.band * P.band);
    gl.uniform1f(U.uStars, cur.stars * P.starGain);
    gl.uniform1f(U.uWarp, warp);
    gl.uniform3fv(U.uColA, cur.colA); gl.uniform3fv(U.uColB, cur.colB);
    gl.uniform4f(U.uPulse, pulses[0] ? pulses[0].r : 0, pulses[1] ? pulses[1].r : 0, pulses[2] ? pulses[2].r : 0, 0.5);

    const zeichne = (cloud, kind) => {
      if (!cloud) return;
      gl.uniform1f(U.uKind, kind);
      gl.bindVertexArray(cloud.vao);
      if (P.halo > 0.01) { gl.uniform1f(U.uHalo, 1); gl.drawArrays(gl.POINTS, 0, cloud.n); }
      gl.uniform1f(U.uHalo, 0); gl.drawArrays(gl.POINTS, 0, cloud.n);
    };
    zeichne(starC, 3);
    // Neuronen und Schale drehen GEGENEINANDER -- ein Koerper, zwei Takte.
    gl.uniform1f(U.uAng, -ang * 0.45);
    zeichne(neuroC, 2);
    gl.uniform1f(U.uAng, ang);
    zeichne(shell, 0);
    gl.bindVertexArray(null);

    if (progTone) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, w, h);
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.disable(gl.BLEND);
      gl.useProgram(progTone);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.uniform1i(uTex, 0);
      gl.uniform1f(uExp, P.exposure);
      gl.bindVertexArray(quad); gl.drawArrays(gl.TRIANGLES, 0, 3); gl.bindVertexArray(null);
    }

    raf = requestAnimationFrame(frame);
  }
  raf = requestAnimationFrame(frame);

  return {
    setState(s) {
      if (!PRESETS[s] || s === name) return;
      name = s; target = PRESETS[s];
      if (s === 'thinking') nextPulse = 0;
      warpT = performance.now();   // 6-Sekunden-Warp: Punkt -> Streifen -> Punkt
    },
    state() { return name; },
    // Ergaenzt fuer die Einbindung: der Router haelt den Orb beim Verlassen der
    // Live-Ansicht an. Ein erneutes createOrb() auf demselben Canvas bekaeme
    // denselben GL-Kontext, legte aber alle Programme und Puffer neu an.
    pause() { cancelAnimationFrame(raf); raf = 0; },
    resume() { if (!raf) { last = performance.now(); raf = requestAnimationFrame(frame); } },
    setLevel(v) { levelZiel = Math.max(0, Math.min(1, v)); },
    pulse(stark) { if (pulses.length < 3) pulses.push({ r: 0.2 }); if (stark) warpT = performance.now(); },
    setParams(o) {
      const wolke = (o.count != null && o.count !== P.count);
      const sterne = (o.stars != null && o.stars !== P.stars);
      const nerven = (o.somata != null && o.somata !== P.somata) || (o.neuroDensity != null && o.neuroDensity !== P.neuroDensity);
      Object.assign(P, o);
      if (wolke) shell = sphereCloud(gl, P.count, true);
      if (sterne) {
        starC = P.stars > 0 ? starCloud(gl, P.stars) : null;
        streakC = P.stars > 0 ? streakCloud(gl, Math.round(P.stars * 0.12)) : null;
      }
      if (nerven) neuroC = neuronCloud(gl, P.somata, P.neuroDensity);
    },
    stop() { cancelAnimationFrame(raf); ro.disconnect(); }
  };
}
