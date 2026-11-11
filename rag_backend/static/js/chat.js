// ===== Chat-Ansicht: Verlauf aus der Datenbank, Streaming, Werkzeug-Schritte =====
let chatCurrentId = null;
let chatBusy = false;

// Bildanhaenge reisen als data-URI mit der Nachricht -- keine Datei auf der Platte.
// Die Grenzen spiegeln die des Backends, damit der Nutzer das Limit SIEHT, statt
// dass stillschweigend weggefiltert wird.
let chatAttachments = [];
let chatMultimodal = false;
const CHAT_MAX_IMAGES = 2;
const CHAT_MAX_IMAGE_CHARS = 10 * 1024 * 1024;

function chatClearAttachments() {
  chatAttachments = [];
  chatRenderAttachments();
}

function chatRenderAttachments() {
  const strip = $('chat-attachments');
  if (!strip) return;
  strip.innerHTML = '';
  strip.style.display = chatAttachments.length ? 'flex' : 'none';
  chatAttachments.forEach(function (uri, i) {
    const item = document.createElement('span');
    item.className = 'chat-attach';
    const im = document.createElement('img');
    im.src = uri;
    im.alt = 'Anhang ' + (i + 1);
    const del = document.createElement('span');
    del.className = 'chat-attach-del';
    del.textContent = '×';
    del.title = 'Anhang entfernen';
    del.addEventListener('click', function () {
      chatAttachments.splice(i, 1);
      chatRenderAttachments();
    });
    item.appendChild(im);
    item.appendChild(del);
    strip.appendChild(item);
  });
}

function chatAddFiles(files) {
  const list = Array.prototype.slice.call(files || []).filter(function (f) {
    return f && /^image\//.test(f.type);
  });
  list.forEach(function (f) {
    if (chatAttachments.length >= CHAT_MAX_IMAGES) return;
    const fr = new FileReader();
    fr.onload = function () {
      const uri = String(fr.result || '');
      if (!/^data:image\//.test(uri)) return;
      if (uri.length > CHAT_MAX_IMAGE_CHARS) {
        alert('Bild ist zu groß (' + Math.round(uri.length / 1048576) + ' MB, erlaubt sind '
          + Math.round(CHAT_MAX_IMAGE_CHARS / 1048576) + ' MB).');
        return;
      }
      if (chatAttachments.length >= CHAT_MAX_IMAGES) return;
      chatAttachments.push(uri);
      chatRenderAttachments();
    };
    fr.readAsDataURL(f);
  });
}

// Der Anhang-Knopf erscheint nur, wenn LLM_ENABLE_MULTIMODAL aktiv ist. Ein Knopf,
// der ein Bild annimmt, das die Engine anschliessend nicht ansehen kann, ist schlimmer
// als kein Knopf -- man haelt die Antwort dann fuer eine Bildbeschreibung.
async function chatCheckMultimodal() {
  try {
    const s = await api('/v1/dashboard/config');
    chatMultimodal = !!(s.config && s.config.multimodal);
    // Der Kopf der Unterhaltung zeigt das Modell -- die Angabe kommt nur hierher.
    window.argusModell = (s.model || '').split('/').pop();
    chatSetTitle($('chat-title').textContent, chatKopfMeta);
  } catch (e) { chatMultimodal = false; }
  const btn = $('chat-attach');
  if (btn) btn.style.display = chatMultimodal ? '' : 'none';
  if (!chatMultimodal) chatClearAttachments();
}

// Markdown bewusst eng statt per Bibliothek: der Text kommt vom Modell und enthaelt
// Zitate aus gelesenen Webseiten -- der Indirect-Injection-Pfad. Erst alles escapen,
// danach entsteht Markup, und nur fuer die Konstrukte unten.
function chatEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function chatMd(src) {
  const blocks = [];
  // Platzhalter je Aufruf zufaellig: ein fester Marker koennte im Modelltext stehen
  // und dort einen fremden Code-Block einsetzen.
  const TOK = 'ARGUSCODE' + Math.random().toString(36).slice(2, 10) + '_';
  // Code-Bloecke zuerst herausloesen, sonst deuten die Inline-Regeln Sternchen
  // innerhalb von Code als Formatierung.
  let t = String(src || '').replace(/```(\w*)\n?([\s\S]*?)```/g, function (_, lang, code) {
    blocks.push({ lang: lang || '', code: code.replace(/\n$/, '') });
    return TOK + (blocks.length - 1) + TOK;
  });

  t = chatEsc(t);

  // Quellenverweise [1] als Pillen. NACH dem Escapen, und nur wenn keine
  // Klammer folgt -- sonst zerlegt die Regel Markdown-Links wie [Text](url).
  t = t.replace(/\[(\d{1,2})\](?!\()/g,
                '<span class="cite" data-nr="$1" tabindex="0" role="link">$1</span>');

  t = t.replace(/^### (.+)$/gm, '<h3>$1</h3>')
       .replace(/^## (.+)$/gm, '<h2>$1</h2>')
       .replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // GitHub-Tabellen: Kopfzeile, Trennzeile aus ---/:--:, dann Datenzeilen.
  // Muss VOR den Listen laufen (eine Trennzeile besteht aus Strichen) und vor
  // den Inline-Regeln, damit **fett** in den Zellen noch greift.
  t = t.replace(
    /(?:^|\n)([ \t]*\|.+\|[ \t]*\n[ \t]*\|[ \t]*:?-{2,}[-: \t|]*\|[ \t]*\n(?:[ \t]*\|.*\|[ \t]*(?:\n|$))+)/g,
    function (_, block) {
      const zeilen = block.trim().split('\n');
      const zellen = function (zeile) {
        return zeile.trim().replace(/^\|/, '').replace(/\|$/, '').split('|')
                    .map(function (c) { return c.trim(); });
      };
      const kopf = zellen(zeilen[0]);
      const ausrichtung = zellen(zeilen[1]).map(function (c) {
        if (/^:-+:$/.test(c)) return ' class="mitte"';
        if (/^-+:$/.test(c)) return ' class="rechts"';
        return '';
      });
      const kl = function (i) { return ausrichtung[i] || ''; };
      let html = '<div class="chat-tabelle"><table><thead><tr>';
      kopf.forEach(function (c, i) { html += '<th' + kl(i) + '>' + c + '</th>'; });
      html += '</tr></thead><tbody>';
      zeilen.slice(2).forEach(function (z) {
        const c = zellen(z);
        // Leerzeilen-Rest ueberspringen: eine Zeile aus lauter leeren Zellen ist keine.
        if (!c.some(function (x) { return x !== ''; })) return;
        html += '<tr>';
        for (let i = 0; i < kopf.length; i++) {
          html += '<td' + kl(i) + '>' + (c[i] === undefined ? '' : c[i]) + '</td>';
        }
        html += '</tr>';
      });
      // Leerzeilen rundherum: sonst klebt ein Satz direkt hinter der Tabelle im
      // selben Absatz-Block und verliert sein <p>.
      return '\n\n' + html + '</tbody></table></div>\n\n';
    });

  t = t.replace(/(?:^|\n)((?:[-*] .+\n?)+)/g, function (_, list) {
    return '\n<ul>' + list.trim().split('\n')
      .map(l => '<li>' + l.replace(/^[-*] /, '') + '</li>').join('') + '</ul>';
  });
  t = t.replace(/(?:^|\n)((?:\d+\. .+\n?)+)/g, function (_, list) {
    return '\n<ol>' + list.trim().split('\n')
      .map(l => '<li>' + l.replace(/^\d+\. /, '') + '</li>').join('') + '</ol>';
  });

  t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>')
       .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
       .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');

  // Links NUR mit http/https-Schema. Ohne diese Schranke bliebe ein vom Modell
  // erzeugtes [Klick](javascript:...) ein klickbarer Skript-Aufruf.
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
                '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  t = t.split(/\n{2,}/).map(function (p) {
    p = p.trim();
    if (!p) return '';
    if (p.indexOf(TOK) === 0 || /^<(h[123]|ul|ol|div class="chat-tabelle")/.test(p)) return p;
    return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
  }).join('\n');

  // [0-9] statt \d: in einem JS-STRING waere \d nur ein 'd'.
  return t.replace(new RegExp(TOK + '([0-9]+)' + TOK, 'g'), function (_, i) {
    const b = blocks[Number(i)];
    return '<div class="chat-code"><div class="chat-code-head"><span>' +
      chatEsc(b.lang || 'code') + '</span><span class="chat-copy" data-code="' +
      encodeURIComponent(b.code) + '">Kopieren</span></div><pre><code>' +
      chatEsc(b.code) + '</code></pre></div>';
  });
}

function chatBindCopy(root) {
  root.querySelectorAll('.chat-copy').forEach(function (el) {
    if (el.dataset.bound) return;
    el.dataset.bound = '1';
    el.addEventListener('click', function () {
      navigator.clipboard.writeText(decodeURIComponent(el.dataset.code || '')).then(function () {
        const old = el.textContent;
        el.textContent = 'Kopiert';
        setTimeout(function () { el.textContent = old; }, 1400);
      });
    });
  });
}

// --- Verlaufsliste ---
// Die geholte Liste bleibt liegen: gesucht wird clientseitig. Ein Suchendpunkt
// waere eine Anfrage je Tastendruck gegen entschluesselte Verlaeufe.
let chatListe = [];

function chatTagGruppe(iso) {
  if (!iso) return 'ÄLTER';
  const d = argusDate(iso);
  if (isNaN(d)) return 'ÄLTER';
  const heute = new Date(); heute.setHours(0, 0, 0, 0);
  const tag = new Date(d); tag.setHours(0, 0, 0, 0);
  const diff = Math.round((heute - tag) / 86400000);
  return diff <= 0 ? 'HEUTE' : diff === 1 ? 'GESTERN' : 'ÄLTER';
}

function chatUhrzeit(iso) {
  const d = argusDate(iso);
  if (isNaN(d)) return '';
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

function chatListeZeichnen() {
  const box = $('chat-list');
  if (!box) return;
  const suche = ($('chat-suche') && $('chat-suche').value || '').trim().toLowerCase();
  const treffer = suche
    ? chatListe.filter(function (c) { return (c.title || '').toLowerCase().indexOf(suche) >= 0; })
    : chatListe;
  if (!treffer.length) {
    box.innerHTML = '<div class="muted" style="font-size:13px;">'
      + (suche ? 'Nichts gefunden.' : 'Noch keine Chats.') + '</div>';
    return;
  }
  // Nach Tagen gruppieren. Die Liste kommt bereits absteigend sortiert.
  let letzte = null, html = '';
  treffer.forEach(function (c) {
    const g = chatTagGruppe(c.last_at || c.created_at);
    if (g !== letzte) { html += '<div class="chat-gruppe">' + g + '</div>'; letzte = g; }
    html += '<div class="chat-item' + (c.id === chatCurrentId ? ' active' : '') + '" data-id="' + c.id + '">'
      + '<span class="chat-item-title" title="' + chatEsc(c.title) + '">' + chatEsc(c.title) + '</span>'
      + '<span class="chat-item-meta">' + chatUhrzeit(c.last_at || c.created_at)
      + ' \u00b7 ' + (c.messages || 0) + ' Nachrichten</span>'
      + '<span class="chat-item-del" data-del="' + c.id + '" title="Chat loeschen">&times;</span>'
      + '</div>';
  });
  box.innerHTML = html;
  box.querySelectorAll('.chat-item').forEach(function (el) {
    el.addEventListener('click', function (ev) {
      if (ev.target.dataset.del) { chatDelete(Number(ev.target.dataset.del), ev); return; }
      chatOpen(Number(el.dataset.id));
    });
  });
}

async function chatLoadList() {
  const box = $('chat-list');
  if (!box) return;
  try {
    const res = await api('/v1/dashboard/chats');
    chatListe = res.chats || [];
    chatListeZeichnen();
  } catch (e) {
    box.innerHTML = '<div class="muted" style="color:var(--color-magenta);">Liste nicht ladbar.</div>';
  }
}

// Gemerkt, weil der Modellname erst mit /config eintrifft -- dann muss die
// Kopfzeile neu geschrieben werden, ohne dass der Aufrufer sie nochmal kennt.
let chatKopfMeta = '';

function chatSetTitle(title, meta) {
  const t = $('chat-title'), m = $('chat-meta');
  if (t) t.textContent = title || 'Neuer Chat';
  chatKopfMeta = meta || '';
  // Modell und Nachrichtenzahl in einer Zeile, wie im Entwurf.
  if (m) {
    const modell = (window.argusModell || '').toUpperCase();
    m.textContent = [modell, chatKopfMeta].filter(Boolean).join(' ' + '·' + ' ');
  }
  chatKopfKv();
}

// Dieselbe Angabe wie in der Navigation, nur hier im Kopf der Unterhaltung.
function chatKopfKv() {
  const el = $('chat-head-kv');
  const nav = $('nav-kv-pct'), comp = $('nav-kv-comp');
  if (!el || !nav) return;
  const p = (nav.textContent || '').trim();
  const c = (comp && comp.textContent || '').trim();
  el.textContent = p && p !== '\u2014' ? ('KV ' + p + (c ? ' \u00b7 ' + c : '')) : '';
}

// Verlauf als Markdown herunterladen -- clientseitig, kein Endpunkt noetig.
function chatExport() {
  const zeilen = ['# ' + ($('chat-title').textContent || 'Chat'), ''];
  document.querySelectorAll('#chat-messages .chat-msg').forEach(function (w) {
    const wer = w.classList.contains('user') ? 'Du' : 'ARGUS';
    const b = w.querySelector('.chat-bubble');
    if (!b) return;
    zeilen.push('## ' + wer, '', b.innerText.trim(), '');
    const q = [...w.querySelectorAll('.src-pille')].map(function (p) { return p.href; });
    if (q.length) zeilen.push('Quellen: ' + q.join(', '), '');
  });
  const blob = new Blob([zeilen.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'argus-chat-' + new Date().toISOString().slice(0, 10) + '.md';
  a.click();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
}

// Kontextauslastung und Groesse der Collection -- die Fusszeile des Entwurfs.
async function chatFussAktualisieren() {
  const el = $('chat-fuss-kontext');
  if (!el) return;
  const nav = $('nav-kv-pct');
  const kv = nav ? (nav.textContent || '').trim() : '';
  let chunks = '';
  try {
    if (window.argusAuth && window.argusAuth.isAdmin()) {
      const r = await api('/v1/dashboard/documents');
      const n = (r.documents || []).reduce(function (a, d) { return a + (d.chunk_count || 0); }, 0);
      if (n) chunks = kvZahl(n) + ' CHUNKS INDEXIERT';
    }
  } catch (e) { /* Fusszeile ist Beiwerk */ }
  el.textContent = [kv && kv !== '\u2014' ? 'KONTEXT ' + kv : '', chunks].filter(Boolean).join(' \u00b7 ');
}

async function chatOpen(id) {
  if (chatBusy) return;
  try {
    const d = await api('/v1/dashboard/chats/' + id);
    chatCurrentId = d.id;
    chatSetTitle(d.title, (d.messages || []).length + ' Nachrichten');
    const box = $('chat-messages');
    box.innerHTML = '';
    (d.messages || []).forEach(function (m) {
      const msg = chatAddMsg(m.role, m.content, true, null, m.id);
      chatApplySteps(msg, m.steps);
      chatSetZeit(msg, m.ts, m.latency_ms);
    });
    if (!(d.messages || []).length) {
      box.innerHTML = '<div class="chat-empty">Neuer Chat &mdash; stell deine Frage.</div>';
    }
    chatBindCopy(box);
    chatScroll(true);
    chatLoadList();
  } catch (e) {
    chatSetTitle('Chat nicht ladbar', '');
  }
}

async function chatNew() {
  if (chatBusy) return;
  try {
    const d = await api('/v1/dashboard/chats', { method: 'POST' });
    chatCurrentId = d.id;
    chatSetTitle('Neuer Chat', '');
    $('chat-messages').innerHTML = '<div class="chat-empty">Neuer Chat &mdash; stell deine Frage.</div>';
    await chatLoadList();
    const inp = $('chat-input');
    if (inp) inp.focus();
  } catch (e) { /* Liste bleibt wie sie ist */ }
}

async function chatDelete(id, ev) {
  if (ev) ev.stopPropagation();
  if (!confirm('Diesen Chat mitsamt Verlauf loeschen?')) return;
  try {
    await api('/v1/dashboard/chats/' + id, { method: 'DELETE' });
    if (id === chatCurrentId) {
      chatCurrentId = null;
      chatSetTitle('Neuer Chat', '');
      $('chat-messages').innerHTML = '<div class="chat-empty">Chat geloescht.</div>';
    }
    await chatLoadList();
  } catch (e) { alert('Loeschen fehlgeschlagen.'); }
}

async function chatDeleteMsg(msgId, following) {
  if (!chatCurrentId || !msgId) return false;
  try {
    await api('/v1/dashboard/chats/' + chatCurrentId + '/messages/' + msgId
              + (following ? '?following=true' : ''), { method: 'DELETE' });
    return true;
  } catch (e) {
    alert('Loeschen fehlgeschlagen: ' + (e.message || e));
    return false;
  }
}

// Alles ab dieser Nachricht aus dem DOM entfernen -- das Backend hat es ebenso getan.
function chatDropFrom(wrap) {
  while (wrap && wrap.nextSibling) wrap.parentNode.removeChild(wrap.nextSibling);
  if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
}

function chatBuildActions(msg, role) {
  const bar = document.createElement('div');
  bar.className = 'chat-actions';

  const knopf = (glyph, titel, fn) => {
    const b = document.createElement('button');
    b.className = 'chat-action';
    b.innerHTML = glyph;
    b.title = titel;
    b.setAttribute('aria-label', titel);
    b.addEventListener('click', fn);
    bar.appendChild(b);
    return b;
  };

  if (role === 'user') {
    // Bearbeiten: Text zurueck in die Eingabe, diese Nachricht und alles danach weg.
    knopf('&#9998;', 'Bearbeiten und erneut senden', async function () {
      if (chatBusy) return;
      const text = msg.bubble.textContent;
      if (!await chatDeleteMsg(msg.wrap.dataset.msgId, true)) return;
      chatDropFrom(msg.wrap);
      const inp = $('chat-input');
      inp.value = text;
      inp.style.height = 'auto';
      inp.style.height = Math.min(inp.scrollHeight, 180) + 'px';
      inp.focus();
      chatLoadList();
    });
  } else {
    // Frage UND Antwort verwerfen: /v1/dashboard/chat persistiert jede eingehende
    // Nachricht -- nur die Antwort zu loeschen liesse die Frage doppelt zurueck.
    knopf(window.argusIcon('reload', 13) + 'NEU GENERIEREN',
          'Antwort verwerfen und erneut versuchen', async function () {
      if (chatBusy) return;
      const vorher = msg.wrap.previousElementSibling;
      if (!vorher || !vorher.classList.contains('user')) {
        alert('Keine vorherige Frage gefunden.');
        return;
      }
      const frage = vorher.querySelector('.chat-bubble').textContent;
      const frageId = vorher.dataset.msgId;
      if (!frageId) { alert('Diese Nachricht ist noch nicht gespeichert.'); return; }
      if (!await chatDeleteMsg(frageId, true)) return;
      chatDropFrom(vorher);
      chatAddMsg('user', frage, false);
      chatScroll(true);
      chatRunTurn(frage, []);
    });
  }

  knopf(window.argusIcon('copy', 13) + 'KOPIEREN', 'Text kopieren', function () {
    copyToClipboard(msg.bubble.textContent);
  });

  // Vorlesen ueber denselben Weg wie die Live-Ansicht.
  if (role !== 'user') {
    knopf(window.argusIcon('laut', 13) + 'VORLESEN', 'Antwort vorlesen', function () {
      if (window.argusSprich) window.argusSprich(msg.bubble.textContent);
    });
  }

  knopf(window.argusIcon('trash', 13), 'Diese Nachricht loeschen', async function () {
    if (chatBusy) return;
    if (!confirm('Diese Nachricht loeschen?')) return;
    if (!await chatDeleteMsg(msg.wrap.dataset.msgId, false)) return;
    msg.wrap.remove();
    chatLoadList();
  });

  return bar;
}

// --- Nachrichten ---
function chatAddMsg(role, content, asMarkdown, images, msgId) {
  const box = $('chat-messages');
  const empty = box.querySelector('.chat-empty');
  if (empty) empty.remove();
  const wrap = document.createElement('div');
  wrap.className = 'chat-msg ' + (role === 'user' ? 'user' : 'ai');
  if (msgId) wrap.dataset.msgId = msgId;
  const who = document.createElement('div');
  who.className = 'chat-role';
  if (role === 'user') {
    who.textContent = 'Du';
  } else {
    // Punkt, Name, Uhrzeit und Dauer -- wie im Entwurf.
    who.innerHTML = '<span class="rolle-punkt"></span><span class="rolle-name">ARGUS</span>'
                  + '<span class="rolle-zustand"></span>'
                  + '<span class="rolle-zeit" id="zeit-' + (msgId || 'neu') + '"></span>';
  }
  const tools = document.createElement('div');
  tools.className = 'chat-tools';
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble';
  if (asMarkdown && role !== 'user') bubble.innerHTML = chatMd(content);
  else bubble.textContent = content || '';
  wrap.appendChild(who);
  // Ueber die Antwort: erst was getan wurde, dann das Ergebnis. Anhaengen statt
  // insertBefore -- die Blase ist hier noch kein Kind von wrap.
  if (role !== 'user') wrap.appendChild(tools);
  // Nur data:image-URIs: ein fremdes Schema (javascript:, http zu einem Tracker)
  // darf hier nicht als src landen.
  const imgs = (images || []).filter(function (u) { return typeof u === 'string' && /^data:image\//.test(u); });
  if (imgs.length) {
    const strip = document.createElement('div');
    strip.className = 'chat-images';
    imgs.forEach(function (u) {
      const im = document.createElement('img');
      im.src = u;
      im.alt = 'Angehängtes Bild';
      strip.appendChild(im);
    });
    wrap.appendChild(strip);
  }
  wrap.appendChild(bubble);
  box.appendChild(wrap);
  const msg = { wrap: wrap, bubble: bubble, tools: tools, reasoning: null,
                zeit: wrap.querySelector('.rolle-zeit'),
                punkt: wrap.querySelector('.rolle-punkt'),
                zustand: wrap.querySelector('.rolle-zustand') };
  msg.actions = chatBuildActions(msg, role);
  wrap.appendChild(msg.actions);
  wrap.classList.toggle('has-id', !!msgId);
  return msg;
}

// Frisch gestreamte Nachrichten haben noch keine ID (persistiert wird am
// Stream-Ende). IDs nachtraeglich zuordnen statt neu zu rendern -- das wuerde
// Denkblock und Werkzeugzeilen wegwerfen.
async function chatAssignIds() {
  if (!chatCurrentId) return;
  try {
    const d = await api('/v1/dashboard/chats/' + chatCurrentId);
    const wraps = document.querySelectorAll('#chat-messages .chat-msg');
    const msgs = d.messages || [];
    const start = Math.max(0, msgs.length - wraps.length);
    for (let i = 0; i < wraps.length; i++) {
      const m = msgs[start + i];
      if (m && m.id) {
        wraps[i].dataset.msgId = m.id;
        wraps[i].classList.add('has-id');
      }
    }
  } catch (e) { /* Aktionen bleiben bis zum naechsten Oeffnen inaktiv */ }
}

// Zustand der laufenden Antwort: Kugel und Beschriftung neben dem Namen. Die
// Farben sind dieselben wie beim Orb in der Live-Ansicht.
const ZUSTAND_WORT = { recherche: 'Recherchiert', denken: 'Denkt nach',
                       antwort: 'Antwortet' };
const ZUSTAND_KLASSEN = ['is-recherche', 'is-denken', 'is-antwort'];

function chatSetZustand(msg, zustand) {
  if (!msg || !msg.punkt) return;
  ZUSTAND_KLASSEN.forEach(function (k) {
    msg.punkt.classList.remove(k);
    if (msg.zustand) msg.zustand.classList.remove(k);
  });
  msg.punkt.classList.toggle('is-aktiv', !!zustand);
  if (!msg.zustand) return;
  if (!zustand) { msg.zustand.textContent = ''; return; }
  msg.punkt.classList.add('is-' + zustand);
  msg.zustand.classList.add('is-' + zustand);
  msg.zustand.textContent = ZUSTAND_WORT[zustand] || '';
}

// Aus der Beschriftung des Schritts ableiten, was gerade laeuft: suchen und lesen
// ist Recherche, alles andere zaehlt als Arbeit am Werkzeug.
function zustandAusWerkzeug(label) {
  return /suche|search|lese|read|recherche|quelle/i.test(String(label || ''))
    ? 'recherche' : 'denken';
}

// Zugeklappt voreingestellt: interessant ist die Antwort, das Denken nur dann,
// wenn sie ueberrascht.
function chatAddReasoning(msg, text) {
  if (!msg.reasoning) {
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'chat-think-head';
    // Beschriftung wie im Entwurf: der Begriff plus die Dauer, die er gebraucht hat.
    head.innerHTML = icon('hirn', 13) + '<span class="think-text">GEDANKENGANG</span>'
                   + icon('chevron', 11);
    msg.denkStart = performance.now();
    const body = document.createElement('div');
    body.className = 'chat-think-body';
    head.addEventListener('click', function () {
      const open = body.classList.toggle('open');
      head.classList.toggle('open', open);
    });
    msg.wrap.insertBefore(head, msg.bubble);
    msg.wrap.insertBefore(body, msg.bubble);
    msg.reasoning = { head: head, body: body, text: '' };
  }
  msg.reasoning.text += text || '';
  msg.reasoning.body.textContent = msg.reasoning.text;
  const t = msg.reasoning.head.querySelector('.think-text');
  if (t && msg.denkStart) {
    t.textContent = 'GEDANKENGANG \u00b7 ' + chatDauer(performance.now() - msg.denkStart, true);
  }
  if (msg.reasoning.body.classList.contains('open')) {
    msg.reasoning.body.scrollTop = msg.reasoning.body.scrollHeight;
  }
}

// Uhrzeit und Dauer im Nachrichtenkopf. Die Dauer steht nur an Antworten.
function chatSetZeit(msg, iso, ms) {
  if (!msg || !msg.zeit) return;
  const d = argusDate(iso);
  let t = isNaN(d) ? '' : String(d.getHours()).padStart(2, '0') + ':'
        + String(d.getMinutes()).padStart(2, '0') + ':'
        + String(d.getSeconds()).padStart(2, '0');
  if (ms) t += (t ? ' \u00b7 ' : '') + chatDauer(ms, true);
  msg.zeit.textContent = t;
}

function chatScroll(force) {
  const box = $('chat-messages');
  if (!box) return;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
  if (force || nearBottom) box.scrollTop = box.scrollHeight;
}

// Eine Aufgabe fragt EINMAL: die erste schreibende Aktion erzeugt die Karte, danach
// laufen die weiteren Schritte durch. Loeschende Aktionen fragen einzeln und doppelt
// (Cooldown). Nach ID abgelegt, damit das resolved-Ereignis seine Karte findet.
const chatConfCards = {};

function chatConfFinish(id, status) {
  const card = chatConfCards[id];
  if (!card) return;
  const TEXT = { approved: '✅ freigegeben', rejected: '⛔ abgelehnt',
                 timeout: '⌛ abgelaufen', cooldown: '⏳ wartet auf zweite Bestätigung' };
  card.actions.innerHTML = '';
  card.state.textContent = TEXT[status] || status;
  card.wrap.classList.remove('is-pending');
  card.wrap.classList.add('is-' + (status === 'approved' ? 'ok' : 'off'));
  if (status !== 'cooldown') delete chatConfCards[id];
}

async function chatConfPost(id, action) {
  return await api('/v1/dashboard/confirmations/' + id + '/' + action, { method: 'POST' });
}

function chatAddConfirmation(msg, ev) {
  // Ein resolved-Ereignis ohne Karte (Reload mitten im Warten) darf nichts anlegen.
  if (ev.phase === 'resolved') { chatConfFinish(ev.id, ev.status); return; }
  if (chatConfCards[ev.id]) return;

  const destructive = (ev.tool_class || '') === 'destructive';
  const wrap = document.createElement('div');
  wrap.className = 'chat-confirm is-pending' + (destructive ? ' is-destructive' : '');

  const head = document.createElement('div');
  head.className = 'chat-confirm-head';
  head.textContent = destructive
    ? '⚠️ Löschende Aktion — Bestätigung nötig'
    : (ev.task_scope ? '🔐 Freigabe für diese Aufgabe' : '🔐 Freigabe nötig');

  const sub = document.createElement('div');
  sub.className = 'chat-confirm-sub';
  sub.textContent = ev.task_scope
    ? 'Gilt für alle schreibenden Schritte dieser Aufgabe. Löschen fragt weiterhin einzeln.'
    : ('Werkzeug: ' + (ev.tool_name || '?'));

  const toggle = document.createElement('div');
  toggle.className = 'chat-confirm-toggle';
  toggle.textContent = 'Details anzeigen';
  const body = document.createElement('pre');
  body.className = 'chat-confirm-body';
  body.textContent = ev.description || '';
  toggle.addEventListener('click', function () {
    const open = body.classList.toggle('open');
    toggle.textContent = open ? 'Details verbergen' : 'Details anzeigen';
  });

  const actions = document.createElement('div');
  actions.className = 'chat-confirm-actions';
  const state = document.createElement('span');
  state.className = 'chat-confirm-state';

  const card = { wrap: wrap, actions: actions, state: state };
  chatConfCards[ev.id] = card;

  function button(text, cls, fn) {
    const b = document.createElement('button');
    b.className = 'chat-confirm-btn ' + cls;
    b.textContent = text;
    b.addEventListener('click', fn);
    return b;
  }

  function renderCooldown(seconds) {
    // Der Countdown macht sichtbar, dass die Wartezeit Absicht ist und kein Haenger.
    actions.innerHTML = '';
    let left = Math.ceil(seconds || 0);
    const btn = button('Endgültig bestätigen', 'is-danger', async function () {
      try {
        const r = await chatConfPost(ev.id, 'confirm-cooldown');
        if (r.status === 'cooldown') { renderCooldown(r.seconds_remaining); return; }
        chatConfFinish(ev.id, r.status);
      } catch (e) { state.textContent = 'Fehler: ' + (e.message || e); }
    });
    actions.appendChild(btn);
    actions.appendChild(button('Ablehnen', 'is-plain', async function () {
      try { chatConfFinish(ev.id, (await chatConfPost(ev.id, 'reject')).status); }
      catch (e) { state.textContent = 'Fehler: ' + (e.message || e); }
    }));
    actions.appendChild(state);
    const tick = setInterval(function () {
      if (!chatConfCards[ev.id]) { clearInterval(tick); return; }
      left -= 1;
      if (left > 0) { btn.disabled = true; state.textContent = 'noch ' + left + ' s'; }
      else { btn.disabled = false; state.textContent = 'bereit'; clearInterval(tick); }
    }, 1000);
    btn.disabled = left > 0;
    state.textContent = left > 0 ? ('noch ' + left + ' s') : 'bereit';
  }

  actions.appendChild(button('Genehmigen', 'is-ok', async function () {
    actions.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    try {
      const r = await chatConfPost(ev.id, 'approve');
      if (r.status === 'cooldown') { renderCooldown(r.cooldown_seconds); return; }
      chatConfFinish(ev.id, r.status);
    } catch (e) {
      state.textContent = 'Fehler: ' + (e.message || e);
      actions.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
    }
  }));
  actions.appendChild(button('Ablehnen', 'is-plain', async function () {
    actions.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    try { chatConfFinish(ev.id, (await chatConfPost(ev.id, 'reject')).status); }
    catch (e) {
      state.textContent = 'Fehler: ' + (e.message || e);
      actions.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
    }
  }));
  actions.appendChild(state);

  wrap.appendChild(head);
  wrap.appendChild(sub);
  wrap.appendChild(toggle);
  wrap.appendChild(body);
  wrap.appendChild(actions);
  msg.wrap.insertBefore(wrap, msg.bubble);
}

// Quellen je Nachricht, fuer die Verweis-Pillen. WeakMap statt Attribut: die
// Liste gehoert zum DOM-Knoten und verschwindet mit ihm.
const chatQuellen = new WeakMap();

// Nur http(s) durchlassen. Die URLs stammen aus Webseiten, also aus fremder
// Hand -- ein javascript:-Schema als Link waere eine offene Tuer.
function chatSichereUrl(u) {
  try {
    const url = new URL(String(u || ''), location.href);
    return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : '';
  } catch (e) { return ''; }
}

function chatDomain(u) {
  try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return u; }
}

// Quellenkarten ueber der Antwort. Kein Favicon: das laege auf einem fremden
// Server, die CSP erlaubt Bilder nur von 'self', und jeder Abruf wuerde verraten,
// welche Seiten gelesen werden. Ein Monogramm aus der Domain reicht.
function chatAddSources(msg, items) {
  if (!items || !items.length) return;
  let box = msg.sources;
  if (!box) {
    box = document.createElement('div');
    box.className = 'chat-sources';
    msg.wrap.insertBefore(box, msg.bubble);
    msg.sources = box;
    chatQuellen.set(msg.wrap, []);
  }
  const liste = chatQuellen.get(msg.wrap) || [];
  items.forEach(function (q) {
    const url = chatSichereUrl(q.url);
    if (!url || liste.some(function (e) { return e.nr === q.nr; })) return;
    liste.push({ nr: q.nr, url: url, titel: q.titel || '' });
    const dom = chatDomain(url);
    const a = document.createElement('a');
    a.className = 'src-pille';
    a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.title = (q.titel || '') + ' — ' + dom;
    const nr = document.createElement('span');
    nr.className = 'src-nr'; nr.textContent = q.nr;
    a.appendChild(nr);
    a.appendChild(document.createTextNode(q.titel || dom));
    box.appendChild(a);
  });
  chatQuellen.set(msg.wrap, liste);
  chatMarkiereVerweise(msg);
}

// Dokumente, aus denen die Dokumentensuche Textstellen geliefert hat. Die Liste
// kommt aus dem Suchergebnis, nicht aus dem Text des Modells. Jedes Ereignis
// traegt die ganze Liste des Turns, deshalb wird die Zeile neu aufgebaut.
// Dateinamen nur ueber textContent: sie stammen aus hochgeladenen Dateien.
function chatAddDocSources(msg, items) {
  if (!msg || !items || !items.length) return;
  let box = msg.dokumente;
  if (!box) {
    box = document.createElement('div');
    box.className = 'chat-sources chat-dokquellen';
    msg.wrap.insertBefore(box, msg.bubble);
    msg.dokumente = box;
  }
  box.textContent = '';
  const kopf = document.createElement('span');
  kopf.className = 'dok-kopf';
  kopf.textContent = 'Gefunden in';
  box.appendChild(kopf);
  items.forEach(function (d) {
    if (!d || !d.name) return;
    const wert = Number(d.score);
    const p = document.createElement('span');
    p.className = 'src-pille is-dok';
    p.appendChild(document.createTextNode(String(d.name)));
    if (isFinite(wert)) {
      const w = document.createElement('span');
      w.className = 'dok-wert';
      w.textContent = wert.toFixed(2).replace('.', ',');
      p.appendChild(w);
      p.title = 'Relevanz ' + w.textContent + ' (0 bis 1)';
    }
    box.appendChild(p);
  });
}

// Eine Pille ohne passende Quelle ist ein toter Verweis -- schlimmer als kein
// Beleg, weil sie Sicherheit vortaeuscht. Solche Nummern werden stillgelegt:
// erfunden vom Modell, oder die Quelle wurde als unsicheres Schema verworfen.
function chatMarkiereVerweise(msg) {
  const liste = chatQuellen.get(msg.wrap) || [];
  msg.bubble.querySelectorAll('.cite').forEach(function (pill) {
    const da = liste.some(function (e) { return String(e.nr) === pill.dataset.nr; });
    pill.classList.toggle('is-tot', !da);
  });
}

// Eine gespeicherte Mitschrift wieder aufbauen: Werkzeugzeilen, Denkblock,
// Quellen. Ohne das zeigt ein neu geladener Verlauf nur den nackten Antworttext.
function chatApplySteps(msg, steps) {
  if (!msg || !steps) return;
  (steps.schritte || []).forEach(function (sch) {
    if (sch.art === 'werkzeug') {
      const row = chatAddTool(msg.tools, sch.text || '', null);
      const d = row.querySelector('.tool-dauer');
      if (d && sch.dauer_ms !== undefined) d.textContent = chatDauer(sch.dauer_ms);
      if (sch.gesperrt) chatToolGesperrt(row);
    }
    else if (sch.art === 'denken') chatAddReasoning(msg, sch.text || '');
  });
  chatAddSources(msg, steps.quellen || []);
  chatAddDocSources(msg, steps.dokumente || []);
}

// Klick auf eine Verweis-Pille oeffnet die zugehoerige Quelle.
document.addEventListener('click', function (ev) {
  const pill = ev.target.closest && ev.target.closest('.cite');
  if (!pill) return;
  const wrap = pill.closest('.chat-msg');
  const liste = wrap ? chatQuellen.get(wrap) : null;
  const treffer = (liste || []).find(function (e) { return String(e.nr) === pill.dataset.nr; });
  if (treffer) window.open(treffer.url, '_blank', 'noopener,noreferrer');
});

// Die Statuszeilen des Backends tragen ein Emoji vorweg (Lupe, Blatt, Uhr). Im
// Entwurf steht links der Haken, danach nur Text -- zwei Symbole nebeneinander
// waeren doppelt gemoppelt. Geschnitten wird nur am Anfang; ein Emoji mitten im
// Suchbegriff gehoert zur Anfrage und bleibt.
function ohneEmoji(text) {
  return String(text || '')
    .replace(/^[\s\u{200D}\u{FE0F}\u{1F300}-\u{1FAFF}\u{2190}-\u{21FF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}]+/u, '')
    .trim();
}

// Ein Schritt je Zeile: Haken, Text, Dauer rechts. Die Dauer kommt erst mit
// dem tool_done-Ereignis nach -- bis dahin steht dort ein Punkt.
function chatAddTool(toolsEl, label, index) {
  toolsEl.classList.add('is-da');
  const row = document.createElement('div');
  row.className = 'tool-zeile';
  if (index !== undefined && index !== null) row.dataset.index = index;
  const sym = document.createElement('span');
  sym.className = 'tool-sym';
  // Ein uebersprungener Schritt traegt keinen Haken, sondern einen Strich.
  const uebersprungen = /übersprungen|uebersprungen|abgebrochen/i.test(label);
  sym.classList.toggle('is-aus', uebersprungen);
  sym.innerHTML = icon(uebersprungen ? 'strich' : 'haken', 14);
  const txt = document.createElement('span');
  txt.className = 'tool-text';
  txt.textContent = ohneEmoji(label);
  const dauer = document.createElement('span');
  dauer.className = 'tool-dauer';
  dauer.textContent = uebersprungen ? '\u2014' : '\u00b7';
  row.appendChild(sym); row.appendChild(txt); row.appendChild(dauer);
  toolsEl.appendChild(row);
  return row;
}

// Deutsches Komma. Der Entwurf ist bewusst unterschiedlich genau:
// Werkzeugschritte auf Hundertstel (0,42 s), die Gesamtdauer auf Zehntel (6,4 s).
function chatDauer(ms, grob) {
  if (ms === null || ms === undefined) return '·';
  if (ms < 100 && !grob) return '<0,1 s';
  const stellen = grob ? 1 : (ms < 10000 ? 2 : 1);
  return (ms / 1000).toFixed(stellen).replace('.', ',') + ' s';
}

// Ein Schritt, den die Sperre nach Firmendaten abgewiesen hat: Schloss statt Haken,
// "gesperrt" statt Dauer. Sonst saehe eine abgewiesene Websuche aus wie eine
// ausgefuehrte.
function chatToolGesperrt(row) {
  if (!row) return;
  row.classList.add('is-gesperrt');
  const sym = row.querySelector('.tool-sym');
  if (sym) { sym.classList.add('is-gesperrt'); sym.innerHTML = icon('schloss', 14); }
  const d = row.querySelector('.tool-dauer');
  if (d) d.textContent = 'gesperrt';
}

function chatToolFertig(toolsEl, index, ms) {
  const row = toolsEl.querySelector('.tool-zeile[data-index="' + index + '"]');
  if (!row) return;
  const d = row.querySelector('.tool-dauer');
  if (d && d.textContent === '\u00b7') d.textContent = chatDauer(ms);
}

// Der Senden-Knopf ist waehrend des Streams der Abbrechen-Knopf: ein Turn kann
// Minuten dauern.
function chatSetBusy(on) {
  chatBusy = on;
  const btn = $('chat-send');
  if (!btn) return;
  btn.innerHTML = on ? window.argusIcon('stop', 15) : window.argusIcon('send', 18);
  btn.title = on ? 'Antwort abbrechen (Esc)' : 'Senden (Enter)';
  btn.setAttribute('aria-label', on ? 'Abbrechen' : 'Senden');
  btn.classList.toggle('is-stop', on);
}

async function chatSend() {
  const inp = $('chat-input');
  const text = (inp.value || '').trim();
  const images = chatAttachments.slice();
  if ((!text && !images.length) || chatBusy) return;
  inp.value = '';
  inp.style.height = 'auto';
  chatAddMsg('user', text, false, images);
  chatClearAttachments();
  chatScroll(true);
  chatRunTurn(text, images);
}

function chatRunTurn(text, images) {
  chatSetBusy(true);
  const ai = chatAddMsg('ai', '', false);
  let acc = '';

  window.argusChatStream(text, {
    sessionId: chatCurrentId,
    images: images,
    onSession: function (sid) {
      // Die vom Backend angelegte Sitzung uebernehmen, damit Folgefragen im selben
      // Verlauf landen.
      if (sid && sid !== chatCurrentId) { chatCurrentId = sid; chatLoadList(); }
    },
    onReasoning: function (t) { chatSetZustand(ai, 'denken'); chatAddReasoning(ai, t); chatScroll(); },
    onConfirmation: function (ev) { chatAddConfirmation(ai, ev); chatScroll(true); },
    onTool: function (label, emoji, index) {
      chatSetZustand(ai, zustandAusWerkzeug(label));
      chatAddTool(ai.tools, label, index); chatScroll();
    },
    onToolDone: function (index, ms) { chatToolFertig(ai.tools, index, ms); },
    onToolBlocked: function (index) {
      chatToolGesperrt(ai.tools.querySelector('.tool-zeile[data-index="' + index + '"]'));
    },
    onSources: function (items) { chatAddSources(ai, items); chatScroll(); },
    onDocSources: function (items) { chatAddDocSources(ai, items); chatScroll(); },
    onToken: function (tk) {
      // Das erste Token beendet Denken und Recherche: ab hier wird geschrieben.
      if (!acc) chatSetZustand(ai, 'antwort');
      // Waehrend des Streams reiner Text -- Markdown je Token flackert bei halb
      // geschriebenen Code-Bloecken.
      acc += tk;
      ai.bubble.textContent = acc;
      chatScroll();
    },
    onDone: function () {
      chatSetZustand(ai, null);
      ai.bubble.innerHTML = chatMd(acc);
      chatMarkiereVerweise(ai);
      chatBindCopy(ai.bubble);
      chatSetBusy(false);
      chatScroll();
      chatLoadList();
      chatAssignIds();
    },
    // Teilantwort bleibt formatiert stehen und wird gekennzeichnet -- sonst haelt
    // man sie beim naechsten Oeffnen fuer eine vollstaendige Antwort.
    onAborted: function () {
      chatSetZustand(ai, null);
      ai.bubble.innerHTML = chatMd(acc);
      chatBindCopy(ai.bubble);
      chatAddTool(ai.tools, '■ abgebrochen');
      chatSetBusy(false);
      chatScroll();
      chatLoadList();
      chatAssignIds();
    },
    onError: function (e) {
      ai.bubble.textContent = 'Fehler: ' + ((e && e.message) || e);
      chatSetBusy(false);
    }
  });
}

function chatInit() {
  const suche = $('chat-suche');
  if (suche) suche.addEventListener('input', chatListeZeichnen);
  const exp = $('chat-export');
  if (exp) exp.addEventListener('click', chatExport);
  const lupe = $('chat-suche-icon');
  if (lupe && window.argusIcon) lupe.innerHTML = window.argusIcon('lupe', 14);
  const nk = $('chat-new');
  if (nk && window.argusIcon && !nk.dataset.symbol) {
    nk.dataset.symbol = '1';
    nk.innerHTML = window.argusIcon('plus', 14) + 'Neuer Chat';
  }

  const inp = $('chat-input'), send = $('chat-send'), neu = $('chat-new');
  if (!inp || !send || !neu) return;
  send.addEventListener('click', function () {
    if (chatBusy) { window.argusChatStop(); return; }
    chatSend();
  });
  neu.addEventListener('click', chatNew);
  inp.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && chatBusy) { e.preventDefault(); window.argusChatStop(); return; }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!chatBusy) chatSend(); }
  });
  inp.addEventListener('input', function () {
    inp.style.height = 'auto';
    inp.style.height = Math.min(inp.scrollHeight, 180) + 'px';
  });

  // Dateien kommen durch ZIEHEN in den Chat -- kein Knopf, kein Dialog. Der Browser
  // gibt einer Seite den echten Pfad nicht heraus (C:\fakepath), also muss der Inhalt
  // hochgeladen werden; ein Pfad zum Weiterreichen existiert gar nicht.
  // dragenter/dragleave feuern auch beim Ueberfahren von Kindelementen -- ohne den
  // Zaehler flackert der Rahmen bei jeder Nachrichtenblase.
  const chatView = $('view-chat');
  if (chatView) {
    let dragTiefe = 0;
    const ziehtDateien = function (e) {
      const dt = e.dataTransfer;
      return !!dt && Array.prototype.indexOf.call(dt.types || [], 'Files') >= 0;
    };
    chatView.addEventListener('dragenter', function (e) {
      if (!ziehtDateien(e)) return;
      e.preventDefault();
      dragTiefe++;
      chatView.classList.add('is-drop-over');
    });
    chatView.addEventListener('dragover', function (e) {
      if (!ziehtDateien(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });
    chatView.addEventListener('dragleave', function () {
      dragTiefe = Math.max(0, dragTiefe - 1);
      if (!dragTiefe) chatView.classList.remove('is-drop-over');
    });
    chatView.addEventListener('drop', function (e) {
      if (!ziehtDateien(e)) return;
      e.preventDefault();
      dragTiefe = 0;
      chatView.classList.remove('is-drop-over');
      chatDropDateien(e.dataTransfer.files);
    });
  }

  const attach = $('chat-attach'), file = $('chat-file');
  if (attach && file) {
    attach.addEventListener('click', function () { file.click(); });
    file.addEventListener('change', function () {
      chatAddFiles(file.files);
      file.value = '';   // gleiche Datei zweimal waehlbar
    });
  }
  // Screenshot per Strg+V direkt ins Eingabefeld.
  inp.addEventListener('paste', function (e) {
    if (!chatMultimodal) return;
    const items = (e.clipboardData && e.clipboardData.files) || [];
    if (items.length) { chatAddFiles(items); e.preventDefault(); }
  });
}

window.argusChat = {
  enter: async function () {
    if (!window.__chatReady) { chatInit(); window.__chatReady = true; }
    await chatCheckMultimodal();
    await chatLoadList();
    if (chatCurrentId == null) {
      const first = document.querySelector('.chat-item');
      if (first) chatOpen(Number(first.dataset.id));
      else chatSetTitle('Neuer Chat', '');
    }
    //--- Kopf-KV und Fusszeile tragen Zahlen, die sich ausserhalb dieser Ansicht
    //--- weiterbewegen -- beim Betreten also nachziehen.
    chatKopfKv();
    chatFussAktualisieren();
  },

  // Bleibt fuer Aufrufer von aussen bestehen, verhaelt sich aber wie das Ziehen:
  // Notiz ins Eingabefeld, abgeschickt wird von Hand.
  noteUpload: function (namen) { chatNotizEinsetzen(namen); }
};

// Gezogene Dateien. Bilder gehen den Multimodal-Weg (data-URI an der Nachricht, nichts
// auf der Platte), alles andere wird hochgeladen. Bewusst NACHEINANDER: der Fortschritt
// steht im Platzhalter, und bei mehreren grossen Dateien bleibt die Reihenfolge sichtbar.
async function chatDropDateien(liste) {
  const alle = Array.prototype.slice.call(liste || []);
  if (!alle.length || chatBusy) return;
  const bildKnopf = $('chat-attach');
  const multimodal = !!bildKnopf && bildKnopf.style.display !== 'none';
  const bilder = multimodal ? alle.filter(function (f) { return /^image\//.test(f.type || ''); }) : [];
  const dateien = alle.filter(function (f) { return bilder.indexOf(f) < 0; });

  if (bilder.length) chatAddFiles(bilder);
  if (!dateien.length) return;
  if (!window.argusFiles || !window.argusFiles.uploadOne) return;

  const inp = $('chat-input');
  const platzhalter = inp ? inp.placeholder : '';
  const namen = [];
  const fehler = [];
  if (inp) inp.disabled = true;
  for (let i = 0; i < dateien.length; i++) {
    const f = dateien[i];
    if (inp) {
      inp.placeholder = 'lade ' + f.name + ' hoch \u2026 (' + (i + 1) + '/' + dateien.length + ')';
    }
    try {
      const res = await window.argusFiles.uploadOne(f, function () {});
      namen.push((res && res.name) || f.name);
    } catch (e) {
      fehler.push(f.name + ': ' + ((e && e.message) || 'fehlgeschlagen'));
    }
  }
  if (inp) {
    inp.disabled = false;
    inp.placeholder = fehler.length ? fehler.join(' \u00b7 ') : platzhalter;
    if (fehler.length) setTimeout(function () { inp.placeholder = platzhalter; }, 8000);
  }
  if (namen.length) chatNotizEinsetzen(namen);
}

// Die Notiz kommt ins EINGABEFELD, nicht in den Verlauf. Der Nutzer schreibt dahinter,
// was mit der Datei geschehen soll, und schickt beides zusammen ab -- damit steht der
// Dateiname in seiner Nachricht und bleibt im Verlauf, den das Backend spaeter liest.
// Frueher wurde hier eine fertige Nachricht VERSCHICKT, die 'Tu noch nichts davon
// ungefragt' enthielt -- die hat Argus rundenlang am Handeln gehindert.
function chatNotizEinsetzen(namen) {
  const inp = $('chat-input');
  if (!inp || !namen || !namen.length) return;
  const notiz = 'Beigelegte Datei(en): '
    + namen.map(function (n) { return 'Uploads/' + n; }).join(', ') + '. ';
  const rest = (inp.value || '').trim();
  inp.value = notiz + rest;
  inp.focus();
  inp.setSelectionRange(inp.value.length, inp.value.length);
  inp.dispatchEvent(new Event('input'));
}
