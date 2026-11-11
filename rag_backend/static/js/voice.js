// Abbruch des laufenden Turns. Das Backend merkt den Abbruch ueber
// request.is_disconnected() und persistiert die Teilantwort -- hier genuegt abort().
window.__argusAbort = null;

window.argusChatStop = function () {
  if (window.__argusAbort) {
    window.__argusAbort.abort();
    window.__argusAbort = null;
    return true;
  }
  return false;
};

window.argusChatBusy = function () { return !!window.__argusAbort; };

window.argusChatStream = async function (message, cb) {
  cb = cb || {};
  // Ein laufender Turn wird abgeloest, statt zwei Streams nebeneinander laufen zu lassen.
  window.argusChatStop();
  const ctl = new AbortController();
  window.__argusAbort = ctl;
  const finish = function () { if (window.__argusAbort === ctl) window.__argusAbort = null; };
  let res;
  try {
    res = await fetch('/v1/dashboard/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      signal: ctl.signal,
      // Kein history-Feld: das Backend liest den Verlauf an der session_id aus der
      // Datenbank. Ein mitgeschickter Verlauf liesse den Client eine Vorgeschichte
      // erfinden. Ohne session_id legt das Backend eine neue Sitzung an.
      body: JSON.stringify({
        message: message,
        session_id: cb.sessionId || null,
        images: cb.images || []
      })
    });
  } catch (e) {
    finish();
    window.setOrbState('idle');
    // Ein Abbruch ist kein Fehler -- Teilantwort behalten, normal freigeben.
    if (e && e.name === 'AbortError') { if (cb.onAborted) cb.onAborted(); else if (cb.onDone) cb.onDone(); return; }
    if (cb.onError) cb.onError(e);
    return;
  }
  if (!res.ok) { finish(); window.setOrbState('idle'); if (cb.onError) cb.onError(new Error('HTTP ' + res.status)); return; }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let gotDone = false;
  try {
    while (true) {
      const r = await reader.read();
      if (r.done) break;
      buf += dec.decode(r.value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const raw = buf.slice(0, idx).trim(); buf = buf.slice(idx + 2);
        if (!raw.startsWith('data:')) continue;
        let ev; try { ev = JSON.parse(raw.slice(5).trim()); } catch (e) { continue; }
        if (ev.type === 'session') { if (cb.onSession) cb.onSession(ev.session_id); }
        else if (ev.type === 'state') { window.setOrbState(ev.value); }
        else if (ev.type === 'confirmation') { if (cb.onConfirmation) cb.onConfirmation(ev); }
        else if (ev.type === 'reasoning') { window.setOrbState('thinking'); if (cb.onReasoning) cb.onReasoning(ev.text); }
        else if (ev.type === 'tool') { window.setOrbTool(ev.emoji); if (cb.onTool) cb.onTool(ev.label, ev.emoji, ev.index); }
        else if (ev.type === 'sources') { if (cb.onSources) cb.onSources(ev.items || []); }
        else if (ev.type === 'tool_blocked') { if (cb.onToolBlocked) cb.onToolBlocked(ev.index); }
        else if (ev.type === 'doc_sources') { if (cb.onDocSources) cb.onDocSources(ev.items || []); }
        else if (ev.type === 'tool_done') { if (cb.onToolDone) cb.onToolDone(ev.index, ev.dauer_ms); }
        else if (ev.type === 'token') { if (cb.onToken) cb.onToken(ev.text); }
        else if (ev.type === 'done') { gotDone = true; window.setOrbState('idle'); if (cb.onDone) cb.onDone(); }
      }
    }
  } catch (e) {
    // Netzabbruch mitten im Stream: ohne diesen Zweig bliebe busy fuer immer stehen.
    finish();
    window.setOrbState('idle');
    if (e && e.name === 'AbortError') { if (cb.onAborted) cb.onAborted(); else if (cb.onDone) cb.onDone(); return; }
    if (cb.onError) cb.onError(e);
    return;
  }
  finish();
  if (!gotDone) {
    // Serverseitig abgebrochen: freigeben, Teilantwort bleibt nutzbar.
    window.setOrbState('idle');
    if (cb.onDone) cb.onDone();
  }
};

// ===== Tonpegel -> Orb =====
// Der Orb soll auf die Stimme reagieren, nicht nur auf Zustandswechsel: beim
// Zuhoeren auf das Mikro, beim Antworten auf die Sprachausgabe. Ohne das wirkt er
// wie eine Animation, die zufaellig nebenher laeuft.
// Der Wert landet in window.__orbLevel (0..1); der Renderer liest ihn jeden Frame.
window.__orbLevel = 0;

window.argusAudioLevel = (function () {
  var ctx = null, analyser = null, quelle = null, daten = null, raf = 0;

  function ensureCtx() {
    if (!ctx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
    }
    // Nach einer Nutzergeste erlaubt der Browser das Fortsetzen; ohne bleibt der
    // Kontext 'suspended' und der Pegel stuende dauerhaft auf null.
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }

  function messen() {
    if (!analyser) return;
    analyser.getByteTimeDomainData(daten);
    // Effektivwert um die Nulllinie: bildet Lautstaerke besser ab als ein Maximum,
    // das schon ein einzelner Knacks ausreizt.
    var summe = 0;
    for (var i = 0; i < daten.length; i++) {
      var v = (daten[i] - 128) / 128;
      summe += v * v;
    }
    var rms = Math.sqrt(summe / daten.length);
    var ziel = Math.min(1, rms * 3.2);
    // Schnell hoch, langsam runter: so bleibt die Bewegung lesbar statt zu zappeln.
    window.__orbLevel += (ziel - window.__orbLevel) * (ziel > window.__orbLevel ? 0.45 : 0.12);
    raf = requestAnimationFrame(messen);
  }

  function anhaengen(node) {
    var c = ensureCtx();
    if (!c) return false;
    trennen();
    try {
      analyser = c.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.6;
      daten = new Uint8Array(analyser.fftSize);
      quelle = node;
      quelle.connect(analyser);
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(messen);
      return true;
    } catch (e) { return false; }
  }

  function trennen() {
    cancelAnimationFrame(raf);
    raf = 0;
    if (quelle) { try { quelle.disconnect(analyser); } catch (e) { /* schon getrennt */ } }
    quelle = null;
    analyser = null;
    window.__orbLevel = 0;
  }

  return {
    // Mikro: der Stream wird NICHT an die Ausgabe weitergereicht -- sonst hoerte
    // man sich selbst.
    vomMikro: function (stream) {
      var c = ensureCtx();
      if (!c) return;
      anhaengen(c.createMediaStreamSource(stream));
    },
    // Sprachausgabe: hier muss die Quelle zusaetzlich an die Ausgabe, sonst bleibt
    // die Antwort stumm, sobald sie durch den Analyser laeuft.
    vomAudio: function (el) {
      var c = ensureCtx();
      if (!c) return;
      try {
        var src = el.__argusNode || c.createMediaElementSource(el);
        el.__argusNode = src;
        if (anhaengen(src)) src.connect(c.destination);
      } catch (e) { /* Element schon verbunden oder nicht erlaubt */ }
    },
    stop: trennen
  };
})();

// ===== Live-Ansicht: Sprachmodus =====
// Kein Verlauf, nur der laufende Wortwechsel als Untertitel -- gelesen wird im Chat.
// Mikro ist die Hauptbedienung (Leertaste), Tippen die Ausweichmoeglichkeit.
(function () {
  function initVoiceMode() {
    const lab = document.getElementById('vc-statelabel');
    const capUser = document.getElementById('vc-caption-user');
    const capAi = document.getElementById('vc-caption-ai');
    const micBtn = document.getElementById('vc-mic');
    const ttsBtn = document.getElementById('vc-tts');
    const kbdBtn = document.getElementById('vc-keyboard');
    const typebar = document.getElementById('vc-typebar');
    const txt = document.getElementById('vc-text');
    const sendBtn = document.getElementById('vc-send');
    const hint = document.getElementById('vc-hint');

    if (!lab || !micBtn || !capAi) return;

    const LBL = { idle: 'Bereit', listening: 'Hört zu', research: 'Recherchiert',
                  thinking: 'Denkt nach', speaking: 'Spricht' };
    // Zweite Zeile: die Beschriftung nennt den Zustand, das hier den Grund dafuer.
    const SUB = { idle: 'Warte auf Eingabe', listening: 'Mikrofon offen',
                  research: 'Suche im Netz', thinking: 'Formuliert die Antwort',
                  speaking: 'Sprachausgabe laeuft' };
    const CLS = ['is-listening', 'is-thinking', 'is-speaking', 'is-research'];
    // Eigener Knoten fuer das Wort: der Punkt daneben soll beim Wechsel stehen bleiben.
    const wort = document.getElementById('vc-statewort');
    const sub = document.getElementById('vc-substate');

    function setState(s) {
      window.setOrbState(s);
      if (wort) wort.textContent = LBL[s] || s;
      if (sub) sub.textContent = SUB[s] || '';
      CLS.forEach(function (c) { lab.classList.remove(c); });
      if (s !== 'idle') lab.classList.add('is-' + s);
    }

    function showUser(text) {
      capUser.textContent = kurz(text);
      capUser.classList.toggle('is-on', !!text);
    }
    // Eine Zeile, hart gekuerzt. Alles Laengere gehoert in die Chat-Ansicht.
    function kurz(text, max) {
      text = String(text || '').replace(/\s+/g, ' ').trim();
      max = max || 74;
      return text.length > max ? text.slice(0, max - 1) + '…' : text;
    }
    function showAi(text) {
      capAi.textContent = kurz(text);
      capAi.classList.toggle('is-on', !!text);
    }
    function note(text) {
      showAi(text);
      capAi.style.color = '#8a93a6';
      setTimeout(function () { capAi.style.color = ''; }, 4000);
    }

    // Eine Sitzung fuer alle gesprochenen Turns: ohne sie liest das Backend einen
    // leeren Verlauf und kann keine Rueckfrage beantworten.
    let liveSessionId = null, ttsOn = true, busy = false, audio = null;

    function setBusy(on) {
      busy = on;
      micBtn.classList.toggle('is-stop', on);
      if (hint) hint.textContent = on ? 'Esc bricht ab' : 'Leertaste zum Sprechen · Esc bricht ab';
    }

    function stopAudio() {
      if (audio) { audio.pause(); audio = null; }
      window.argusAudioLevel.stop();
    }

    function submit(text) {
      if (busy || !text) return;
      setBusy(true);
      stopAudio();
      showUser(text);
      showAi('');
      let acc = '';
      window.argusChatStream(text, {
        sessionId: liveSessionId,
        onSession: function (sid) { if (sid) liveSessionId = sid; },
        onReasoning: function () { setState('thinking'); },
        // Entschieden wird in der Chat-Ansicht; hier nur der Hinweis, damit die
        // Wartezeit erklaert ist.
        onConfirmation: function (ev) {
          showAi(ev.phase === 'pending'
            ? '🔐 Freigabe nötig — bitte im Chat bestätigen …'
            : ('🔐 Freigabe ' + (ev.status || '')));
        },
        onTool: function (label) { setState('research'); showAi(kurz(label)); },
        // Bewusst NICHT die Antwort: dafuer gibt es die Chat-Ansicht. Hier steht nur,
        // dass gerade gesprochen wird -- der Orb soll das Bild fuellen, nicht Text.
        onToken: function (tk) { acc += tk; setState('speaking'); },
        onDone: async function () { setBusy(false); showAi(''); await speak(acc); },
        onAborted: function () { setBusy(false); setState('idle'); },
        onError: function (e) { setBusy(false); note('Fehler: ' + ((e && e.message) || e)); setState('idle'); }
      });
    }

    // Gesprochene Absaetze bleiben als Blob liegen: ein zweites VORLESEN
    // desselben Textes soll nicht noch einmal synthetisiert werden. Der
    // tts-service cacht ebenfalls -- das hier spart zusaetzlich den Transfer.
    // Klein gehalten, ein Absatz Audio sind schnell ein paar hundert KB.
    const ttsCache = new Map();
    const TTS_CACHE_MAX = 8;

    async function ttsBlob(text) {
      const treffer = ttsCache.get(text);
      if (treffer) {
        // Neu einsortieren, damit Haeufiges nicht herausfaellt.
        ttsCache.delete(text);
        ttsCache.set(text, treffer);
        return treffer;
      }
      const r = await fetch('/v1/dashboard/tts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: text })
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const blob = await r.blob();
      ttsCache.set(text, blob);
      while (ttsCache.size > TTS_CACHE_MAX) ttsCache.delete(ttsCache.keys().next().value);
      return blob;
    }

    async function speak(text) {
      if (!ttsOn || !text) { setState('idle'); return; }
      try {
        setState('speaking');
        const url = URL.createObjectURL(await ttsBlob(text));
        audio = new Audio(url);
        audio.crossOrigin = 'anonymous';
        audio.onended = function () {
          URL.revokeObjectURL(url); audio = null;
          window.argusAudioLevel.stop(); setState('idle');
        };
        audio.onerror = function () {
          URL.revokeObjectURL(url); audio = null;
          window.argusAudioLevel.stop(); setState('idle');
        };
        await audio.play();
        // Erst nach play(): vorher ist der Audio-Kontext in manchen Browsern noch
        // gesperrt und der Pegel bliebe auf null.
        window.argusAudioLevel.vomAudio(audio);
      } catch (e) { setState('idle'); }
    }

    let rec = null, chunks = [], recording = false;

    async function startMic() {
      // Laufende Sprachausgabe abbrechen: dazwischenreden soll gehen, wie beim
      // Telefonat. Sonst nimmt das Mikro die eigene Stimme des Assistenten auf.
      stopAudio();
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        window.argusAudioLevel.vomMikro(stream);
        rec = new MediaRecorder(stream);
        chunks = [];
        rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
        rec.onstop = async function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          window.argusAudioLevel.stop();
          micBtn.classList.remove('rec');
          recording = false;
          const blob = new Blob(chunks, { type: (rec && rec.mimeType) || 'audio/webm' });
          setState('thinking');
          try {
            const fd = new FormData();
            fd.append('file', blob, 'audio.webm');
            const r = await fetch('/v1/dashboard/stt', { method: 'POST', body: fd });
            if (r.status === 401) { if (window.argusAuth) window.argusAuth.sessionLost(); return; }
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const j = await r.json();
            const text = (j.text || '').trim();
            if (text) submit(text);
            else { note('Nichts verstanden.'); setState('idle'); }
          } catch (e) { note('Spracherkennung: ' + (e.message || e)); setState('idle'); }
        };
        rec.start();
        recording = true;
        micBtn.classList.add('rec');
        setState('listening');
      } catch (e) { note('Mikrofon nicht verfügbar: ' + (e.message || e)); }
    }

    function stopMic() { if (rec && recording) rec.stop(); }

    micBtn.onclick = function () {
      if (busy) { window.argusChatStop(); stopAudio(); setState('idle'); return; }
      recording ? stopMic() : startMic();
    };
    ttsBtn.onclick = function () {
      ttsOn = !ttsOn;
      ttsBtn.classList.toggle('off', !ttsOn);
      if (!ttsOn) stopAudio();
    };
    kbdBtn.onclick = function () {
      typebar.hidden = !typebar.hidden;
      if (!typebar.hidden) txt.focus();
    };
    sendBtn.onclick = function () {
      const t = txt.value.trim();
      if (t) { txt.value = ''; submit(t); }
    };
    txt.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); sendBtn.click(); }
      if (e.key === 'Escape') { e.preventDefault(); typebar.hidden = true; }
    });

    // Leertaste als Sprechtaste -- aber nur, wenn gerade kein Textfeld den Fokus hat.
    document.addEventListener('keydown', function (e) {
      const live = document.getElementById('view-live');
      if (!live || !live.classList.contains('active')) return;
      const tag = (document.activeElement && document.activeElement.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (e.code === 'Space') { e.preventDefault(); micBtn.click(); }
      if (e.key === 'Escape' && busy) { e.preventDefault(); window.argusChatStop(); stopAudio(); setState('idle'); }
    });

    // Orb-Zustaende, die anderswo gesetzt werden, sollen die Beschriftung mitnehmen.
    const originalSetOrbState = window.setOrbState;
    window.setOrbState = function (s) {
      if (originalSetOrbState) originalSetOrbState(s);
      if (s !== 'tool' && lab) {
        lab.textContent = LBL[s] || s;
        CLS.forEach(function (c) { lab.classList.remove(c); });
        if (s !== 'idle') lab.classList.add('is-' + s);
      }
    };

    // Pegel auf den Mikro-Knopf spiegeln. Laeuft nur, solange die Ansicht offen ist.
    let levelRaf = 0;
    function levelLoop() {
      micBtn.style.setProperty('--level', (window.__orbLevel || 0).toFixed(3));
      levelRaf = requestAnimationFrame(levelLoop);
    }
    levelLoop();

    // Beim Verlassen der Ansicht Ton und Aufnahme beenden -- sonst spricht der
    // Assistent in die Chat-Ansicht hinein.
    // Vorlesen auch aus der Chat-Ansicht. speak() setzt nebenbei den Orb-Zustand --
    // das ist gewollt: gesprochen wird gesprochen, egal von welcher Seite aus.
    window.argusSprich = function (text) { ttsOn = true; return speak(text); };

    window.argusLive = {
      leave: function () {
        stopAudio();
        if (recording) stopMic();
        cancelAnimationFrame(levelRaf);
        levelRaf = 0;
      },
      enter: function () { if (!levelRaf) levelLoop(); }
    };
  }

  if (document.readyState !== 'loading') initVoiceMode();
  else document.addEventListener('DOMContentLoaded', initVoiceMode);
})();

